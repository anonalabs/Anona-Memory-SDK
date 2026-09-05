"""
Anona Memory MCP server.

Exposes Anona memory as MCP tools so any MCP client (Claude Desktop, Claude Code,
Cursor) can store and retrieve memories natively.

Run:
    ANONA_API_KEY=anona_live_... uvx --from 'anona[mcp]' anona-mcp

Client config (e.g. claude_desktop_config.json):
    {
      "mcpServers": {
        "anona": {
          "command": "uvx",
          "args": ["--from", "anona[mcp]", "anona-mcp"],
          "env": {"ANONA_API_KEY": "anona_live_..."}
        }
      }
    }
"""
from __future__ import annotations

import os
import sys

from anona import AnonaClient, AnonaError

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "The Anona MCP server requires the 'mcp' extra:\n"
        "    pip install 'anona[mcp]'\n"
    )
    raise

# REST base for this stdio server, which authenticates with an API key.
# Distinct from the *remote* MCP endpoint (https://memory.anonalabs.com/mcp),
# which speaks OAuth 2.1 and must stay on that host to keep its issuer valid.
DEFAULT_BASE_URL = "https://api.anonalabs.com"
SPACE_ENV_VAR = "ANONA_SPACE_ID"

mcp = FastMCP("Anona Memory")

# What record reports when the engine accepted the write but extraction produced
# no facts — the same signal the remote MCP server surfaces. The API returns
# success even when nothing was stored, and the handler used to answer "Stored" for all of them,
# so ~44% of short, context-free writes were silently misreported as stored.
_NOTHING_EXTRACTED = (
    "Nothing was stored in space '{space_id}': no facts could be extracted from "
    "that content. Short, context-free statements often extract to nothing — "
    "re-record it with a sentence of context (what the thing is, or why it "
    "matters), which usually fixes it."
)

# One reused client for the whole stdio process, keyed on (api_key, base_url).
# A fresh AnonaClient per tool call opened a new TLS connection pool each time;
# stdio serves many calls from one long-lived process, so the pool is reused.
_client_singleton: AnonaClient | None = None
_client_key: tuple[str, str] | None = None


def _client() -> AnonaClient:
    global _client_singleton, _client_key
    api_key = os.environ.get("ANONA_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANONA_API_KEY is not set. Add it to the 'env' block of your MCP "
            "server config."
        )
    base_url = os.environ.get("ANONA_BASE_URL", DEFAULT_BASE_URL)
    key = (api_key, base_url)
    if _client_singleton is None or _client_key != key:
        if _client_singleton is not None:
            _client_singleton.close()
        _client_singleton = AnonaClient(api_key=api_key, base_url=base_url)
        _client_key = key
    return _client_singleton


def _stored_something(result: dict) -> bool:
    """Whether a record actually persisted a memory, read off the API response.

    ``memory_ids`` is the full set the write created; ``memory_id`` is the
    singular fallback. A queued/async write (``job_id`` present, or a pending
    status) has not extracted yet, so it must not be reported as empty either.
    """
    if not isinstance(result, dict):
        return True
    if result.get("memory_ids") or result.get("memory_id"):
        return True
    if result.get("job_id") or result.get("status") in (
        "processing",
        "pending",
        "queued",
    ):
        return True
    return False


def _resolve_space(space_id: str | None) -> str:
    resolved = space_id or os.environ.get(SPACE_ENV_VAR)
    if not resolved:
        raise ValueError(
            f"No space_id given and {SPACE_ENV_VAR} is not set. Call list_spaces "
            "to see the spaces you can use."
        )
    return resolved


def _format_error(exc: AnonaError) -> str:
    """Pull a human message out of the API's error envelope."""
    detail = exc.detail
    if isinstance(detail, dict):
        err = detail.get("error", detail)
        if isinstance(err, dict):
            return err.get("message") or str(err)
    return str(detail)


@mcp.tool()
def record(
    content: str,
    space_id: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
) -> str:
    """Store a fact in Anona memory so it can be retrieved in later conversations.

    Args:
        content: The fact to record, written as a complete sentence.
        space_id: Space to store it in. Defaults to the ANONA_SPACE_ID env var.
        user_id: Optional end user this memory belongs to. A scoped retrieve
            only sees memories written under the same user.
        agent_id: Optional agent that owns this memory.
        session_id: Optional conversation or run this memory came from.
    """
    space = _resolve_space(space_id)
    try:
        result = _client().record(
            space_id=space,
            content=content,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
        )
    except AnonaError as exc:
        return f"Failed to store memory: {_format_error(exc)}"
    # Report what actually happened. The engine acknowledges a write even when
    # extraction produced no memory, so a bare "Stored" would be a lie ~44% of
    # the time on short, context-free content.
    if not _stored_something(result):
        return _NOTHING_EXTRACTED.format(space_id=space)
    return f"Stored in space '{space}'."


@mcp.tool()
def retrieve(
    query: str,
    space_id: str | None = None,
    limit: int = 5,
    user_id: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
) -> str:
    """Search Anona memory for facts relevant to a query.

    Args:
        query: What to search for, in natural language.
        space_id: Space to search. Defaults to the ANONA_SPACE_ID env var.
        limit: Maximum number of memories to return.
        user_id: Restrict the search to memories written under this end user.
        agent_id: Restrict the search to memories written under this agent.
        session_id: Restrict the search to memories from this conversation/run.
    """
    space = _resolve_space(space_id)
    try:
        results = _client().retrieve(
            space_id=space,
            query=query,
            limit=limit,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
        )
    except AnonaError as exc:
        return f"Search failed: {_format_error(exc)}"

    if not results:
        return f"No memories found in space '{space}' for that query."

    lines = []
    for i, m in enumerate(results, 1):
        score = m.get("relevance_score")
        suffix = f"  (relevance {score:.2f})" if isinstance(score, (int, float)) else ""
        lines.append(f"{i}. {m.get('content', '')}{suffix}")
    return "\n".join(lines)


@mcp.tool()
def list_spaces() -> str:
    """List the Anona memory spaces this API key can access."""
    try:
        spaces = _client().list_spaces()
    except AnonaError as exc:
        return f"Failed to list spaces: {_format_error(exc)}"

    if not spaces:
        return "No spaces found. Create one in the Anona dashboard."

    return "\n".join(
        f"- {s.get('name', '(unnamed)')}  [space_id: {s.get('space_id', '')}]"
        for s in spaces
    )


@mcp.tool()
def reason(
    query: str,
    space_id: str | None = None,
    model: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
) -> str:
    """Get a synthesized summary of what Anona memory knows about a topic.

    Unlike retrieve, which returns individual memories, this returns a single
    narrative answer built from everything stored in the space.

    Args:
        query: The topic to summarize.
        space_id: Space to draw from. Defaults to the ANONA_SPACE_ID env var.
        model: Which LLM answers — a tier name ('fast', 'balanced') or a model
            id. Omit to use the space's default.
        user_id: Narrow the synthesis to memories written under this end user.
        agent_id: Narrow the synthesis to memories written under this agent.
        session_id: Narrow the synthesis to memories from this conversation/run.
    """
    space = _resolve_space(space_id)
    try:
        insights = _client().reason(
            space_id=space,
            query=query,
            model=model,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
        )
    except AnonaError as exc:
        return f"Failed to get insights: {_format_error(exc)}"
    return insights or f"No insights available in space '{space}' for that query."


def main() -> None:
    """Console-script entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
