"""Compound tag filters — HTTP mocked, no live API.

`tags` is a flat list under one match mode. `tag_groups` is the boolean
expression that replaces it when a filter has clauses that combine differently,
or a clause that excludes. It shipped in the API and was documented long before
this package could send it, so a caller following the docs got a TypeError.

The expression travels unmodelled on purpose: the API owns every rule in it, so
the only thing this package can get wrong is dropping it, reshaping it, or
sending it on some methods and not others. All three are what these pin, and
the async twins are covered because each keeps a separate method body.
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

GROUPS = [
    {"or": [{"tags": ["project:alpha"]}, {"tags": ["project:beta"]}]},
    {"not": {"tags": ["status:archived"], "match": "any_strict"}},
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def client():
    c = AnonaClient(api_key=KEY, base_url=BASE)
    yield c
    c.close()


@respx.mock
def test_retrieve_sends_the_expression_verbatim(client):
    route = respx.post(f"{BASE}/v1/retrieve").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    client.retrieve(SPACE, "what is left to do", tag_groups=GROUPS)
    body = json.loads(route.calls.last.request.content)
    # Nested structure untouched: anything this package did to the shape could
    # only be wrong.
    assert body["tag_groups"] == GROUPS


@respx.mock
def test_retrieve_omits_the_key_when_no_filter_is_asked_for(client):
    """A key present as null would change the request body for every caller who
    never wanted a filter."""
    route = respx.post(f"{BASE}/v1/retrieve").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    client.retrieve(SPACE, "q", tags=["a"])
    body = json.loads(route.calls.last.request.content)
    assert "tag_groups" not in body


@respx.mock
def test_get_context_sends_the_expression(client):
    route = respx.post(f"{BASE}/v1/retrieve").mock(
        return_value=httpx.Response(200, json={"results": [], "context": ""})
    )
    client.get_context(SPACE, "q", tag_groups=GROUPS)
    assert json.loads(route.calls.last.request.content)["tag_groups"] == GROUPS


@respx.mock
def test_reason_sends_the_expression(client):
    """Worth more here than on retrieve: the predicate rides every iteration of
    the agent loop rather than one query, so it narrows what the agent is
    allowed to look at instead of filtering an answer after the fact."""
    route = respx.post(f"{BASE}/v1/reason").mock(
        return_value=httpx.Response(200, json={"insights": "ok"})
    )
    client.reason(SPACE, "q", tag_groups=GROUPS)
    assert json.loads(route.calls.last.request.content)["tag_groups"] == GROUPS


@respx.mock
def test_reason_still_sends_only_the_question_when_nothing_narrows_it(client):
    route = respx.post(f"{BASE}/v1/reason").mock(
        return_value=httpx.Response(200, json={"insights": "ok"})
    )
    client.reason(SPACE, "q")
    assert json.loads(route.calls.last.request.content) == {
        "space_id": SPACE,
        "query": "q",
    }


@pytest.mark.anyio
@respx.mock
async def test_async_retrieve_sends_the_expression(client):
    route = respx.post(f"{BASE}/v1/retrieve").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    await client.async_retrieve(SPACE, "q", tag_groups=GROUPS)
    assert json.loads(route.calls.last.request.content)["tag_groups"] == GROUPS
    await client.aclose()


@pytest.mark.anyio
@respx.mock
async def test_async_reason_sends_the_expression(client):
    """Separate method body from the sync twin, which is how an argument lands
    on one and not the other."""
    route = respx.post(f"{BASE}/v1/reason").mock(
        return_value=httpx.Response(200, json={"insights": "ok"})
    )
    await client.async_reason(SPACE, "q", tag_groups=GROUPS)
    assert json.loads(route.calls.last.request.content)["tag_groups"] == GROUPS
    await client.aclose()
