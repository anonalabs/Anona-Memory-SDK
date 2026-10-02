"""Space routing on `record` / `record_batch` / `retrieve` and their async twins.

`route="auto"` lets the server pick the space instead of the caller naming one.
What is pinned here is the part a customer can observe: the request shape, the
two local pre-flight refusals (both selectors, neither selector — the server's
`422 route_conflict`, reproduced client-side so a doomed call costs no round
trip), that an addressed call still serialises byte-for-byte as it did before
routing existed, and the two response shapes the types must not misrepresent —
`searched` is *absent* on an addressed read, and an abstention is one entry
whose `space_id` is null.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from anona.client import AnonaClient, AnonaError

BASE = "http://test.anona.local"
KEY = "anona_live_testkey"
SPACE = "space-1"

# What a routed write answers with. `confidence` is null on a rule or a
# fallback decision — neither is a judgement with a score behind it.
ROUTED_WRITE = {
    "memory_id": "m1",
    "status": "stored",
    "routed_to": [
        {
            "space_id": "billing",
            "confidence": 0.91,
            "stage": "model",
            "reason": "invoice terms",
        }
    ],
}

ROUTED_READ = {
    "results": [{"memory_id": "m1", "content": "net-30"}],
    "searched": [
        {
            "space_id": "billing",
            "confidence": 0.87,
            "stage": "model",
            "reason": "invoice terms",
        }
    ],
}

# An abstention: nothing was searched, so there are no results, and the single
# entry carries the reason. Not an empty list — an empty list has nowhere to put
# it — and `space_id` is null rather than the default space, which on a read is
# the bag of everything that fitted nowhere.
ABSTAINED_READ = {
    "results": [],
    "searched": [
        {
            "space_id": None,
            "confidence": 0.21,
            "stage": "model",
            "reason": "no_space_fits",
        }
    ],
}

BATCH_RESULT = {"job_id": "j1", "job_ids": ["j1", "j2"], "status": "queued", "accepted": 2}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def client():
    c = AnonaClient(api_key=KEY, base_url=BASE)
    yield c
    c.close()


# ── record: the routed request shape ──────────────────────────────────────────


@respx.mock
def test_routed_record_sends_route_and_no_space_id(client):
    route = respx.post(f"{BASE}/v1/record").mock(
        return_value=httpx.Response(201, json=ROUTED_WRITE)
    )
    client.record(content="Acme pays net-30", route="auto")
    body = json.loads(route.calls.last.request.content)
    assert body["route"] == "auto"
    # A routed write must not name a space: sending both is the 422.
    assert "space_id" not in body


@respx.mock
def test_routed_record_sends_fallback_and_max_targets(client):
    route = respx.post(f"{BASE}/v1/record").mock(
        return_value=httpx.Response(201, json=ROUTED_WRITE)
    )
    client.record(content="c", route="auto", fallback_space_id="inbox", max_targets=1)
    body = json.loads(route.calls.last.request.content)
    assert body["fallback_space_id"] == "inbox"
    assert body["max_targets"] == 1


@respx.mock
def test_max_targets_above_the_cap_is_left_to_the_server(client):
    # The cap is 1 today and the server 422s 2. Not clamped client-side, so
    # widening it needs no new SDK release — but it must reach the wire as sent,
    # not be silently rewritten to something that succeeds.
    route = respx.post(f"{BASE}/v1/record").mock(
        return_value=httpx.Response(201, json=ROUTED_WRITE)
    )
    client.record(content="c", route="auto", max_targets=3)
    assert json.loads(route.calls.last.request.content)["max_targets"] == 3


@respx.mock
def test_routed_to_is_parsed_off_the_response(client):
    respx.post(f"{BASE}/v1/record").mock(return_value=httpx.Response(201, json=ROUTED_WRITE))
    target = client.record(content="c", route="auto")["routed_to"][0]
    assert target["space_id"] == "billing"
    assert target["stage"] == "model"
    assert target["confidence"] == 0.91
    assert target["reason"] == "invoice terms"


# ── record: the two local refusals ────────────────────────────────────────────


@respx.mock
def test_record_with_both_selectors_fails_locally(client):
    route = respx.post(f"{BASE}/v1/record")
    with pytest.raises(AnonaError) as exc:
        client.record(space_id=SPACE, content="c", route="auto")
    assert exc.value.status_code == 422
    assert exc.value.code == "route_conflict"
    # Failing fast means exactly that: nothing went out.
    assert not route.called


@respx.mock
def test_record_with_neither_selector_fails_locally(client):
    route = respx.post(f"{BASE}/v1/record")
    with pytest.raises(AnonaError) as exc:
        client.record(content="c")
    assert exc.value.code == "route_conflict"
    assert not route.called


@respx.mock
def test_missing_content_is_still_a_type_error(client):
    # `content` only carries a default so `space_id` could have one. Omitting it
    # must stay the error Python would have raised, not a 422 from the server.
    route = respx.post(f"{BASE}/v1/record")
    with pytest.raises(TypeError, match="content"):
        client.record(space_id=SPACE)
    assert not route.called


# ── record: the addressed write is unchanged ──────────────────────────────────


@respx.mock
def test_addressed_write_is_byte_identical(client):
    route = respx.post(f"{BASE}/v1/record").mock(
        return_value=httpx.Response(201, json={"memory_id": "m1", "status": "stored"})
    )
    client.record(
        SPACE,
        "Alice prefers email",
        {"source": "zendesk"},
        ["agent:triage"],
        True,
        "alice",
        "triage",
        "sess1",
        "2026-08-01T10:00:00Z",
        "from support chat",
    )
    expected = {
        "space_id": SPACE,
        "content": "Alice prefers email",
        "metadata": {"source": "zendesk"},
        "context": "from support chat",
        "tags": ["agent:triage"],
        "user_id": "alice",
        "agent_id": "triage",
        "session_id": "sess1",
        "timestamp": "2026-08-01T10:00:00Z",
        "async": True,
    }
    sent = json.loads(route.calls.last.request.content)
    # Keys and their order both, which is what "the same bytes" means for a
    # payload httpx serialises for us.
    assert sent == expected
    assert list(sent) == list(expected)


@respx.mock
@pytest.mark.anyio
async def test_async_record_routes_and_carries_context(client):
    # The async writer built its own payload and had no `context` parameter at
    # all; it shares `_record_body` with the sync one now, so it cannot drift
    # again.
    route = respx.post(f"{BASE}/v1/record").mock(
        return_value=httpx.Response(201, json=ROUTED_WRITE)
    )
    result = await client.async_record(content="c", context="from chat", route="auto")
    body = json.loads(route.calls.last.request.content)
    assert body["route"] == "auto" and "space_id" not in body
    assert body["context"] == "from chat"
    assert result["routed_to"][0]["stage"] == "model"
    await client.aclose()


@respx.mock
@pytest.mark.anyio
async def test_async_record_refuses_both_and_neither(client):
    route = respx.post(f"{BASE}/v1/record")
    for kwargs in ({"space_id": SPACE, "route": "auto"}, {}):
        with pytest.raises(AnonaError) as exc:
            await client.async_record(content="c", **kwargs)
        assert exc.value.code == "route_conflict"
    assert not route.called
    await client.aclose()


# ── retrieve: the routed request shape ────────────────────────────────────────


@respx.mock
def test_routed_retrieve_sends_route_and_no_space_id(client):
    route = respx.post(f"{BASE}/v1/retrieve").mock(
        return_value=httpx.Response(200, json=ROUTED_READ)
    )
    client.retrieve(query="acme payment terms", route="auto")
    body = json.loads(route.calls.last.request.content)
    assert body["route"] == "auto"
    assert "space_id" not in body


@respx.mock
def test_routed_retrieve_sends_fallback_space_id(client):
    route = respx.post(f"{BASE}/v1/retrieve").mock(
        return_value=httpx.Response(200, json=ROUTED_READ)
    )
    client.retrieve(query="q", route="auto", fallback_space_id="inbox")
    assert json.loads(route.calls.last.request.content)["fallback_space_id"] == "inbox"


@respx.mock
def test_searched_is_parsed_off_the_response(client):
    respx.post(f"{BASE}/v1/retrieve").mock(return_value=httpx.Response(200, json=ROUTED_READ))
    memories = client.retrieve(query="q", route="auto")
    # Still an ordinary list, which is what keeps every existing caller working.
    assert isinstance(memories, list)
    assert [m["memory_id"] for m in memories] == ["m1"]
    looked = memories.searched
    assert len(looked) == 1
    assert looked[0]["space_id"] == "billing"
    assert looked[0]["stage"] == "model"
    assert looked[0]["confidence"] == 0.87
    assert looked[0]["reason"] == "invoice terms"


@respx.mock
def test_abstention_parses_with_a_null_space_id(client):
    respx.post(f"{BASE}/v1/retrieve").mock(
        return_value=httpx.Response(200, json=ABSTAINED_READ)
    )
    memories = client.retrieve(query="q", route="auto")
    assert list(memories) == []
    assert len(memories.searched) == 1
    assert memories.searched[0]["space_id"] is None
    assert memories.searched[0]["reason"] == "no_space_fits"


@respx.mock
def test_searched_is_none_on_an_addressed_read(client):
    # The server omits the key entirely rather than sending null, so the two
    # cases stay indistinguishable — which is exactly what they are to a caller.
    respx.post(f"{BASE}/v1/retrieve").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    assert client.retrieve(SPACE, "q").searched is None


@respx.mock
def test_results_still_behave_as_a_plain_list(client):
    # `retrieve` returns a `list` subclass rather than a wrapper precisely so
    # that this keeps working.
    respx.post(f"{BASE}/v1/retrieve").mock(return_value=httpx.Response(200, json=ROUTED_READ))
    memories = client.retrieve(query="q", route="auto")
    assert json.dumps(memories) == json.dumps(ROUTED_READ["results"])
    assert memories + [{"x": 1}] == [*ROUTED_READ["results"], {"x": 1}]


# ── retrieve: the two local refusals, and the unchanged addressed read ────────


@respx.mock
def test_retrieve_refuses_both_and_neither(client):
    route = respx.post(f"{BASE}/v1/retrieve")
    for kwargs in ({"space_id": SPACE, "route": "auto"}, {}):
        with pytest.raises(AnonaError) as exc:
            client.retrieve(query="q", **kwargs)
        assert exc.value.status_code == 422
        assert exc.value.code == "route_conflict"
    assert not route.called


@respx.mock
def test_missing_query_is_still_a_type_error(client):
    route = respx.post(f"{BASE}/v1/retrieve")
    with pytest.raises(TypeError, match="query"):
        client.retrieve(SPACE)
    assert not route.called


@respx.mock
def test_addressed_read_is_byte_identical(client):
    route = respx.post(f"{BASE}/v1/retrieve").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    client.retrieve(
        SPACE,
        "what did alice say",
        5,
        "fast",
        "alice",
        "triage",
        "sess1",
        "2026-08-01T10:00:00Z",
        "2026-08-02T10:00:00Z",
        "2026-07-01T00:00:00Z",
        "2026-07-31T00:00:00Z",
        20,
        ["fact"],
        ["agent:triage"],
        "all_strict",
        [{"tags": ["x"]}],
        False,
        0.2,
        "me",
    )
    expected = {
        "space_id": SPACE,
        "query": "what did alice say",
        "limit": 5,
        "mode": "fast",
        "top_k": 20,
        "memory_type": ["fact"],
        "tags": ["agent:triage"],
        "tags_match": "all_strict",
        "tag_groups": [{"tags": ["x"]}],
        "prefer_observations": False,
        "min_score": 0.2,
        "member_id": "me",
        "user_id": "alice",
        "agent_id": "triage",
        "session_id": "sess1",
        "as_of": "2026-08-01T10:00:00Z",
        "query_timestamp": "2026-08-02T10:00:00Z",
        "occurred_after": "2026-07-01T00:00:00Z",
        "occurred_before": "2026-07-31T00:00:00Z",
    }
    sent = json.loads(route.calls.last.request.content)
    assert sent == expected
    assert list(sent) == list(expected)


@respx.mock
@pytest.mark.anyio
async def test_async_retrieve_routes_and_matches_the_sync_payload(client):
    # The async twin built its own payload and had drifted from the sync one
    # before; it shares `_search_body` now, so it cannot drift again.
    route = respx.post(f"{BASE}/v1/retrieve").mock(
        return_value=httpx.Response(200, json=ROUTED_READ)
    )
    memories = await client.async_retrieve(query="q", route="auto")
    assert memories.searched[0]["stage"] == "model"

    client.retrieve(SPACE, "q", tags=["t"], min_score=0.1)
    sync_raw = route.calls.last.request.content
    await client.async_retrieve(SPACE, "q", tags=["t"], min_score=0.1)
    assert route.calls.last.request.content == sync_raw
    await client.aclose()


# ── record_batch: routed as one unit ──────────────────────────────────────────


@respx.mock
def test_routed_batch_sends_the_write_fields(client):
    route = respx.post(f"{BASE}/v1/record/batch").mock(
        return_value=httpx.Response(202, json=BATCH_RESULT)
    )
    client.record_batch(
        items=[{"content": "a"}],
        route="auto",
        fallback_space_id="inbox",
        max_targets=1,
    )
    body = json.loads(route.calls.last.request.content)
    assert body["route"] == "auto"
    assert "space_id" not in body
    assert body["fallback_space_id"] == "inbox"
    assert body["max_targets"] == 1


@respx.mock
def test_batch_response_carries_job_ids_and_no_routed_to(client):
    # The whole batch is routed as one unit, so there is no per-item decision to
    # report. Asserted so nobody invents a field the server does not return.
    respx.post(f"{BASE}/v1/record/batch").mock(
        return_value=httpx.Response(202, json=BATCH_RESULT)
    )
    result = client.record_batch(items=[{"content": "a"}, {"content": "b"}], route="auto")
    assert result["job_ids"] == ["j1", "j2"]
    assert "routed_to" not in result


@respx.mock
def test_batch_refuses_both_and_neither(client):
    route = respx.post(f"{BASE}/v1/record/batch")
    for kwargs in ({"space_id": SPACE, "route": "auto"}, {}):
        with pytest.raises(AnonaError) as exc:
            client.record_batch(items=[{"content": "a"}], **kwargs)
        assert exc.value.status_code == 422
        assert exc.value.code == "route_conflict"
    assert not route.called


@respx.mock
def test_batch_missing_items_is_still_a_type_error(client):
    route = respx.post(f"{BASE}/v1/record/batch")
    with pytest.raises(TypeError, match="items"):
        client.record_batch(SPACE)
    assert not route.called


@respx.mock
def test_addressed_batch_is_byte_identical(client):
    route = respx.post(f"{BASE}/v1/record/batch").mock(
        return_value=httpx.Response(202, json=BATCH_RESULT)
    )
    client.record_batch(SPACE, [{"content": "a"}], "alice", "triage", "sess1")
    expected = {
        "space_id": SPACE,
        "items": [{"content": "a"}],
        "user_id": "alice",
        "agent_id": "triage",
        "session_id": "sess1",
    }
    sent = json.loads(route.calls.last.request.content)
    assert sent == expected
    assert list(sent) == list(expected)


@respx.mock
@pytest.mark.anyio
async def test_async_batch_routes_too(client):
    route = respx.post(f"{BASE}/v1/record/batch").mock(
        return_value=httpx.Response(202, json=BATCH_RESULT)
    )
    result = await client.async_record_batch(items=[{"content": "a"}], route="auto")
    body = json.loads(route.calls.last.request.content)
    assert body["route"] == "auto" and "space_id" not in body
    assert result["job_id"] == "j1"
    await client.aclose()


# ── read: `max_targets`, the cap on how many spaces a routed read may search ──
#
# Different from the write side's `max_targets`, which is capped at 1: a memory
# is filed in one space, but a question can be answered out of several.


@respx.mock
def test_routed_retrieve_sends_max_targets(client):
    route = respx.post(f"{BASE}/v1/retrieve").mock(
        return_value=httpx.Response(200, json=ROUTED_READ)
    )
    client.retrieve(query="was acme charged?", route="auto", max_targets=3)
    body = json.loads(route.calls.last.request.content)
    assert body["max_targets"] == 3
    assert body["route"] == "auto"


@respx.mock
def test_an_unset_max_targets_is_omitted(client):
    """Omitted, not defaulted client-side, so the organization's setting applies
    and raising the server default needs no new SDK release."""
    route = respx.post(f"{BASE}/v1/retrieve").mock(
        return_value=httpx.Response(200, json=ROUTED_READ)
    )
    client.retrieve(query="q", route="auto")
    assert "max_targets" not in json.loads(route.calls.last.request.content)


@respx.mock
def test_max_targets_on_an_addressed_retrieve_fails_locally(client):
    """It means nothing without routing, and a caller who passed it there thinks
    they widened a search that never routed. Refused before the round trip."""
    route = respx.post(f"{BASE}/v1/retrieve").mock(
        return_value=httpx.Response(200, json=ROUTED_READ)
    )
    with pytest.raises(AnonaError) as excinfo:
        client.retrieve("billing", "invoices?", max_targets=2)
    assert excinfo.value.code == "route_conflict"
    assert not route.calls, "the request went out anyway"


@respx.mock
def test_async_retrieve_takes_max_targets_too(client):
    """The sync and async twins share `_search_body`; they had drifted once
    before, and a routing field is exactly what one copy would have missed."""
    import asyncio

    route = respx.post(f"{BASE}/v1/retrieve").mock(
        return_value=httpx.Response(200, json=ROUTED_READ)
    )

    async def main():
        await client.async_retrieve(query="q", route="auto", max_targets=2)

    asyncio.run(main())
    assert json.loads(route.calls.last.request.content)["max_targets"] == 2
