"""Unit tests for AnonaClient — HTTP mocked with respx, no live gateway."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from anona.client import AnonaClient, AnonaError

BASE = "http://test.anona.local"
KEY = "anona_live_testkey"
SPACE = "space-1"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def client():
    c = AnonaClient(api_key=KEY, base_url=BASE)
    yield c
    c.close()


# ── record (sync path unchanged) ───────────────────────────────────────────────


@respx.mock
def test_record_sync(client):
    route = respx.post(f"{BASE}/v1/record").mock(
        return_value=httpx.Response(201, json={"memory_id": "mem_1", "status": "stored"})
    )
    out = client.record(space_id=SPACE, content="hello")
    assert out["memory_id"] == "mem_1"
    body = json.loads(route.calls.last.request.content)
    assert body["space_id"] == SPACE
    # No async flag on the default path.
    assert "async" not in body


# ── record(background=True) → job ──────────────────────────────────────────────


@respx.mock
def test_record_background_sends_async_flag(client):
    route = respx.post(f"{BASE}/v1/record").mock(
        return_value=httpx.Response(
            201, json={"memory_id": None, "job_id": "job_1", "status": "processing"}
        )
    )
    out = client.record(space_id=SPACE, content="hello", background=True)
    assert out["job_id"] == "job_1"
    assert out["status"] == "processing"
    body = json.loads(route.calls.last.request.content)
    assert body["async"] is True


# ── record_batch ───────────────────────────────────────────────────────────────


@respx.mock
def test_record_batch(client):
    route = respx.post(f"{BASE}/v1/record/batch").mock(
        return_value=httpx.Response(
            202, json={"job_id": "job_b", "status": "processing", "accepted": 2}
        )
    )
    out = client.record_batch(
        space_id=SPACE, items=[{"content": "a"}, {"content": "b"}]
    )
    assert out["accepted"] == 2
    assert out["job_id"] == "job_b"
    body = json.loads(route.calls.last.request.content)
    assert len(body["items"]) == 2


# ── get_job ────────────────────────────────────────────────────────────────────


@respx.mock
def test_get_job(client):
    respx.get(f"{BASE}/v1/spaces/{SPACE}/jobs/job_1").mock(
        return_value=httpx.Response(
            200, json={"job_id": "job_1", "status": "completed", "error": None}
        )
    )
    out = client.get_job(space_id=SPACE, job_id="job_1")
    assert out["status"] == "completed"


@respx.mock
def test_error_raises_anonaerror(client):
    respx.get(f"{BASE}/v1/spaces/{SPACE}/jobs/nope").mock(
        return_value=httpx.Response(404, json={"error": {"code": "not_found"}})
    )
    with pytest.raises(AnonaError) as ei:
        client.get_job(space_id=SPACE, job_id="nope")
    assert ei.value.status_code == 404


# ── async parity ───────────────────────────────────────────────────────────────


@pytest.mark.anyio
@respx.mock
async def test_async_record_background_and_get_job():
    route = respx.post(f"{BASE}/v1/record").mock(
        return_value=httpx.Response(201, json={"job_id": "job_a", "status": "processing"})
    )
    respx.get(f"{BASE}/v1/spaces/{SPACE}/jobs/job_a").mock(
        return_value=httpx.Response(200, json={"job_id": "job_a", "status": "pending"})
    )
    async with AnonaClient(api_key=KEY, base_url=BASE) as c:
        job = await c.async_record(space_id=SPACE, content="x", background=True)
        assert job["job_id"] == "job_a"
        assert json.loads(route.calls.last.request.content)["async"] is True
        status = await c.async_get_job(space_id=SPACE, job_id="job_a")
        assert status["status"] == "pending"
