"""SoftmaxSync's write path.

The adapter shipped writing unscoped and reading scoped. A scoped retrieve is
strict, so `compile()` returned empty forever and looked exactly like a space
still extracting. These assert the two halves agree, because nothing else does.
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

from anona.integrations.softmax import ARTIFACT_NAME, SoftmaxSync


class _Recorder:
    """Stands in for AnonaClient, capturing what the adapter actually sends."""

    def __init__(self) -> None:
        self.batch_calls: list[dict] = []
        self.retrieve_calls: list[dict] = []

    def record_batch(self, space_id, items, user_id=None, agent_id=None, session_id=None):
        self.batch_calls.append({"space_id": space_id, "items": items,
                                 "user_id": user_id, "agent_id": agent_id})
        return {"job_id": "job-1"}

    def retrieve(self, space_id, query, limit=10, user_id=None, agent_id=None, **kw):
        self.retrieve_calls.append({"space_id": space_id, "user_id": user_id,
                                    "agent_id": agent_id})
        return [{"content": "a note", "metadata": {"generation": "2"}}]

    def close(self) -> None:
        pass


def _sync(monkeypatch) -> tuple[SoftmaxSync, _Recorder]:
    s = SoftmaxSync(softmax_token="t", anona_key="anona_live_x",
                    coworld="paintbot", policy="my-policy")
    rec = _Recorder()
    s._anona = rec
    return s, rec


def _artifact_zip(observations: list[dict], generation: int = 0) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(ARTIFACT_NAME, json.dumps(
            {"generation": generation, "observations": observations}))
    return buf.getvalue()


def test_the_write_is_scoped_the_same_way_the_read_is(monkeypatch) -> None:
    s, rec = _sync(monkeypatch)
    monkeypatch.setattr(s, "fetch_artifact",
                        lambda *a, **k: {"generation": 0,
                                         "observations": [{"beat": "catlock"}]})
    monkeypatch.setattr(s, "episode_meta", lambda *a, **k: {"completed_at": "2026-09-11T00:00:00Z"})

    assert s.ingest_episode("ereq_1", "pv_1") == 1
    s.compile("anything")

    write = rec.batch_calls[0]
    read = rec.retrieve_calls[0]
    assert write["user_id"] == read["user_id"] == "my-policy"
    assert write["agent_id"] == read["agent_id"] == "paintbot"
    assert write["space_id"] == read["space_id"] == "softmax-paintbot"


def test_event_time_rides_the_item(monkeypatch) -> None:
    s, rec = _sync(monkeypatch)
    monkeypatch.setattr(s, "fetch_artifact",
                        lambda *a, **k: {"generation": 1, "observations": [{"x": 1}]})
    monkeypatch.setattr(s, "episode_meta", lambda *a, **k: {"completed_at": "2026-09-11T12:00:00Z"})
    s.ingest_episode("ereq_1", "pv_1")
    assert rec.batch_calls[0]["items"][0]["timestamp"] == "2026-09-11T12:00:00Z"


def test_no_artifact_records_nothing(monkeypatch) -> None:
    s, rec = _sync(monkeypatch)
    monkeypatch.setattr(s, "fetch_artifact", lambda *a, **k: None)
    assert s.ingest_episode("ereq_1", "pv_1") == 0
    assert rec.batch_calls == []


def test_compile_trims_to_max_bytes(monkeypatch) -> None:
    s, rec = _sync(monkeypatch)
    long = "x" * 900
    rec.retrieve = lambda *a, **k: [{"content": long, "metadata": {}} for _ in range(10)]
    blob = s.compile("q", max_bytes=1000)
    assert len(blob.encode()) <= 1000
    assert json.loads(blob)["notes"]


def test_compile_reports_the_highest_generation_seen(monkeypatch) -> None:
    s, rec = _sync(monkeypatch)
    rec.retrieve = lambda *a, **k: [
        {"content": "a", "metadata": {"generation": "1"}},
        {"content": "b", "metadata": {"generation": "4"}},
    ]
    assert json.loads(s.compile("q"))["generation"] == 4


def test_fetch_artifact_returns_none_on_a_bad_zip(monkeypatch) -> None:
    s, _ = _sync(monkeypatch)

    class _R:
        status_code = 200
        content = b"not a zip"

    monkeypatch.setattr(s._http, "get", lambda *a, **k: _R())
    assert s.fetch_artifact("ereq_1", "pv_1") is None
