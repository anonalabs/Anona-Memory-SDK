# Anona Memory SDK

Official SDKs for [Anona Memory](https://memory.anonalabs.com) — managed AI memory for intelligent agents. Record, retrieve, and reason over memories per user/space via a simple client, or auto-inject memory into LiteLLM calls with one line.

- **Python** — this repository root. Documented below.
- **TypeScript** — [`typescript/`](typescript/). Zero dependencies, runs on Node 18+, Bun, Deno, Cloudflare Workers and the browser, with adapters for the Vercel AI SDK and the OpenAI Agents SDK. See [`typescript/README.md`](typescript/README.md).

```typescript
import { Anona } from "@anona-labs/memory";

const anona = new Anona({ apiKey: process.env.ANONA_API_KEY! });
await anona.record({ spaceId: "support", content: "Alice prefers email" });
const hits = await anona.retrieve({ spaceId: "support", query: "how to contact Alice" });
```

```bash
npm install @anona-labs/memory
```

## Install

```bash
pip install anona
```

With the LiteLLM integration (or `mcp` for the MCP server):

```bash
pip install "anona[litellm]"
```

## Quickstart

```python
from anona import AnonaClient

# base_url defaults to https://api.anonalabs.com — pass it only to override.
client = AnonaClient(api_key="anona_live_...")

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

### Async ingestion (don't block on a write)

Recording runs fact extraction, so a normal `record()` takes a moment. In a
chat loop or any latency-sensitive path, queue the write with `background=True`
and poll the returned job instead:

```python
import time

job = client.record(
    space_id="space_123",
    content="User prefers dark mode.",
    background=True,        # returns a job_id, doesn't wait
)

while True:
    status = client.get_job(space_id="space_123", job_id=job["job_id"])
    if status["status"] in ("completed", "failed", "cancelled", "not_found"):
        break
    time.sleep(2)

# Backfill many memories at once (always queued, up to 100 per call):
batch = client.record_batch(
    space_id="space_123",
    items=[
        {"content": "User is on the Pro plan."},
        {"content": "Signed up in 2024.", "timestamp": "2024-03-01T00:00:00Z"},
    ],
)
print(batch["accepted"], "queued as job", batch["job_id"])
```

Async variants (`async_record`, `async_retrieve`, `async_reason`) are available on the same client, or use it as a context manager:

```python
async with AnonaClient(api_key="...") as client:
    await client.async_record(space_id="space_123", content="...")
```

## API

### `AnonaClient(api_key, base_url="https://api.anonalabs.com")`

- `record(space_id, content, metadata=None, background=False, timestamp=None) -> dict` — store a memory; `background=True` queues it and returns a `job_id`; `timestamp` (ISO 8601) is when the *event* happened, for importing history
- `record_batch(space_id, items) -> dict` — bulk-ingest up to 100 items (always queued); returns a `job_id`
- `get_job(space_id, job_id) -> dict` — poll a queued job's status (free); `status` is one of pending / processing / completed / failed / cancelled / not_found
- `retrieve(space_id, query, limit=10, as_of=None, query_timestamp=None) -> list[dict]` — see [Time travel](#time-travel) for the two temporal arguments
- `reason(space_id, query) -> str | None`
- `list_spaces() -> list[dict]`
- `upload_file(space_id, file, *, filename=None, strategy=None, tags=None) -> dict` — upload a file (path / bytes / file-like) so retrieval can draw on its content; ingested asynchronously, returns `job_ids`. PDF, DOCX, PPTX, XLSX, images (OCR), HTML, TXT/MD, CSV, audio. Files over 25 MB are rejected client-side.
- `list_documents(space_id, limit=100, offset=0) -> list[dict]`
- `delete_document(space_id, document_id) -> None` — remove a document and the memories extracted from it
- `get_graph(space_id, limit=500, min_count=1) -> dict` — entity relationship graph (nodes + co-occurrence edges)
- `list_entities(space_id, limit=100, offset=0) -> list[dict]`
- `get_entity(space_id, entity_id) -> dict` — one entity + its observations
- `async_record(...)`, `async_record_batch(...)`, `async_get_job(...)`, `async_retrieve(...)`, `async_reason(...)`, `async_list_spaces(...)`, `async_upload_file(...)`, `async_list_documents(...)`, `async_delete_document(...)`, `async_get_graph(...)`, `async_list_entities(...)`, `async_get_entity(...)` — async equivalents
- `close()` / `aclose()` — release underlying HTTP clients

Errors raise `AnonaError(status_code, detail)`.

## Time travel

Three arguments, and the distinction between them is the whole point: one is
about **when something happened**, the other two are about **what you knew and
when**.

```python
# Importing history: date the memory to the event, not to the import run.
client.record(
    space_id="support",
    content="Renewed the enterprise contract",
    timestamp="2025-06-14T10:00:00Z",
)

# What did the space know in June, ignoring everything learned since?
client.retrieve(space_id="support", query="contract status", as_of="2026-06-01T00:00:00Z")

# Same corpus, but score recency and resolve "last June" against a past instant.
client.retrieve(space_id="support", query="what changed last June",
                query_timestamp="2026-01-01T00:00:00Z")
```

- **`timestamp`** on `record` is when the *event* occurred. It feeds recency
  ranking and is returned as `occurred_start` / `occurred_end`. It does not
  change when the memory was *recorded*, so it has no effect on `as_of`.
- **`as_of`** on `retrieve` is a hard cutoff: only memories **recorded** at or
  before that instant come back. A backdated import is recorded today no matter
  what `timestamp` it carries.
- **`query_timestamp`** on `retrieve` only re-ranks. It moves the "now" that
  recency and relative dates are measured against, and never removes a result.
  Reach for `as_of` when you need the cutoff actually enforced.

## Framework adapters

Anona plugs into the Python agent frameworks through optional extras. Every
adapter handles recall and storage for you, and scopes memories per end user.

| Framework | Install | Import |
| --- | --- | --- |
| LangChain / LangGraph | `pip install 'anona[langchain]'` | `anona.integrations.langchain` |
| CrewAI | `pip install 'anona[crewai]'` | `anona.integrations.crewai` |
| LlamaIndex | `pip install 'anona[llamaindex]'` | `anona.integrations.llamaindex` |
| Google ADK | `pip install 'anona[adk]'` | `anona.integrations.google_adk` |
| Microsoft Agent Framework | `pip install 'anona[msagent]'` | `anona.integrations.ms_agent` |
| AWS Strands | `pip install 'anona[strands]'` | `anona.integrations.strands` |

All six are built on one `MemoryBridge`, which owns scope resolution and the
failure contract:

```python
from anona.integrations import MemoryBridge
from anona.integrations.langchain import AnonaMemory

bridge = MemoryBridge(
    api_key="anona_live_...",
    space_id="my-space",
    user_id="customer-42",   # optional scope: this user's memories only
)

agent = create_agent(model="gpt-4o-mini", middleware=[AnonaMemory(bridge=bridge)])
```

**Memory failures never raise into your agent.** A failed recall or store is
logged and the agent runs on without memory, rather than taking your
application down.

Each adapter's own module docstring documents its scoping, failure behaviour
and per-call cost. Full docs: https://docs.anonalabs.com/integrations/langchain

Runnable end-to-end scripts for all six live in [`examples/`](examples/).

## LiteLLM integration

Auto-inject relevant memories into every `litellm.completion()` call, and auto-store the resulting Q&A pair:

```python
from anona.integrations.litellm import AnonaMemory

mem = AnonaMemory(
    api_key="anona_live_...",
    space_id="space_123",
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
      "args": [
        "--from",
        "anona[mcp]",
        "anona-mcp"
      ],
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
- one of `langchain`, `crewai`, `llama-index-core`, `google-adk`,
  `agent-framework-core`, `strands-agents` (optional, only for the matching
  framework adapter — see the extras above for the verified version floors)

## License

MIT
