"""Unit tests for the Anona MCP server tools — no live gateway required."""
from __future__ import annotations

import json

import pytest
import httpx
import respx

pytest.importorskip("mcp", reason="MCP server requires the 'mcp' extra")

from anona.integrations import mcp as anona_mcp  # noqa: E402

BASE = "http://test.anona.local"
KEY = "anona_live_testkey"
SPACE = "space-1"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ANONA_API_KEY", KEY)
    monkeypatch.setenv("ANONA_BASE_URL", BASE)
    monkeypatch.delenv("ANONA_SPACE_ID", raising=False)


# ── tool registration ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_all_tools_registered():
    tools = await anona_mcp.mcp.list_tools()
    assert {t.name for t in tools} == {"record", "retrieve", "list_spaces", "reason"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── space resolution ──────────────────────────────────────────────────────────


def test_space_id_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("ANONA_SPACE_ID", "from-env")
    assert anona_mcp._resolve_space(None) == "from-env"


def test_explicit_space_id_wins(monkeypatch):
    monkeypatch.setenv("ANONA_SPACE_ID", "from-env")
    assert anona_mcp._resolve_space("explicit") == "explicit"


def test_missing_space_raises_actionable_error():
    with pytest.raises(ValueError, match="ANONA_SPACE_ID"):
        anona_mcp._resolve_space(None)


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("ANONA_API_KEY")
    with pytest.raises(RuntimeError, match="ANONA_API_KEY"):
        anona_mcp._client()


# ── record ──────────────────────────────────────────────────────────────────


@respx.mock
def test_record_posts_memory():
    route = respx.post(f"{BASE}/v1/record").mock(
        return_value=httpx.Response(201, json={"memory_id": "m1"})
    )
    out = anona_mcp.record("Srujan uses zsh.", space_id=SPACE)

    assert route.called
    body = json.loads(route.calls[0].request.read())
    assert body["content"] == "Srujan uses zsh."
    assert body["space_id"] == SPACE
    assert SPACE in out


@respx.mock
def test_record_surfaces_gateway_error_message():
    respx.post(f"{BASE}/v1/record").mock(
        return_value=httpx.Response(
            403,
            json={"error": {"code": "space_access_denied", "message": "No access."}},
        )
    )
    out = anona_mcp.record("x", space_id=SPACE)
    assert "Failed to store memory" in out
    assert "No access." in out


# ── retrieve ────────────────────────────────────────────────────────────────────


@respx.mock
def test_retrieve_formats_results_with_scores():
    respx.post(f"{BASE}/v1/retrieve").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"content": "Name is Srujan.", "relevance_score": 0.91},
                    {"content": "Builds Anona."},
                ]
            },
        )
    )
    out = anona_mcp.retrieve("who am i", space_id=SPACE)

    assert "1. Name is Srujan." in out
    assert "relevance 0.91" in out
    assert "2. Builds Anona." in out


@respx.mock
def test_retrieve_empty_results():
    respx.post(f"{BASE}/v1/retrieve").mock(return_value=httpx.Response(200, json={"results": []}))
    assert "No memories found" in anona_mcp.retrieve("nothing", space_id=SPACE)


@respx.mock
def test_retrieve_passes_limit():
    route = respx.post(f"{BASE}/v1/retrieve").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    anona_mcp.retrieve("q", space_id=SPACE, limit=3)
    assert json.loads(route.calls[0].request.read())["limit"] == 3


# ── list_spaces ───────────────────────────────────────────────────────────────


@respx.mock
def test_list_spaces_renders_ids():
    respx.get(f"{BASE}/v1/spaces/").mock(
        return_value=httpx.Response(
            200, json={"spaces": [{"name": "Demo", "space_id": "demo-1"}], "total": 1}
        )
    )
    out = anona_mcp.list_spaces()
    assert "Demo" in out
    assert "demo-1" in out


@respx.mock
def test_list_spaces_empty():
    respx.get(f"{BASE}/v1/spaces/").mock(
        return_value=httpx.Response(200, json={"spaces": [], "total": 0})
    )
    assert "No spaces found" in anona_mcp.list_spaces()


# ── reason ──────────────────────────────────────────────────────────────


@respx.mock
def test_reason_returns_text():
    respx.post(f"{BASE}/v1/reason").mock(
        return_value=httpx.Response(200, json={"insights": "Uses FastAPI."})
    )
    assert anona_mcp.reason("stack", space_id=SPACE) == "Uses FastAPI."


@respx.mock
def test_reason_surfaces_error_message():
    respx.post(f"{BASE}/v1/reason").mock(
        return_value=httpx.Response(
            403, json={"error": {"code": "space_access_denied", "message": "No access."}}
        )
    )
    out = anona_mcp.reason("stack", space_id=SPACE)
    assert "Failed to get insights" in out
    assert "No access." in out


# ── auth header ───────────────────────────────────────────────────────────────


@respx.mock
def test_api_key_sent_as_bearer():
    route = respx.get(f"{BASE}/v1/spaces/").mock(
        return_value=httpx.Response(200, json={"spaces": [], "total": 0})
    )
    anona_mcp.list_spaces()
    assert route.calls[0].request.headers["authorization"] == f"Bearer {KEY}"
