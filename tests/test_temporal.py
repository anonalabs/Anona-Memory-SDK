"""Event-time writes and point-in-time recall — HTTP mocked, no live API.

Every one of these parameters shipped in the API and was documented before this
package supported it, so a caller following the docs got a TypeError. The tests
pin the wire field names, since that is what a missed port gets wrong.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from anona.client import AnonaClient

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


# ── record(timestamp=...) — when the event happened ───────────────────────────


@respx.mock
def test_record_sends_event_timestamp(client):
    route = respx.post(f"{BASE}/v1/record").mock(
        return_value=httpx.Response(201, json={"memory_id": "m", "status": "stored"})
    )
    client.record(
        space_id=SPACE, content="shipped the beta", timestamp="2025-06-14T10:00:00Z"
    )
    body = json.loads(route.calls.last.request.content)
    assert body["timestamp"] == "2025-06-14T10:00:00Z"


@respx.mock
def test_record_omits_timestamp_when_absent(client):
    route = respx.post(f"{BASE}/v1/record").mock(
        return_value=httpx.Response(201, json={"memory_id": "m", "status": "stored"})
    )
    client.record(space_id=SPACE, content="hello")
    body = json.loads(route.calls.last.request.content)
    # Absent, not null: the API forbids unknown keys and treats null
    # differently from a field that was never sent.
    assert "timestamp" not in body


# ── retrieve(as_of=..., query_timestamp=...) ──────────────────────────────────


@respx.mock
def test_retrieve_sends_as_of_and_query_timestamp(client):
    route = respx.post(f"{BASE}/v1/retrieve").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    client.retrieve(
        space_id=SPACE,
        query="q",
        as_of="2026-06-01T00:00:00Z",
        query_timestamp="2026-01-01T00:00:00Z",
    )
    body = json.loads(route.calls.last.request.content)
    # Two distinct fields, deliberately. `as_of` filters on when a memory was
    # recorded; `query_timestamp` only re-ranks. Sending one for the other
    # turns a hard cutoff into a scoring hint and returns memories the caller
    # asked to exclude.
    assert body["as_of"] == "2026-06-01T00:00:00Z"
    assert body["query_timestamp"] == "2026-01-01T00:00:00Z"


@respx.mock
def test_retrieve_without_temporal_args_is_unchanged(client):
    route = respx.post(f"{BASE}/v1/retrieve").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    client.retrieve(space_id=SPACE, query="q")
    body = json.loads(route.calls.last.request.content)
    assert body == {"space_id": SPACE, "query": "q", "limit": 10, "mode": "accurate"}


# ── retrieve(occurred_after=..., occurred_before=...) ─────────────────────────


@respx.mock
def test_retrieve_sends_the_event_time_window(client):
    route = respx.post(f"{BASE}/v1/retrieve").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    client.retrieve(
        space_id=SPACE,
        query="q",
        occurred_after="2024-06-01T00:00:00Z",
        occurred_before="2024-06-30T23:59:59Z",
    )
    body = json.loads(route.calls.last.request.content)
    # A different axis from `as_of`, not a variant of it: this bounds when the
    # thing happened, `as_of` bounds when we were told. History imported today
    # all shares one record time, so only this one can address it.
    assert body["occurred_after"] == "2024-06-01T00:00:00Z"
    assert body["occurred_before"] == "2024-06-30T23:59:59Z"


@respx.mock
def test_retrieve_sends_one_bound_alone(client):
    route = respx.post(f"{BASE}/v1/retrieve").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    client.retrieve(space_id=SPACE, query="q", occurred_after="2024-06-01T00:00:00Z")
    body = json.loads(route.calls.last.request.content)
    # Either bound alone is a valid open-ended window, so the other must be
    # absent rather than sent as null.
    assert body["occurred_after"] == "2024-06-01T00:00:00Z"
    assert "occurred_before" not in body


# ── the async client carries the same five ────────────────────────────────────


@pytest.mark.anyio
@respx.mock
async def test_async_record_sends_event_timestamp(client):
    route = respx.post(f"{BASE}/v1/record").mock(
        return_value=httpx.Response(201, json={"memory_id": "m", "status": "stored"})
    )
    await client.async_record(
        space_id=SPACE, content="c", timestamp="2025-06-14T10:00:00Z"
    )
    body = json.loads(route.calls.last.request.content)
    assert body["timestamp"] == "2025-06-14T10:00:00Z"
    await client.aclose()


@pytest.mark.anyio
@respx.mock
async def test_async_retrieve_sends_as_of_and_query_timestamp(client):
    route = respx.post(f"{BASE}/v1/retrieve").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    await client.async_retrieve(
        space_id=SPACE,
        query="q",
        as_of="2026-06-01T00:00:00Z",
        query_timestamp="2026-01-01T00:00:00Z",
    )
    body = json.loads(route.calls.last.request.content)
    # Parity matters more here than anywhere: the async client is a separate
    # method body, so every one of these had to be added twice, and the sync
    # tests above would pass with the async half missing entirely.
    assert body["as_of"] == "2026-06-01T00:00:00Z"
    assert body["query_timestamp"] == "2026-01-01T00:00:00Z"
    await client.aclose()


@pytest.mark.anyio
@respx.mock
async def test_async_retrieve_sends_the_event_time_window(client):
    route = respx.post(f"{BASE}/v1/retrieve").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    await client.async_retrieve(
        space_id=SPACE,
        query="q",
        occurred_after="2024-06-01T00:00:00Z",
        occurred_before="2024-06-30T23:59:59Z",
    )
    body = json.loads(route.calls.last.request.content)
    assert body["occurred_after"] == "2024-06-01T00:00:00Z"
    assert body["occurred_before"] == "2024-06-30T23:59:59Z"
    await client.aclose()
