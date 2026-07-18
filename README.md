# Anona Memory SDK

Python SDK for [Anona Memory](https://anona.ai) — managed AI memory for intelligent agents. Record, retrieve, and reason over memories per user/space via a simple client, or auto-inject memory into LiteLLM calls with one line.

## Install

```bash
pip install anona
```

With LiteLLM integration:

```bash
pip install "anona[litellm]"
```

## Quickstart

```python
from anona import AnonaClient

client = AnonaClient(api_key="anona_live_...", base_url="https://api.anona.ai")

# Record a memory
client.record(space_id="space_123", content="User prefers dark mode.")

# Retrieve memories
results = client.retrieve(space_id="space_123", query="UI preferences", limit=5)
for r in results:
    print(r["relevance_score"], r["content"])

# Reason: a synthesized insight across memories
summary = client.reason(space_id="space_123", query="What do we know about this user?")
print(summary)

client.close()
```

Async variants (`async_record`, `async_retrieve`, `async_reason`) are available on the same client, or use it as a context manager:

```python
async with AnonaClient(api_key="...") as client:
    await client.async_record(space_id="space_123", content="...")
```

## API

### `AnonaClient(api_key, base_url="https://api.anona.ai")`

- `record(space_id, content, metadata=None) -> dict`
- `retrieve(space_id, query, limit=10) -> list[dict]`
- `reason(space_id, query) -> str | None`
- `list_spaces() -> list[dict]`
- `async_record(...)`, `async_retrieve(...)`, `async_reason(...)`, `async_list_spaces(...)` — async equivalents
- `close()` / `aclose()` — release underlying HTTP clients

Errors raise `AnonaError(status_code, detail)`.

## LiteLLM integration

Auto-inject relevant memories into every `litellm.completion()` call, and auto-store the resulting Q&A pair:

```python
from anona.integrations.litellm import AnonaMemory

mem = AnonaMemory(
    api_key="anona_live_...",
    space_id="space_123",
    base_url="https://api.anona.ai",
    recall_limit=5,       # how many memories to retrieve per call
    inject_mode="system", # "system" or "user"
    store_after=True,     # auto-store the exchange after each call
)
mem.enable()

# All subsequent litellm.completion() calls now auto-recall + auto-store.
import litellm
litellm.completion(model="gpt-4o", messages=[{"role": "user", "content": "..."}])
```

## MCP server

The SDK ships an [MCP](https://modelcontextprotocol.io) server so any MCP client
— Claude Desktop, Claude Code, Cursor — can read and write Anona memory as native
tools: `record`, `retrieve`, `list_spaces`, and `reason`.

Install the extra:

```bash
pip install "anona[mcp]"
```

**Claude Desktop / Cursor** — add to `claude_desktop_config.json` (or
`~/.cursor/mcp.json`), then restart:

```json
{
  "mcpServers": {
    "anona": {
      "command": "uvx",
      "args": ["--from", "anona[mcp]", "anona-mcp"],
      "env": {
        "ANONA_API_KEY": "anona_live_...",
        "ANONA_SPACE_ID": "space_123"
      }
    }
  }
}
```

**Claude Code** — one command:

```bash
claude mcp add anona \
  --env ANONA_API_KEY=anona_live_... \
  --env ANONA_SPACE_ID=space_123 \
  -- uvx --from "anona[mcp]" anona-mcp
```

`ANONA_SPACE_ID` sets the default space so you can just say "remember this"
without naming one; override it per call with the `space_id` argument. The key
is personal — the server only reaches spaces you are a member of.

## Requirements

- Python >= 3.10
- `httpx >= 0.24`
- `litellm >= 1.0` (optional, only for the LiteLLM integration)
- `mcp >= 1.2` (optional, only for the MCP server)

## License

MIT
