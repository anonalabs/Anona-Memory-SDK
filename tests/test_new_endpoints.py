"""Request shapes for the endpoints the client did not reach before.

The parity test at the bottom is the important one: this client carries a sync
and an async copy of every method, and the copies have drifted before — an
argument added to `retrieve` that never reached `async_retrieve`, and so on.
"""
from __future__ import annotations

import asyncio
import inspect
import json

import httpx
import pytest

from anona.client import AnonaClient

BASE = "http://test.anona.local"
KEY = "anona_live_testkey"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Recorder:
    """Captures every request and answers 200 (204 on a DELETE)."""

    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode() or "null"
        self.calls.append((request.method, str(request.url), json.loads(body)))
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"ok": True})

    @property
    def last(self) -> tuple[str, str, object]:
        return self.calls[-1]


@pytest.fixture
def rec():
    return Recorder()


@pytest.fixture
def client(rec):
    c = AnonaClient(api_key=KEY, base_url=BASE)
    c._client = httpx.Client(
        transport=httpx.MockTransport(rec.handler),
        headers={"Authorization": f"Bearer {KEY}"},
    )
    yield c
    c.close()


# ── Memories ──────────────────────────────────────────────────────────────────


def test_list_memories_paths_and_params(client, rec):
    client.list_memories(
        "s1",
        limit=5,
        offset=10,
        prefer_observations=False,
        member_id="me",
        state="invalidated",
        memory_type="world",
    )
    method, url, _ = rec.last
    assert method == "GET"
    assert "/v1/spaces/s1/memories" in url
    assert "limit=5" in url and "offset=10" in url
    # False has to survive: it is the whole reason to pass the flag.
    assert "prefer_observations=false" in url.lower()
    assert "member_id=me" in url
    assert "state=invalidated" in url
    # The API's query name is `type`, not `memory_type`.
    assert "type=world" in url


def test_get_memory_history_path(client, rec):
    client.get_memory_history("s1", "mem-1")
    method, url, _ = rec.last
    assert method == "GET"
    assert url.endswith("/v1/spaces/s1/memories/mem-1/history")


def test_update_memory_sends_only_what_was_passed(client, rec):
    client.update_memory("s1", "mem-1", text="corrected", state="invalidated", reason="typo")
    method, url, body = rec.last
    assert method == "PATCH"
    assert url.endswith("/v1/spaces/s1/memories/mem-1")
    assert body == {"text": "corrected", "state": "invalidated", "reason": "typo"}


def test_cancel_job_and_usage_paths(client, rec):
    client.cancel_job("s1", "job-1")
    assert rec.last[0] == "DELETE"
    assert rec.last[1].endswith("/v1/spaces/s1/jobs/job-1")

    client.get_usage()
    assert rec.last[1].endswith("/v1/usage/me")


# ── Spaces / documents ────────────────────────────────────────────────────────


def test_get_space_and_get_document_paths(client, rec):
    client.get_space("s1")
    assert rec.last[0] == "GET" and rec.last[1].endswith("/v1/spaces/s1")

    client.get_document("s1", "doc-1")
    assert rec.last[1].endswith("/v1/spaces/s1/documents/doc-1")


def test_qualified_shared_space_id_is_encoded_into_one_segment(client, rec):
    # A share is addressed `owner:name`, and the colon must stay inside the
    # segment or the path stops matching the route.
    client.get_space("acme:notes")
    assert rec.last[1].endswith("/v1/spaces/acme%3Anotes")


# ── Reason settings ───────────────────────────────────────────────────────────


def test_reason_settings_get_set_reset(client, rec):
    client.get_reason_settings("s1")
    assert rec.last[0] == "GET" and rec.last[1].endswith("/v1/spaces/s1/reason-settings")

    client.set_reason_settings("s1", model="balanced")
    assert rec.last[0] == "PUT" and rec.last[2] == {"model": "balanced"}

    # Clearing the override is an explicit null: PUT is a full replace.
    client.set_reason_settings("s1")
    assert rec.last[2] == {"model": None}

    client.reset_reason_settings("s1")
    assert rec.last[0] == "DELETE"


# ── Memory models / profile / catalog ─────────────────────────────────────────


def test_memory_model_lifecycle_paths(client, rec):
    client.list_memory_models("s1", limit=5)
    assert rec.last[0] == "GET" and "/v1/spaces/s1/models" in rec.last[1]

    client.create_memory_model("s1", name="n", query="q")
    assert rec.last[0] == "POST" and rec.last[2] == {"name": "n", "query": "q"}

    client.get_memory_model("s1", "m1")
    assert rec.last[1].endswith("/v1/spaces/s1/models/m1")

    client.update_memory_model("s1", "m1", name="n2")
    assert rec.last[0] == "PATCH" and rec.last[2] == {"name": "n2"}

    client.refresh_memory_model("s1", "m1")
    assert rec.last[0] == "POST" and rec.last[1].endswith("/models/m1/refresh")

    client.clear_memory_model("s1", "m1")
    assert rec.last[1].endswith("/models/m1/clear")

    client.get_memory_model_history("s1", "m1")
    assert rec.last[1].endswith("/models/m1/history")

    client.delete_memory_model("s1", "m1")
    assert rec.last[0] == "DELETE"


def test_space_profile_and_model_catalog_paths(client, rec):
    client.get_space_profile("s1")
    assert rec.last[1].endswith("/v1/spaces/s1/profile")

    # /v1/models is the LLM catalog; /v1/spaces/{id}/models is memory models.
    client.list_catalog_models()
    assert rec.last[1].endswith("/v1/models")


# ── Search arguments that had no way through before ───────────────────────────


def test_retrieve_sends_the_full_filter_set(client, rec):
    client.retrieve(
        space_id="s1",
        query="q",
        top_k=50,
        memory_type=["world"],
        tags=["a"],
        tags_match="all_strict",
        prefer_observations=False,
        min_score=0.3,
        member_id="me",
    )
    _, url, body = rec.last
    assert url.endswith("/v1/retrieve")
    assert body["top_k"] == 50
    assert body["memory_type"] == ["world"]
    assert body["tags"] == ["a"] and body["tags_match"] == "all_strict"
    assert body["prefer_observations"] is False
    assert body["min_score"] == 0.3
    assert body["member_id"] == "me"


def test_unfiltered_retrieve_stays_byte_identical(client, rec):
    # The no-performance-regression guarantee: an unscoped, unfiltered search
    # must build the payload it always did.
    client.retrieve(space_id="s1", query="q")
    assert rec.last[2] == {"space_id": "s1", "query": "q", "limit": 10, "mode": "accurate"}


def test_record_sends_context(client, rec):
    client.record(space_id="s1", content="hi", context="from a support ticket")
    assert rec.last[2]["context"] == "from a support ticket"


def test_get_context_sends_block_order_and_budget(client, rec):
    client.get_context("s1", "q", max_tokens=500, block_order="stable", tags=["a"])
    _, url, body = rec.last
    assert url.endswith("/v1/retrieve")
    assert body["format"] == "block"
    assert body["context_max_tokens"] == 500
    assert body["block_order"] == "stable"
    assert body["tags"] == ["a"]


def test_retrieve_receipt_carries_the_filters_too(client, rec):
    client.retrieve_receipt(space_id="s1", query="q", tags=["a"], min_score=0.5)
    body = rec.last[2]
    assert body["receipt"] is True
    assert body["tags"] == ["a"] and body["min_score"] == 0.5


# ── Sync/async parity ─────────────────────────────────────────────────────────


def test_every_public_method_has_both_a_sync_and_an_async_form():
    names = [n for n, _ in inspect.getmembers(AnonaClient, inspect.isfunction) if not n.startswith("_")]
    sync = {n for n in names if not n.startswith("async_")} - {"close", "aclose"}
    asyn = {n[len("async_") :] for n in names if n.startswith("async_")}
    assert sync == asyn, sorted(sync ^ asyn)
    assert len(sync) > 45


def test_async_forms_send_the_same_request_as_the_sync_ones(rec):
    client = AnonaClient(api_key=KEY, base_url=BASE)
    client._async_client = httpx.AsyncClient(
        transport=httpx.MockTransport(rec.handler),
        headers={"Authorization": f"Bearer {KEY}"},
    )

    async def main():
        await client.async_list_memories("s1", prefer_observations=False)
        await client.async_update_memory("s1", "m1", text="t")
        await client.async_get_usage()
        await client.async_list_catalog_models()
        await client.async_retrieve(space_id="s1", query="q", tags=["a"])
        await client.aclose()

    asyncio.run(main())

    assert "prefer_observations=false" in rec.calls[0][1].lower()
    assert rec.calls[1][0] == "PATCH" and rec.calls[1][2] == {"text": "t"}
    assert rec.calls[2][1].endswith("/v1/usage/me")
    assert rec.calls[3][1].endswith("/v1/models")
    assert rec.calls[4][2]["tags"] == ["a"]
