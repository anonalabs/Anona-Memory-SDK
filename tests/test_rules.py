"""Rules a space must follow.

A rule is not a setting a caller can afford to get subtly wrong: it applies to
every answer the space gives and overrides what the memories say. So the wire
shape is pinned here — the field names, the defaults a create fills in, and the
fact that an edit is a PATCH carrying only what was passed. Two of those have a
silent failure mode. A create that omitted `is_active` would store a rule the
caller asked to keep switched off, and the cap counts active ones; a PATCH that
nulled the fields it was not given would blank the text of something enforced on
every answer.

The async client is covered too, because the package keeps a separate body per
method and updating only one of the pair is how this drifts.
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

RULE = {
    "id": "rl_1",
    "space_id": SPACE,
    "name": "Never call revenue committed",
    "content": "Never describe revenue as committed without a signed document.",
    "priority": 100,
    "is_active": True,
    "tags": [],
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def client():
    c = AnonaClient(api_key=KEY, base_url=BASE)
    yield c
    c.close()


@respx.mock
def test_list_rules_reads_the_space_and_returns_the_items(client):
    route = respx.get(f"{BASE}/v1/spaces/{SPACE}/rules").mock(
        return_value=httpx.Response(200, json={"items": [RULE], "total": 1})
    )
    rules = client.list_rules(SPACE)
    assert route.called
    assert [r["id"] for r in rules] == ["rl_1"]


@respx.mock
def test_create_rule_sends_every_field_with_its_defaults(client):
    """A create that dropped `is_active` would store a rule as active that the
    caller asked to keep switched off."""
    route = respx.post(f"{BASE}/v1/spaces/{SPACE}/rules").mock(
        return_value=httpx.Response(201, json=RULE)
    )
    client.create_rule(
        SPACE,
        name="No committed revenue",
        content="Never describe revenue as committed.",
    )
    assert json.loads(route.calls.last.request.content) == {
        "name": "No committed revenue",
        "content": "Never describe revenue as committed.",
        "priority": 0,
        "is_active": True,
        "tags": [],
    }


@respx.mock
def test_create_rule_can_store_one_switched_off(client):
    route = respx.post(f"{BASE}/v1/spaces/{SPACE}/rules").mock(
        return_value=httpx.Response(201, json=RULE)
    )
    client.create_rule(
        SPACE,
        name="Seasonal",
        content="Quote Q4 pricing.",
        priority=500,
        is_active=False,
        tags=["pricing"],
    )
    body = json.loads(route.calls.last.request.content)
    assert body["is_active"] is False
    assert body["priority"] == 500
    assert body["tags"] == ["pricing"]


@respx.mock
def test_update_rule_sends_only_what_changed(client):
    """A partial edit, unlike the settings PUTs: nulling the fields the caller
    did not pass would blank the text of a rule enforced on every answer."""
    route = respx.patch(f"{BASE}/v1/spaces/{SPACE}/rules/rl_1").mock(
        return_value=httpx.Response(200, json=RULE)
    )
    client.update_rule(SPACE, "rl_1", is_active=False)
    assert json.loads(route.calls.last.request.content) == {"is_active": False}


@respx.mock
def test_update_rule_keeps_priority_zero(client):
    """0 is the lowest priority, not an absent one. A truthiness test here drops
    the only field the caller sent, and an empty patch is refused."""
    route = respx.patch(f"{BASE}/v1/spaces/{SPACE}/rules/rl_1").mock(
        return_value=httpx.Response(200, json=RULE)
    )
    client.update_rule(SPACE, "rl_1", priority=0)
    assert json.loads(route.calls.last.request.content) == {"priority": 0}


@respx.mock
def test_delete_rule_deletes(client):
    route = respx.delete(f"{BASE}/v1/spaces/{SPACE}/rules/rl_1").mock(
        return_value=httpx.Response(204)
    )
    assert client.delete_rule(SPACE, "rl_1") is None
    assert route.called


@respx.mock
def test_rule_ids_are_encoded_in_the_path(client):
    """A space's id is its name, so it can legally contain a space."""
    route = respx.delete(f"{BASE}/v1/spaces/my%20space/rules/rl%201").mock(
        return_value=httpx.Response(204)
    )
    client.delete_rule("my space", "rl 1")
    assert route.called


@pytest.mark.anyio
async def test_async_rules_match_the_sync_calls():
    async with AnonaClient(api_key=KEY, base_url=BASE) as c:
        with respx.mock:
            listing = respx.get(f"{BASE}/v1/spaces/{SPACE}/rules").mock(
                return_value=httpx.Response(200, json={"items": [], "total": 0})
            )
            post = respx.post(f"{BASE}/v1/spaces/{SPACE}/rules").mock(
                return_value=httpx.Response(201, json=RULE)
            )
            patch = respx.patch(f"{BASE}/v1/spaces/{SPACE}/rules/rl_1").mock(
                return_value=httpx.Response(200, json=RULE)
            )
            delete = respx.delete(f"{BASE}/v1/spaces/{SPACE}/rules/rl_1").mock(
                return_value=httpx.Response(204)
            )

            assert await c.async_list_rules(SPACE) == []
            await c.async_create_rule(SPACE, name="n", content="c")
            await c.async_update_rule(SPACE, "rl_1", priority=10)
            await c.async_delete_rule(SPACE, "rl_1")

            assert listing.called and delete.called
            assert json.loads(post.calls.last.request.content) == {
                "name": "n",
                "content": "c",
                "priority": 0,
                "is_active": True,
                "tags": [],
            }
            assert json.loads(patch.calls.last.request.content) == {"priority": 10}
