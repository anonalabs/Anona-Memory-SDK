"""`_parse_deadline` on every shape of `expires_at` a server may send.

This file exists because of a defect it would have caught. `temp.py` used
`datetime.UTC`, which is Python 3.11+, while the package declares
`requires-python = ">=3.10"`. Two of the three references were in the import,
so 3.10 failed loudly at import once any test touched the CLI. The third was a
bare `tzinfo=UTC` inside the naive branch -- and that branch is **dead when
talking to the real server**, which always sends an offset. So running the real
CLI against production proved nothing about it, and a matrix with no test for
an offset-free timestamp went green on a `NameError`.

Nothing here reaches a network. Written to run on the oldest Python the
package claims, so the test cannot be the thing that breaks the old job.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from anona.cli.temp import StartError, _parse_deadline


def _iso(hours: float, *, offset: bool) -> str:
    when = datetime.now(timezone.utc) + timedelta(hours=hours)
    return (when if offset else when.replace(tzinfo=None)).isoformat()


def test_an_offset_bearing_deadline_is_read_as_given():
    got = _parse_deadline(_iso(72, offset=True))
    assert got is not None
    assert 71 * 3600 < got - time.time() <= 72 * 3600


def test_a_naive_deadline_is_read_as_utc():
    # The branch the real server never exercises: no offset, so the code has
    # to supply one. It must agree with the offset-bearing form above rather
    # than being read in whatever zone the machine happens to sit in.
    got = _parse_deadline(_iso(72, offset=False))
    assert got is not None
    assert 71 * 3600 < got - time.time() <= 72 * 3600


def test_the_two_forms_agree():
    # The assertion that makes this a test of correctness and not just of
    # not-raising: the same instant written both ways must parse the same.
    when = datetime.now(timezone.utc) + timedelta(hours=72)
    with_offset = _parse_deadline(when.isoformat())
    without = _parse_deadline(when.replace(tzinfo=None).isoformat())
    assert with_offset is not None and without is not None
    assert abs(with_offset - without) < 1


def test_a_trailing_z_is_accepted():
    when = datetime.now(timezone.utc) + timedelta(hours=72)
    got = _parse_deadline(when.replace(tzinfo=None).isoformat() + "Z")
    assert got is not None
    assert 71 * 3600 < got - time.time() <= 72 * 3600


@pytest.mark.parametrize("value", [None, 17, {}, []])
def test_a_non_string_deadline_is_a_message_not_a_traceback(value):
    # StartError is the clean, message-only exit -- so the caller prints one
    # sentence rather than a traceback. Asserting the type is the point: a bare
    # ValueError or TypeError escaping here reaches the user as a crash.
    with pytest.raises(StartError, match="no expiry time"):
        _parse_deadline(value)


@pytest.mark.parametrize("value", ["", "not-a-date", "2026-13-45T99:99:99", "72 hours"])
def test_an_unparseable_deadline_is_a_message_not_a_traceback(value):
    with pytest.raises(StartError, match="not a date"):
        _parse_deadline(value)
