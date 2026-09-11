"""SoftmaxMemory, the half that runs inside a sealed pod.

Every test here is offline on purpose: the class exists precisely because a
Softmax player pod has no network, so anything needing one would be testing
something the adapter must never do.
"""
from __future__ import annotations

import json
import zipfile

import pytest

from anona.integrations.softmax import ARTIFACT_NAME, MEMORY_ENV, SoftmaxMemory


def _artifact(path) -> dict:
    with zipfile.ZipFile(path) as z:
        return json.loads(z.read(ARTIFACT_NAME))


def test_generation_zero_when_nothing_recalled() -> None:
    mem = SoftmaxMemory(env={}, quiet=True)
    assert mem.generation == 0
    assert mem.recall() == []


def test_recall_parses_and_advances_the_generation() -> None:
    blob = json.dumps({"generation": 3, "notes": ["beat catlock on arrows", "fog hides enemies"]})
    mem = SoftmaxMemory(env={MEMORY_ENV: blob}, quiet=True)
    assert mem.generation == 4
    assert mem.recall() == ["beat catlock on arrows", "fog hides enemies"]


def test_unparseable_recall_is_kept_not_discarded() -> None:
    """A blob that will not parse is still evidence of intent."""
    mem = SoftmaxMemory(env={MEMORY_ENV: "not json at all"}, quiet=True)
    assert mem.recall() == ["not json at all"]
    assert mem.generation == 0


def test_non_string_notes_are_filtered() -> None:
    blob = json.dumps({"generation": 0, "notes": ["keep", 42, None, {"no": 1}]})
    mem = SoftmaxMemory(env={MEMORY_ENV: blob}, quiet=True)
    assert mem.recall() == ["keep"]


def test_flush_writes_a_file_url(tmp_path) -> None:
    """`coworld run-episode` sets a file:// URL, not an http one. A client that
    handles only the hosted shape loses every local write silently."""
    target = tmp_path / "nested" / "policy_artifact_0.zip"
    mem = SoftmaxMemory(
        env={"COWORLD_PLAYER_ARTIFACT_UPLOAD_URL": f"file://{target}"}, quiet=True
    )
    mem.remember("held the socket")
    mem.remember({"opponent": "baseline", "lost": True})

    assert mem.flush() is True
    payload = _artifact(target)
    assert payload["generation"] == 0
    assert len(payload["observations"]) == 2
    assert payload["observations"][0]["note"] == "held the socket"
    assert payload["observations"][1]["opponent"] == "baseline"


def test_flush_writes_a_bare_path(tmp_path) -> None:
    target = tmp_path / "policy_artifact_0.zip"
    mem = SoftmaxMemory(env={"COWORLD_PLAYER_ARTIFACT_UPLOAD_URL": str(target)}, quiet=True)
    mem.remember("one")
    assert mem.flush() is True
    assert _artifact(target)["observations"][0]["note"] == "one"


def test_flush_carries_the_recalled_generation_forward(tmp_path) -> None:
    target = tmp_path / "a.zip"
    mem = SoftmaxMemory(
        env={
            MEMORY_ENV: json.dumps({"generation": 2, "notes": []}),
            "COWORLD_PLAYER_ARTIFACT_UPLOAD_URL": str(target),
        },
        quiet=True,
    )
    mem.remember("x")
    mem.flush()
    assert _artifact(target)["generation"] == 3


def test_flush_without_an_endpoint_is_a_noop_not_a_crash() -> None:
    """Fail open: a policy that dies because memory was unavailable is worse
    than one that plays without it."""
    mem = SoftmaxMemory(env={}, quiet=True)
    mem.remember("something")
    assert mem.flush() is False


def test_flush_to_an_unwritable_path_returns_false(tmp_path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    mem = SoftmaxMemory(
        env={"COWORLD_PLAYER_ARTIFACT_UPLOAD_URL": str(blocker / "deeper" / "a.zip")},
        quiet=True,
    )
    mem.remember("x")
    assert mem.flush() is False


def test_remember_all_and_extra(tmp_path) -> None:
    target = tmp_path / "a.zip"
    mem = SoftmaxMemory(env={"COWORLD_PLAYER_ARTIFACT_UPLOAD_URL": str(target)}, quiet=True)
    mem.remember_all(["a", {"b": 1}])
    mem.flush(extra={"seat": 3})
    payload = _artifact(target)
    assert payload["seat"] == 3
    assert len(payload["observations"]) == 2


@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_recall_is_generation_zero(raw: str) -> None:
    mem = SoftmaxMemory(env={MEMORY_ENV: raw}, quiet=True)
    assert mem.generation == 0
    assert mem.recall() == []
