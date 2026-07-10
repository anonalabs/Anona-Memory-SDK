"""
Anona Memory MCP server.

Exposes Anona memory as MCP tools so any MCP client (Claude Desktop, Claude Code,
Cursor) can store and recall memories natively.

Run:
    ANONA_API_KEY=anona_live_... uvx --from anona anona-mcp

Client config (e.g. claude_desktop_config.json):
    {
      "mcpServers": {
        "anona": {
          "command": "uvx",
          "args": ["--from", "anona", "anona-mcp"],
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

DEFAULT_BASE_URL = "https://api.anona.ai"
SPACE_ENV_VAR = "ANONA_SPACE_ID"

mcp = FastMCP("Anona Memory")


def _client() -> AnonaClient:
    api_key = os.environ.get("ANONA_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANONA_API_KEY is not set. Add it to the 'env' block of your MCP "
            "server config."
        )
    return AnonaClient(
        api_key=api_key,
        base_url=os.environ.get("ANONA_BASE_URL", DEFAULT_BASE_URL),
    )


def _resolve_space(space_id: str | None) -> str:
    resolved = space_id or os.environ.get(SPACE_ENV_VAR)
    if not resolved:
        raise ValueError(
            f"No space_id given and {SPACE_ENV_VAR} is not set. Call list_spaces "
            "to see the spaces you can use."
        )
    return resolved


def _format_error(exc: AnonaError) -> str:
    """Pull a human message out of the gateway's error envelope."""
    detail = exc.detail
    if isinstance(detail, dict):
        err = detail.get("error", detail)
        if isinstance(err, dict):
            return err.get("message") or str(err)
    return str(detail)


@mcp.tool()
def remember(content: str, space_id: str | None = None) -> str:
    """Store a fact in Anona memory so it can be recalled in later conversations.

    Args:
        content: The fact to remember, written as a complete sentence.
        space_id: Space to store it in. Defaults to the ANONA_SPACE_ID env var.
    """
    space = _resolve_space(space_id)
    with _client() as c:
        try:
            c.add_memory(space_id=space, content=content)
        except AnonaError as exc:
            return f"Failed to store memory: {_format_error(exc)}"
    return f"Stored in space '{space}'."


@mcp.tool()
def recall(query: str, space_id: str | None = None, limit: int = 5) -> str:
    """Search Anona memory for facts relevant to a query.

    Args:
        query: What to search for, in natural language.
        space_id: Space to search. Defaults to the ANONA_SPACE_ID env var.
        limit: Maximum number of memories to return.
    """
    space = _resolve_space(space_id)
    with _client() as c:
        try:
            results = c.search(space_id=space, query=query, limit=limit)
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
    with _client() as c:
        try:
            spaces = c.list_spaces()
        except AnonaError as exc:
            return f"Failed to list spaces: {_format_error(exc)}"

    if not spaces:
        return "No spaces found. Create one in the Anona dashboard."

    return "\n".join(
        f"- {s.get('name', '(unnamed)')}  [space_id: {s.get('space_id', '')}]"
        for s in spaces
    )


@mcp.tool()
def get_insights(query: str, space_id: str | None = None) -> str:
    """Get a synthesized summary of what Anona memory knows about a topic.

    Unlike recall, which returns individual memories, this returns a single
    narrative answer built from everything stored in the space.

    Args:
        query: The topic to summarize.
        space_id: Space to draw from. Defaults to the ANONA_SPACE_ID env var.
    """
    space = _resolve_space(space_id)
    with _client() as c:
        try:
            insights = c.insights(space_id=space, query=query)
        except AnonaError as exc:
            return f"Failed to get insights: {_format_error(exc)}"
    return insights or f"No insights available in space '{space}' for that query."


def main() -> None:
    """Console-script entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
