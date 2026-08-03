"""A space_id must survive the URL, whatever the customer named the space.

A space_id is the customer-chosen space name, so it can legally contain
characters that are structural in a URL. httpx encodes spaces and non-ASCII on
the wire, but leaves "/", "?" and "#" alone because they are valid URL syntax —
so interpolating the raw value addressed a different route or silently
truncated the path. Every path-based method was affected, `delete_space`
included, which meant such a space could be created and then never reached
again.

Assertions read httpx's `raw_path` — the bytes actually put on the wire.
`URL.path` and `str(url)` both decode %2F back to "/", so asserting on either
would hide the very thing under test.
"""
from __future__ import annotations

import httpx
import pytest

from anona.client import AnonaClient, _seg


def _client_recording(seen: list[str]) -> AnonaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        return httpx.Response(
            200,
            json={"nodes": [], "edges": [], "items": [], "documents": [], "results": []},
        )

    c = AnonaClient(api_key="anona_live_test", base_url="http://t.local")
    c._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer anona_live_test"},
    )
    return c


def test_seg_encodes_url_structural_characters():
    assert _seg("a/b") == "a%2Fb"
    assert _seg("x?y") == "x%3Fy"
    assert _seg("a#b") == "a%23b"


def test_seg_encodes_spaces_and_unicode_but_keeps_plain_ids_readable():
    assert _seg("QA Space 1") == "QA%20Space%201"
    assert _seg("café") == "caf%C3%A9"
    assert _seg("my-space") == "my-space"
    assert _seg("spc_a1b2c3d4") == "spc_a1b2c3d4"


@pytest.mark.parametrize(
    "space_id", ["a/b", "x?y", "a#b", "QA Space 1", "café", "plain"]
)
def test_space_id_stays_in_one_path_segment(space_id):
    seen: list[str] = []
    _client_recording(seen).get_graph(space_id)

    raw = seen[-1]
    parts = raw.split("?")[0].split("/")
    assert parts[:3] == ["", "v1", "spaces"], raw
    assert len(parts) == 5, raw
    assert parts[4] == "graph", raw
    assert "#" not in raw, raw


def test_delete_space_reaches_the_awkward_name():
    """The sharp edge: without encoding, a space named 'a/b' could be created
    and then never removed."""
    seen: list[str] = []
    _client_recording(seen).delete_space("a/b")
    assert seen[-1].endswith("/v1/spaces/a%2Fb"), seen[-1]


def test_other_path_ids_are_encoded_too():
    seen: list[str] = []
    c = _client_recording(seen)

    c.get_job("a/b", "j/1")
    assert seen[-1].endswith("/v1/spaces/a%2Fb/jobs/j%2F1"), seen[-1]

    c.get_entity("a/b", "e/1")
    assert seen[-1].endswith("/v1/spaces/a%2Fb/entities/e%2F1"), seen[-1]

    c.delete_memory("a/b", "m/1")
    assert seen[-1].endswith("/v1/spaces/a%2Fb/memories/m%2F1"), seen[-1]

    c.delete_document("a/b", "d/1")
    assert seen[-1].endswith("/v1/spaces/a%2Fb/documents/d%2F1"), seen[-1]


def test_unencoded_interpolation_really_does_break():
    """Guards the premise, independent of the fix.

    Without this it would be easy to read the tests above as asserting
    something that was always true. These are the paths the old code produced.
    """
    broken = httpx.URL("http://t.local/v1/spaces/a/b/graph")
    assert broken.raw_path.decode() == "/v1/spaces/a/b/graph"
    assert len(broken.raw_path.decode().split("/")) == 6  # one segment too many

    truncated = httpx.URL("http://t.local/v1/spaces/x?y/graph")
    assert truncated.raw_path.decode().split("?")[0] == "/v1/spaces/x"

    fragment = httpx.URL("http://t.local/v1/spaces/a#b/graph")
    assert fragment.raw_path.decode() == "/v1/spaces/a"
