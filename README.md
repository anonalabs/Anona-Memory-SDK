# Anona Memory SDK

Python SDK for [Anona Memory](https://anona.ai) — managed AI memory for intelligent agents. Store, search, and synthesize memories per user/space via a simple client, or auto-inject memory into LiteLLM calls with one line.

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

# Store a memory
client.add_memory(space_id="space_123", content="User prefers dark mode.")

# Search memories
results = client.search(space_id="space_123", query="UI preferences", limit=5)
for r in results:
    print(r["relevance_score"], r["content"])

# Ask for a synthesized insight across memories
summary = client.insights(space_id="space_123", query="What do we know about this user?")
print(summary)

client.close()
```

Async variants (`async_add_memory`, `async_search`, `async_insights`) are available on the same client, or use it as a context manager:

```python
async with AnonaClient(api_key="...") as client:
    await client.async_add_memory(space_id="space_123", content="...")
```

## API

### `AnonaClient(api_key, base_url="https://api.anona.ai")`

- `add_memory(space_id, content, metadata=None) -> dict`
- `search(space_id, query, limit=10) -> list[dict]`
- `insights(space_id, query) -> str | None`
- `async_add_memory(...)`, `async_search(...)`, `async_insights(...)` — async equivalents
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

## Requirements

- Python >= 3.9
- `httpx >= 0.24`
- `litellm >= 1.0` (optional, only for the LiteLLM integration)

## License

MIT
