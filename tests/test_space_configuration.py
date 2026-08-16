"""Per-space configuration — extraction settings, chat defaults and webhooks.

All three shipped in the API long before this package could reach them, so a
caller following the docs had to drop down to raw HTTP. The tests pin the wire
shapes, since that is what a missed port gets wrong — and they cover the async
client too, because the package keeps a separate body per method and updating
only one of the pair is how this drifts.
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


# ── Extraction settings ───────────────────────────────────────────────────────


@respx.mock
def test_get_extraction_settings_reads_the_space(client):
    route = respx.get(f"{BASE}/v1/spaces/{SPACE}/extraction-settings").mock(
        return_value=httpx.Response(
            200,
            json={
                "space_id": SPACE,
                "mode": "concise",
                "guidance": None,
                "custom_prompt": None,
            },
        )
    )
    settings = client.get_extraction_settings(SPACE)
    assert route.called
    assert settings["mode"] == "concise"


@respx.mock
def test_set_extraction_settings_sends_every_field(client):
    """The endpoint replaces the record, so an omitted argument has to travel as
    an explicit null — otherwise the stored value quietly survives."""
    route = respx.put(f"{BASE}/v1/spaces/{SPACE}/extraction-settings").mock(
        return_value=httpx.Response(200, json={"space_id": SPACE})
    )
    client.set_extraction_settings(SPACE, guidance="Capture service names.")
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "mode": None,
        "guidance": "Capture service names.",
        "custom_prompt": None,
    }


@respx.mock
def test_reset_extraction_settings_deletes(client):
    route = respx.delete(f"{BASE}/v1/spaces/{SPACE}/extraction-settings").mock(
        return_value=httpx.Response(204)
    )
    assert client.reset_extraction_settings(SPACE) is None
    assert route.called


@respx.mock
def test_extraction_settings_encode_the_space_id(client):
    """A space's id is its name, so it can legally contain a space."""
    route = respx.get(f"{BASE}/v1/spaces/my%20space/extraction-settings").mock(
        return_value=httpx.Response(200, json={"space_id": "my space"})
    )
    client.get_extraction_settings("my space")
    assert route.called


@pytest.mark.anyio
async def test_async_extraction_settings_match_the_sync_calls():
    async with AnonaClient(api_key=KEY, base_url=BASE) as c:
        with respx.mock:
            get = respx.get(f"{BASE}/v1/spaces/{SPACE}/extraction-settings").mock(
                return_value=httpx.Response(200, json={"space_id": SPACE})
            )
            put = respx.put(f"{BASE}/v1/spaces/{SPACE}/extraction-settings").mock(
                return_value=httpx.Response(200, json={"space_id": SPACE})
            )
            delete = respx.delete(f"{BASE}/v1/spaces/{SPACE}/extraction-settings").mock(
                return_value=httpx.Response(204)
            )
            await c.async_get_extraction_settings(SPACE)
            await c.async_set_extraction_settings(SPACE, mode="verbose")
            await c.async_reset_extraction_settings(SPACE)

            assert get.called and delete.called
            assert json.loads(put.calls.last.request.content)["mode"] == "verbose"


# ── Chat defaults ─────────────────────────────────────────────────────────────


@respx.mock
def test_get_chat_settings_reads_the_space(client):
    route = respx.get(f"{BASE}/v1/spaces/{SPACE}/chat-settings").mock(
        return_value=httpx.Response(200, json={"space_id": SPACE, "memory_limit": 5})
    )
    assert client.get_chat_settings(SPACE)["memory_limit"] == 5
    assert route.called


@respx.mock
def test_set_chat_settings_sends_every_field(client):
    route = respx.put(f"{BASE}/v1/spaces/{SPACE}/chat-settings").mock(
        return_value=httpx.Response(200, json={"space_id": SPACE})
    )
    client.set_chat_settings(SPACE, memory_limit=3)
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "memory_limit": 3,
        "memory_token_budget": None,
        "auto_record": None,
        "memory": None,
    }


@respx.mock
def test_reset_chat_settings_deletes(client):
    route = respx.delete(f"{BASE}/v1/spaces/{SPACE}/chat-settings").mock(
        return_value=httpx.Response(204)
    )
    assert client.reset_chat_settings(SPACE) is None
    assert route.called


@pytest.mark.anyio
async def test_async_chat_settings_match_the_sync_calls():
    async with AnonaClient(api_key=KEY, base_url=BASE) as c:
        with respx.mock:
            get = respx.get(f"{BASE}/v1/spaces/{SPACE}/chat-settings").mock(
                return_value=httpx.Response(200, json={"space_id": SPACE})
            )
            put = respx.put(f"{BASE}/v1/spaces/{SPACE}/chat-settings").mock(
                return_value=httpx.Response(200, json={"space_id": SPACE})
            )
            delete = respx.delete(f"{BASE}/v1/spaces/{SPACE}/chat-settings").mock(
                return_value=httpx.Response(204)
            )
            await c.async_get_chat_settings(SPACE)
            await c.async_set_chat_settings(SPACE, auto_record=False)
            await c.async_reset_chat_settings(SPACE)

            assert get.called and delete.called
            assert json.loads(put.calls.last.request.content)["auto_record"] is False


# ── Webhooks ──────────────────────────────────────────────────────────────────


@respx.mock
def test_create_webhook_posts_url_and_events(client):
    route = respx.post(f"{BASE}/v1/spaces/{SPACE}/webhooks").mock(
        return_value=httpx.Response(201, json={"id": "wh_1", "secret": "whsec_x"})
    )
    created = client.create_webhook(
        SPACE, url="https://example.com/hook", event_types=["memory.created"]
    )
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "url": "https://example.com/hook",
        "event_types": ["memory.created"],
        "enabled": True,
    }
    assert created["secret"] == "whsec_x"


@respx.mock
def test_create_webhook_defaults_to_the_memory_created_event(client):
    route = respx.post(f"{BASE}/v1/spaces/{SPACE}/webhooks").mock(
        return_value=httpx.Response(201, json={"id": "wh_1"})
    )
    client.create_webhook(SPACE, url="https://example.com/hook")
    body = json.loads(route.calls.last.request.content)
    assert body["event_types"] == ["memory.created"]


@respx.mock
def test_list_webhooks_returns_the_items(client):
    respx.get(f"{BASE}/v1/spaces/{SPACE}/webhooks").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "wh_1"}]})
    )
    assert client.list_webhooks(SPACE) == [{"id": "wh_1"}]


@respx.mock
def test_list_webhooks_is_empty_when_the_response_carries_none(client):
    respx.get(f"{BASE}/v1/spaces/{SPACE}/webhooks").mock(
        return_value=httpx.Response(200, json={})
    )
    assert client.list_webhooks(SPACE) == []


@respx.mock
def test_update_webhook_sends_only_what_changed(client):
    """A patch, unlike the settings replacements above: the API changes what it
    is sent and leaves the rest alone, so unset arguments must be omitted."""
    route = respx.patch(f"{BASE}/v1/spaces/{SPACE}/webhooks/wh_1").mock(
        return_value=httpx.Response(200, json={"id": "wh_1", "enabled": False})
    )
    client.update_webhook(SPACE, "wh_1", enabled=False)
    assert json.loads(route.calls.last.request.content) == {"enabled": False}


@respx.mock
def test_delete_webhook_deletes(client):
    route = respx.delete(f"{BASE}/v1/spaces/{SPACE}/webhooks/wh_1").mock(
        return_value=httpx.Response(204)
    )
    assert client.delete_webhook(SPACE, "wh_1") is None
    assert route.called


@respx.mock
def test_webhook_ids_are_encoded_in_the_path(client):
    route = respx.delete(f"{BASE}/v1/spaces/my%20space/webhooks/wh%201").mock(
        return_value=httpx.Response(204)
    )
    client.delete_webhook("my space", "wh 1")
    assert route.called


@respx.mock
def test_list_webhook_deliveries_passes_paging(client):
    route = respx.get(f"{BASE}/v1/spaces/{SPACE}/webhooks/wh_1/deliveries").mock(
        return_value=httpx.Response(200, json={"items": [], "next_cursor": None})
    )
    client.list_webhook_deliveries(SPACE, "wh_1", limit=10, cursor="abc")
    url = str(route.calls.last.request.url)
    assert "limit=10" in url and "cursor=abc" in url


@respx.mock
def test_list_webhook_deliveries_omits_an_absent_cursor(client):
    route = respx.get(f"{BASE}/v1/spaces/{SPACE}/webhooks/wh_1/deliveries").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    client.list_webhook_deliveries(SPACE, "wh_1")
    assert "cursor" not in str(route.calls.last.request.url)


@pytest.mark.anyio
async def test_async_webhooks_match_the_sync_calls():
    async with AnonaClient(api_key=KEY, base_url=BASE) as c:
        with respx.mock:
            post = respx.post(f"{BASE}/v1/spaces/{SPACE}/webhooks").mock(
                return_value=httpx.Response(201, json={"id": "wh_1"})
            )
            listing = respx.get(f"{BASE}/v1/spaces/{SPACE}/webhooks").mock(
                return_value=httpx.Response(200, json={"items": []})
            )
            patch = respx.patch(f"{BASE}/v1/spaces/{SPACE}/webhooks/wh_1").mock(
                return_value=httpx.Response(200, json={"id": "wh_1"})
            )
            delete = respx.delete(f"{BASE}/v1/spaces/{SPACE}/webhooks/wh_1").mock(
                return_value=httpx.Response(204)
            )
            deliveries = respx.get(
                f"{BASE}/v1/spaces/{SPACE}/webhooks/wh_1/deliveries"
            ).mock(return_value=httpx.Response(200, json={"items": []}))

            await c.async_create_webhook(SPACE, url="https://example.com/hook")
            assert await c.async_list_webhooks(SPACE) == []
            await c.async_update_webhook(SPACE, "wh_1", enabled=True)
            await c.async_delete_webhook(SPACE, "wh_1")
            await c.async_list_webhook_deliveries(SPACE, "wh_1")

            assert post.called and listing.called and delete.called and deliveries.called
            assert json.loads(patch.calls.last.request.content) == {"enabled": True}
