"""Per-user profiles — HTTP mocked, no live API.

`GET /v1/spaces/{space_id}/users/{user_id}/profile` and `POST .../ask` shipped
in the API and were documented before this package could reach them, so a
caller following the docs found no method to call. The tests pin the wire shape,
since that is what a missed port gets wrong, and they cover the async twins as
well: this client keeps two separate bodies per method, and a change applied to
only one of them is the usual way this breaks.
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
USER = "alice_123"

PROFILE = {
    "space_id": SPACE,
    "user_id": USER,
    "memory_count": 2,
    "first_seen": "2026-05-02T09:12:00Z",
    "last_active": "2026-08-15T18:40:11Z",
    "memories": [{"id": "m1", "text": "prefers email", "user_id": USER}],
}

ANSWER = {
    "space_id": SPACE,
    "user_id": USER,
    "insights": "Alice prefers email.",
    "usage": {"input_tokens": 210, "output_tokens": 42},
    "model": "us.amazon.nova-pro-v1:0",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def client():
    c = AnonaClient(api_key=KEY, base_url=BASE)
    yield c
    c.close()


# ── get_user_profile ──────────────────────────────────────────────────────────


@respx.mock
def test_get_user_profile_reads_the_user(client):
    route = respx.get(f"{BASE}/v1/spaces/{SPACE}/users/{USER}/profile").mock(
        return_value=httpx.Response(200, json=PROFILE)
    )
    profile = client.get_user_profile(SPACE, USER)
    assert route.called
    assert profile["memory_count"] == 2
    assert profile["last_active"] == "2026-08-15T18:40:11Z"
    assert profile["memories"][0]["id"] == "m1"


@respx.mock
def test_get_user_profile_sends_only_what_the_caller_passed(client):
    """Every query parameter has a server-side default, so sending an unasked-for
    one pins the caller to today's value of a default that is free to move."""
    route = respx.get(f"{BASE}/v1/spaces/{SPACE}/users/{USER}/profile").mock(
        return_value=httpx.Response(200, json=PROFILE)
    )
    client.get_user_profile(SPACE, USER)
    assert dict(route.calls.last.request.url.params) == {}


@respx.mock
def test_get_user_profile_forwards_every_documented_param(client):
    route = respx.get(f"{BASE}/v1/spaces/{SPACE}/users/{USER}/profile").mock(
        return_value=httpx.Response(200, json=PROFILE)
    )
    client.get_user_profile(
        SPACE,
        USER,
        limit=20,
        offset=40,
        memory_type="note",
        format="block",
        context_max_tokens=500,
    )
    assert dict(route.calls.last.request.url.params) == {
        "limit": "20",
        "offset": "40",
        "memory_type": "note",
        "format": "block",
        "context_max_tokens": "500",
    }


@respx.mock
def test_get_user_profile_still_sends_an_explicit_offset_of_zero(client):
    """0 is falsy and is also a legal explicit offset, so a truthiness filter
    would silently drop it."""
    route = respx.get(f"{BASE}/v1/spaces/{SPACE}/users/{USER}/profile").mock(
        return_value=httpx.Response(200, json=PROFILE)
    )
    client.get_user_profile(SPACE, USER, offset=0)
    assert dict(route.calls.last.request.url.params) == {"offset": "0"}


@respx.mock
def test_get_user_profile_encodes_both_path_segments(client):
    """A space's id is its name, and a user_id is whatever the caller scopes
    writes with — either can carry a character that is structural in a URL."""
    route = respx.get(
        f"{BASE}/v1/spaces/my%20space/users/user%2F42/profile"
    ).mock(return_value=httpx.Response(200, json=PROFILE))
    client.get_user_profile("my space", "user/42")
    assert route.called


@respx.mock
def test_an_unknown_user_is_not_an_error(client):
    """A user is a scope tag created by the first write naming it; there is no
    registry, so an unknown user is a 200 and must not read as a failure."""
    respx.get(f"{BASE}/v1/spaces/{SPACE}/users/nobody/profile").mock(
        return_value=httpx.Response(
            200,
            json={
                "space_id": SPACE,
                "user_id": "nobody",
                "memory_count": 0,
                "first_seen": None,
                "last_active": None,
                "memories": [],
            },
        )
    )
    profile = client.get_user_profile(SPACE, "nobody")
    assert profile["memory_count"] == 0
    assert profile["memories"] == []


# ── ask_about_user ────────────────────────────────────────────────────────────


@respx.mock
def test_ask_about_user_posts_the_query(client):
    route = respx.post(f"{BASE}/v1/spaces/{SPACE}/users/{USER}/ask").mock(
        return_value=httpx.Response(200, json=ANSWER)
    )
    answer = client.ask_about_user(SPACE, USER, "What does she prefer?")
    assert json.loads(route.calls.last.request.content) == {
        "query": "What does she prefer?"
    }
    assert answer["insights"] == "Alice prefers email."


@respx.mock
def test_ask_about_user_returns_the_model_that_answered(client):
    """The bill is reconciled against the model that ran, not the one asked for,
    so returning a bare string would discard the only way to explain it."""
    route = respx.post(f"{BASE}/v1/spaces/{SPACE}/users/{USER}/ask").mock(
        return_value=httpx.Response(200, json=ANSWER)
    )
    answer = client.ask_about_user(SPACE, USER, "q", model="balanced")
    assert json.loads(route.calls.last.request.content) == {
        "query": "q",
        "model": "balanced",
    }
    assert answer["model"] == "us.amazon.nova-pro-v1:0"
    assert answer["usage"]["input_tokens"] == 210


@respx.mock
def test_ask_about_user_omits_an_unset_model(client):
    """Omitted means "fall through to the space's reason default"; sending null
    is a different request from sending nothing."""
    route = respx.post(f"{BASE}/v1/spaces/{SPACE}/users/{USER}/ask").mock(
        return_value=httpx.Response(200, json=ANSWER)
    )
    client.ask_about_user(SPACE, USER, "q")
    assert "model" not in json.loads(route.calls.last.request.content)


# ── async twins ───────────────────────────────────────────────────────────────


@pytest.mark.anyio
@respx.mock
async def test_async_get_user_profile_matches_the_sync_call(client):
    route = respx.get(f"{BASE}/v1/spaces/{SPACE}/users/{USER}/profile").mock(
        return_value=httpx.Response(200, json=PROFILE)
    )
    profile = await client.async_get_user_profile(SPACE, USER, limit=20)
    assert dict(route.calls.last.request.url.params) == {"limit": "20"}
    assert profile["memory_count"] == 2
    await client.aclose()


@pytest.mark.anyio
@respx.mock
async def test_async_ask_about_user_matches_the_sync_call(client):
    route = respx.post(f"{BASE}/v1/spaces/{SPACE}/users/{USER}/ask").mock(
        return_value=httpx.Response(200, json=ANSWER)
    )
    answer = await client.async_ask_about_user(SPACE, USER, "q", model="fast")
    assert json.loads(route.calls.last.request.content) == {
        "query": "q",
        "model": "fast",
    }
    assert answer["model"] == "us.amazon.nova-pro-v1:0"
    await client.aclose()
