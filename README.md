# Anona Memory SDK

Official SDKs for [Anona Memory](https://memory.anonalabs.com) — managed AI memory for intelligent agents. Record, retrieve, and reason over memories per user/space via a simple client, or auto-inject memory into LiteLLM calls with one line.

**Documentation** — [Python SDK](https://docs.anonalabs.com/sdk/python) ·
[TypeScript SDK](https://docs.anonalabs.com/sdk/javascript) ·
[Quickstart](https://docs.anonalabs.com/quickstart) ·
[API reference](https://docs.anonalabs.com/api-reference/overview) ·
[MCP server](https://docs.anonalabs.com/mcp-integration) ·
[Framework integrations](https://docs.anonalabs.com/integrations/langchain)

- **Python** — this repository root, published as [`anona`](https://pypi.org/project/anona/) on PyPI. Documented below and at [docs.anonalabs.com/sdk/python](https://docs.anonalabs.com/sdk/python).
- **TypeScript** — [`typescript/`](typescript/), published as [`@anona-labs/memory`](https://www.npmjs.com/package/@anona-labs/memory) on npm. Zero dependencies, runs on Node 18+, Bun, Deno, Cloudflare Workers and the browser, with adapters for the Vercel AI SDK and the OpenAI Agents SDK. See [`typescript/README.md`](typescript/README.md) and [docs.anonalabs.com/sdk/javascript](https://docs.anonalabs.com/sdk/javascript).

```typescript
import { Anona } from "@anona-labs/memory";

const anona = new Anona({ apiKey: process.env.ANONA_API_KEY! });
await anona.record({ spaceId: "support", content: "Alice prefers email" });
const hits = await anona.retrieve({ spaceId: "support", query: "how to contact Alice" });
```

```bash
npm install @anona-labs/memory
```

## Agent Skills

Two installable [Agent Skills](https://docs.anonalabs.com/integrations/skills)
that teach a coding agent to use Anona without being told to — recall before a
task, record after it — in Claude Code, Codex, Hermes, OpenCode or Cursor.

```bash
curl -fsSL https://raw.githubusercontent.com/anonalabs/Anona-Memory-SDK/main/skills/install.sh | bash
```

Or `npx skills add anonalabs/Anona-Memory-SDK`, or the Claude Code plugin
marketplace (`/plugin marketplace add anonalabs/Anona-Memory-SDK`). Source and
options: [`skills/`](skills/).

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

### `AnonaClient(api_key: str, base_url: str = "https://api.anonalabs.com", timeout: float = 120.0, max_retries: int = 2, retry_max_wait: float = 60.0)`

- `record(space_id: str, content: str, metadata: dict | None = None, tags: list[str] | None = None, background: bool = False, user_id: str | None = None, agent_id: str | None = None, session_id: str | None = None, timestamp: str | None = None, context: str | None = None) -> dict` — store a memory; `context` is extra framing stored alongside the content (where it came from, who said it) without becoming the memory's text; `background=True` queues it and returns a `job_id`; `timestamp` (ISO 8601) is when the *event* happened, for importing history; `user_id` / `agent_id` / `session_id` scope it inside the space
- `record_batch(space_id, items) -> dict` — bulk-ingest up to 100 items (always queued); returns a `job_id`
- `get_job(space_id, job_id) -> dict` — poll a queued job's status (free); `status` is one of pending / processing / completed / failed / cancelled / not_found
- `retrieve(space_id: str, query: str, limit: int = 10, mode: str = "accurate", user_id: str | None = None, agent_id: str | None = None, session_id: str | None = None, as_of: str | None = None, query_timestamp: str | None = None, occurred_after: str | None = None, occurred_before: str | None = None, top_k: int | None = None, memory_type: list[str] | None = None, tags: list[str] | None = None, tags_match: str | None = None, tag_groups: list[dict] | None = None, prefer_observations: bool | None = None, min_score: float | None = None, member_id: str | None = None) -> list[dict]` — search; `tags` / `tags_match` filter on the tags a memory was written with and `tag_groups` does the same as a boolean expression (see [Compound tag filters](#compound-tag-filters)), `memory_type` narrows the kind, `min_score` floors relevance, `top_k` bounds the candidates considered before ranking, `prefer_observations=False` also returns the raw evidence behind a synthesised memory, `member_id="me"` returns only what you wrote in a shared space; see [Time travel](#time-travel) for the temporal arguments
- `get_context(space_id: str, query: str, limit: int = 10, max_tokens: int | None = None, block_order: str | None = None, …) -> str` — the relevant memories as one prompt-ready string, ready to paste into a system prompt; takes the same filters as `retrieve`, and `block_order="stable"` keeps the block byte-identical between turns so a provider's prompt cache can hit it
- `retrieve_receipt(space_id: str, query: str, receipt_detail: str = "basic", …) -> RetrieveWithReceipt` — `retrieve`, plus a receipt explaining that search; returns `.memories` and `.receipt_id`, and that id is what `get_receipt` and `explain` take
- `get_receipt(request_id: str) -> dict` — the manifest for one earlier search: what was returned, what was considered, what was cut
- `explain(request_id: str, memory_id: str) -> dict` — account for one specific memory against an earlier search
- `reason(space_id, query) -> str | None`
- `list_spaces() -> list[dict]`
- `create_space(name: str, description: str | None = None) -> dict` — create a space; a space's id *is* its name
- `delete_space(space_id: str) -> None` — delete a space and every memory in it; irreversible
- `get_user_profile(space_id, user_id, *, limit=None, offset=None, memory_type=None, format=None, context_max_tokens=None) -> dict` — everything the space has learned about one end user; see [User profiles](#user-profiles)
- `ask_about_user(space_id, user_id, query, *, model=None) -> dict` — one synthesised answer, drawn from that user's memories only
- `upload_file(space_id, file, *, filename=None, strategy=None, tags=None) -> dict` — upload a file (path / bytes / file-like) so retrieval can draw on its content; ingested asynchronously, returns `job_ids`. PDF, DOCX, PPTX, XLSX, images (OCR), HTML, TXT/MD, CSV, audio. Files over 25 MB are rejected client-side.
- `list_documents(space_id, limit=100, offset=0) -> list[dict]`
- `delete_document(space_id, document_id) -> None` — remove a document and the memories extracted from it
- `get_graph(space_id, limit=500, min_count=1) -> dict` — entity relationship graph (nodes + co-occurrence edges)
- `list_entities(space_id, limit=100, offset=0) -> list[dict]`
- `get_entity(space_id, entity_id) -> dict` — one entity + its observations
- `get_extraction_settings(space_id) -> dict` / `set_extraction_settings(space_id, mode=None, guidance=None, custom_prompt=None, labels=None, free_form_entities=None) -> dict` / `reset_extraction_settings(space_id) -> None` — steer what a write keeps; see [Extraction settings](#extraction-settings)
- `get_chat_settings(space_id) -> dict` / `set_chat_settings(space_id, memory_limit=None, memory_token_budget=None, auto_record=None, memory=None) -> dict` / `reset_chat_settings(space_id) -> None` — per-space defaults for the drop-in proxy endpoints
- `create_webhook(space_id, url, event_types=None, enabled=True) -> dict` — the response carries `secret`, returned only on create
- `list_webhooks(space_id) -> list[dict]`, `update_webhook(space_id, webhook_id, url=None, event_types=None, enabled=None) -> dict`, `delete_webhook(space_id, webhook_id) -> None`
- `list_webhook_deliveries(space_id, webhook_id, limit=50, cursor=None) -> dict` — recent attempts, for debugging a receiver
- `get_space(space_id: str) -> dict` — One space by id
- `list_memories(space_id: str, *, limit: int = 50, offset: int = 0, q: str | None = None, memory_type: str | None = None, state: str | None = None, prefer_observations: bool | None = None, user_id: str | None = None, agent_id: str | None = None, session_id: str | None = None, member_id: str | None = None) -> dict` — Page through the memories stored in a space
- `get_memory_history(space_id: str, memory_id: str) -> dict` — Every recorded version of one memory, newest first
- `update_memory(space_id: str, memory_id: str, *, text: str | None = None, context: str | None = None, occurred_start: str | None = None, occurred_end: str | None = None, memory_type: str | None = None, entities: list[str] | None = None, state: str | None = None, reason: str | None = None) -> dict` — Correct one memory in place
- `get_usage() -> dict` — Credits and rate limit for the organization this key belongs to
- `get_document(space_id: str, document_id: str) -> dict` — One document by id, with its source and memory count
- `cancel_job(space_id: str, job_id: str) -> dict` — Cancel a queued ingestion job
- `get_reason_settings(space_id: str) -> dict` — The model this space uses for `reason`, or null for the default
- `set_reason_settings(space_id: str, *, model: str | None = None) -> dict` — Pin the model `reason` uses for this space
- `reset_reason_settings(space_id: str) -> None` — Clear the space's reason-model override. Owner-only
- `list_memory_models(space_id: str, *, limit: int = 50, offset: int = 0, tags: list[str] | None = None) -> dict` — The memory models defined on a space, with their current content
- `create_memory_model(space_id: str, *, name: str, query: str, model_id: str | None = None, tags: list[str] | None = None, max_tokens: int | None = None, trigger: dict | None = None) -> dict` — Define a memory model
- `get_memory_model(space_id: str, model_id: str) -> dict` — One memory model, with its current content
- `update_memory_model(space_id: str, model_id: str, *, name: str | None = None, query: str | None = None, tags: list[str] | None = None, max_tokens: int | None = None, trigger: dict | None = None) -> dict` — Edit a memory model's definition
- `delete_memory_model(space_id: str, model_id: str) -> None` — Delete a memory model and its content
- `refresh_memory_model(space_id: str, model_id: str) -> dict` — Re-answer a memory model from the space's current memories
- `clear_memory_model(space_id: str, model_id: str) -> dict` — Wipe a memory model's content, keeping its definition
- `get_memory_model_history(space_id: str, model_id: str) -> dict` — Earlier versions of a memory model's content
- `get_space_profile(space_id: str) -> dict` — A space's profile — its mission and disposition
- `list_catalog_models() -> dict` — Every LLM this deployment will answer with, and what each costs
- Every method has an `async_` twin (`async_record`, `async_retrieve`, …) taking the same arguments
- `close()` / `aclose()` — release underlying HTTP clients

`Anona` is an alias for `AnonaClient`, matching the TypeScript package's class name.

### Errors

Errors raise `AnonaError(status_code, detail)`. The exception also carries `code`
(the API's stable machine-readable handle, e.g. `space_not_found`), `request_id`
(what support needs to find the call in the logs) and, on a 429, `retry_after`
in seconds. A 503 with no `request_id` may be a Cloudflare-mangled 502 or 504:
the edge strips the body of those two statuses, so the API rewrites them to 503
before they leave. Report such a failure with a timestamp rather than treating
it as a malformed response.

### Retries

A 429 is retried for every method, `record` included: the limiter refuses the
request before anything runs, so re-sending cannot double-write. A 5xx is
retried only for idempotent methods (GET, PUT, DELETE), because a 5xx on a write
may arrive after the work landed, and replaying it would store the memory
twice. Uploads are never retried. `max_retries=0` turns it off.

**A 429 waits as long as the server says to.** The rate-limit window is a whole
minute, so a client backing off on its own schedule tops out in the low seconds,
spends its retries landing on the same full bucket, and fails anyway. The client
reads `Retry-After`, falling back to the `window_seconds` the rate-limit body
carries, and waits that out, capped at `retry_max_wait` (60s by default). A 5xx
with no hint backs off briefly with jitter. `AnonaError.retry_after` carries the
same number if you would rather schedule it yourself.

### Staying inside the rate limit

Every metered response reports the budget it left you, so a bulk job can pace
itself instead of discovering the ceiling by hitting it. `client.rate_limit` is
updated in place on every call (`limit`, `remaining`, `window_seconds`, `credits_remaining`, `retry_after`):

```python
client.retrieve("support", "billing")
rl = client.rate_limit
if rl.remaining is not None and rl.remaining < 5:
    time.sleep(rl.window_seconds or 60)
```

Fields are `None` until a metered call has been made. The unmetered routes
(spaces, settings, webhooks) report no budget and leave the last reading in
place rather than blanking it.

## Compound tag filters

`tags` is a flat list under a single match mode, so it can say "any of these" or
"all of these" and nothing else. `tag_groups` takes a boolean expression
instead, for the moment a filter has two clauses that combine differently, or a
clause that excludes.

```python
client.retrieve(
    "support",
    "what is left to do",
    tag_groups=[
        {"or": [{"tags": ["project:alpha"]}, {"tags": ["project:beta"]}]},
        {"not": {"tags": ["status:archived"]}},
    ],
)
```

Groups in the list are AND-ed. Each is either a leaf `{"tags": [...], "match":
...}` or one of `{"and": [...]}`, `{"or": [...]}`, `{"not": {...}}`, nested as
deep as you need. `match` on a leaf takes the same values as `tags_match` and
defaults to `any_strict`.

Three things worth knowing:

- **The non-strict modes treat an untagged memory as matching**, so a leaf
  written with `any` or `all` widens an expression rather than narrowing it, and
  both are refused inside a `not` for that reason. Negating "matches, including
  untagged" would exclude every untagged memory in the space.
- **The filter is compiled into the search**, not applied to its output, so an
  excluded memory never competes for a slot. You get the best *n* matches that
  passed the filter rather than whatever survived it.
- **It composes rather than replaces.** `tags`, `user_id`, `agent_id` and
  `session_id` are all AND-ed onto your expression, so a scoped query stays
  scoped when you add a filter.

Limits: 25 leaves and 5 levels of nesting per request. `reason` accepts the same
argument, where it narrows what the reasoning agent may look at.

A filter is only as good as the tags a memory carries. If you are tagging by
hand on every write, [labels](#labels-tag-your-memories-as-they-are-written) can
have extraction do it for you.

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

# Everything that HAPPENED in June 2025, however recently it was imported.
client.retrieve(space_id="support", query="contract status",
                occurred_after="2025-06-01T00:00:00Z",
                occurred_before="2025-06-30T23:59:59Z")
```

- **`timestamp`** on `record` is when the *event* occurred. It feeds recency
  ranking, comes back on a result as `timestamp`, and is what the event-time
  window below matches against. It does not change when the memory was
  *recorded*, so it has no effect on `as_of`. It is **not** copied into
  `occurred_start` / `occurred_end`: those are filled only from a date found in
  the memory's own text, so a memory whose text names no date has both of them
  null while carrying a perfectly good `timestamp`.
- **`as_of`** on `retrieve` is a hard cutoff: only memories **recorded** at or
  before that instant come back. A backdated import is recorded today no matter
  what `timestamp` it carries.
- **`query_timestamp`** on `retrieve` only re-ranks. It moves the "now" that
  recency and relative dates are measured against, and never removes a result.
  Reach for `as_of` when you need the cutoff actually enforced.
- **`occurred_after` / `occurred_before`** on `retrieve` bound when the thing
  *happened*, which is the one question `as_of` cannot answer: a year of
  history imported this morning has one record time and twelve months of event
  time. Either bound alone is an open-ended window, and the test is an overlap,
  so an event straddling an edge is inside.

## User profiles

If you scope writes with `user_id`, a space accumulates a per-user history with
nowhere to address it directly. These two read it back, instead of you
reconstructing it from a wide-open search.

```python
profile = client.get_user_profile("support", "alice_123")
print(profile["memory_count"], profile["last_active"])

# Prompt-ready instead of a list
block = client.get_user_profile(
    "support", "alice_123", format="block", context_max_tokens=500
)
messages = [{"role": "system", "content": block["context"]}, ...]

answer = client.ask_about_user(
    "support", "alice_123", "How does she prefer to be contacted?"
)
print(answer["insights"], answer["model"])
```

`user_id` has to be the **same value** your writes are scoped with — a typo on
either side looks like an empty profile, not an error.

Three things worth knowing:

- **An unknown user is not a 404.** A `user_id` is a scope tag created by the
  first write naming it, not a resource you register, so there is no valid set
  for a typo to fall outside of. A user nobody has recorded under comes back
  with `memory_count` of `0` and an empty `memories` list. An unknown *space* is
  still a 404.
- **`memory_count` can go down.** The default view collapses layers: several raw
  facts become one synthesised note, and the note is what gets counted. A
  profile read during an import can genuinely go 115 → 67 → 15 while the corpus
  behind it grows the whole time. Read it as how many distinct things are
  currently known about this user, never as an ingestion counter, and never as
  the basis for a progress bar.
- **`ask_about_user` returns the whole response**, not the answer string
  `reason` returns, because `model` reports which LLM actually answered. That is
  the rate the call was charged at, and it differs from what you asked for
  whenever you asked for nothing.

## Extraction settings

Recording a memory is not storage. Your text goes through one pass that decides
which facts are worth keeping and how they are phrased, and only what survives
is stored — so a detail dropped there is not ranked low later, it is not there
at all. These settings point that pass at your domain.

```python
client.set_extraction_settings(
    "engineering",
    guidance=(
        "Engineering log. Always capture service names, metric values with "
        "units, and the named owner. Treat incidents as dated events. "
        "Skip standup small talk."
    ),
)
```

- **`guidance`** is *added* to the standard rules and applies in every mode.
  Reach for it first — naming your vocabulary and the fields that always matter
  is what turns a good guess into a reliable one. Max 4,000 characters.
- **`mode`** is `concise` (the default), `verbose` (keep every specific),
  `verbatim` (store the text as written, derive only the metadata around it), or
  `custom`.
- **`custom_prompt`** *replaces* the standard rules, and only applies while
  `mode` is `"custom"`. Max 8,000 characters.
- **`labels`** is different in kind. The three above steer what a write *keeps*;
  labels ask the same pass to *classify* what it kept, along dimensions you
  define. See [Labels](#labels-tag-your-memories-as-they-are-written).

Two things worth knowing. `set_extraction_settings` **replaces** the record, so
anything you leave out is cleared. And settings apply to writes made after the
call — memories already stored are never re-extracted, so changing these is safe
and never rewrites history. To see the effect, save, record a representative
piece of text, and read the memory back.

Unhelpful guidance produces no error; extraction simply keeps different things.

### Labels: tag your memories as they are written

Filtering recall is only as good as the tags a memory carries, and by default
every one of them has to be supplied by hand on the write that created it.
A **label taxonomy** hands that job to extraction: name a dimension once, and
every memory is classified along it as it is written.

```python
client.set_extraction_settings(
    "engineering",
    labels=[{
        "key": "name",
        "type": "multi-text",
        "tag": True,
        "description": (
            "Every name the subject of this memory is known by, including "
            "abbreviations, acronyms and short forms. Write them lowercase, "
            "words separated by single spaces, with no punctuation."
        ),
    }],
)
```

A group with `tag: True` is written onto the memory as the tag
`"<key>:<value>"`, so `retrieve` can filter on it through `tag_groups`. A memory
about Kubernetes then carries `name:kubernetes`, `name:k8s` and `name:kube`, and
any of the three finds it:

```python
client.retrieve(
    "engineering",
    "what did we decide about k8s",
    tag_groups=[{"tags": ["name:k8s"], "match": "any_strict"}],
)
```

There are four group types, in two pairs. `value` and `multi-values` pick from a
`values` list you supply, one or several. `text` and `multi-text` take whatever
the memory itself supplies, one value or all of them, for a vocabulary you
cannot enumerate in advance. `multi-text` is the one identity needs: a thing
rarely has a single surface form, and `text` would keep only the first.

Because tags are matched as exact strings, describe the format you want in the
group's `description` and normalise your query the same way.

A few things are refused rather than stored, because each would look saved and
do nothing: an enumerated group with no `values`; a `text`/`multi-text` group
*with* `values`; two groups sharing a `key`; a key that is not already in tag
form (lowercase letters, digits, `-`, `_`); and `free_form_entities=False` with
no labels, which would keep no entities at all rather than only labelled ones.
Twelve groups maximum, since the taxonomy rides the extraction prompt on every
write.

Labels apply to writes made after you save them, so turning a taxonomy on never
retags your history.

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

## Documentation

Full guides and the complete API reference live at
[docs.anonalabs.com](https://docs.anonalabs.com):

| Page | What it covers |
| --- | --- |
| [Python SDK](https://docs.anonalabs.com/sdk/python) | Every `AnonaClient` method, with examples |
| [TypeScript SDK](https://docs.anonalabs.com/sdk/javascript) | The `Anona` client and its adapters |
| [Quickstart](https://docs.anonalabs.com/quickstart) | First key to first memory |
| [Concepts](https://docs.anonalabs.com/concepts) | Spaces, scoping, observations, credits |
| [API reference](https://docs.anonalabs.com/api-reference/overview) | The REST API these SDKs wrap |
| [MCP server](https://docs.anonalabs.com/mcp-integration) | Local stdio and remote transports |
| [Framework integrations](https://docs.anonalabs.com/integrations/langchain) | LangChain, CrewAI, LlamaIndex, Google ADK and more |

## License

MIT
