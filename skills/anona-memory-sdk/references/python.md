# Python client

`pip install anona`. Requires Python 3.10+, depends only on `httpx`.

```python
from anona import AnonaClient, AnonaError

client = AnonaClient(
    api_key="anona_live_...",              # or ANONA_API_KEY in the environment
    base_url="https://api.anonalabs.com",  # default
    timeout=120.0,                          # default; a loaded reason() runs 70-115s
    max_retries=2,                          # 429 always, 5xx on idempotent calls only
    retry_max_wait=60.0,                    # cap on one wait, as long as a rate-limit window
)
```

After any call, `client.rate_limit` holds what the last response reported:
remaining credits and the rate-limit window. Read it to pace yourself instead of
discovering the ceiling by hitting it.

Every method below has an `async_` twin with the same arguments:
`async_record`, `async_retrieve`, `async_reason`, and so on. Close with
`client.close()` or `await client.aclose()`; both are context managers.

## Memories

| Method | Signature |
| --- | --- |
| `record` | `(space_id, content, metadata=None, tags=None, background=False, user_id=None, agent_id=None, session_id=None, timestamp=None)` |
| `record_batch` | `(space_id, items)` - up to 100 dicts with `content` plus optional `context`, `timestamp`, `metadata`, `tags`. Always queued. |
| `retrieve` | `(space_id, query, limit=10, mode="accurate", user_id=None, agent_id=None, session_id=None, as_of=None, query_timestamp=None, occurred_after=None, occurred_before=None)` |
| `get_context` | `(space_id, query, ...)` - the same search, rendered as one prompt-ready block instead of a list. |
| `reason` | `(space_id, query, user_id=None, agent_id=None, session_id=None, model=None)` |
| `list_memories` | `(space_id, ...)` - browse rather than search. |
| `get_memory_history` | `(space_id, memory_id)` |
| `update_memory` | `(space_id, memory_id, ...)` |
| `delete_memory` | `(space_id, memory_id)` |
| `get_job` / `cancel_job` | `(space_id, job_id)` - for background writes. |

`record` returns `{"memory_id": ...}`, or `{"job_id": ..., "status": "processing"}`
when `background=True`. `retrieve` returns the list of results directly, already
unwrapped from the response envelope. `reason` returns the answer string, or
`None`.

## Time

- `timestamp` on `record` is when the **event** happened, not when you recorded
  it. Use it when importing history.
- `as_of` on `retrieve` restricts to memories **recorded** at or before an
  instant, so you see what the space knew then.
- `query_timestamp` moves the "now" that recency and relative dates are measured
  against. It re-ranks and never removes.
- `occurred_after` / `occurred_before` bound when the thing **happened**. Either
  bound alone is an open-ended window, and the test is an overlap.

## Spaces and documents

`list_spaces`, `get_space`, `create_space`, `delete_space`,
`upload_file`, `list_documents`, `get_document`, `delete_document`.

`upload_file` takes scope as form fields. A document uploaded without scope is
invisible to every scoped query, so pass `user_id` if the space uses scoping.

## Receipts, graph, profiles

- `retrieve_receipt`, `get_receipt`, `explain` - what was retrieved and why.
- `get_graph`, `list_entities`, `get_entity` - the entity graph. Edges are
  co-occurrence, not typed predicates.
- `get_user_profile`, `ask_about_user` - what a space knows about one end user.
- `get_space_profile`, `list_memory_models`, `create_memory_model`,
  `get_memory_model`, `update_memory_model`, `delete_memory_model`,
  `refresh_memory_model`, `clear_memory_model`, `get_memory_model_history` - a
  memory model is the space's standing answer to a standing question.
  Create and refresh are queued and return a `job_id`.

## Settings and webhooks

`get_extraction_settings` / `set_extraction_settings` / `reset_extraction_settings`,
the same three for `chat_settings` and `reason_settings`,
`create_webhook` / `list_webhooks` / `update_webhook` / `delete_webhook` /
`list_webhook_deliveries`, plus `get_usage` and `list_catalog_models`.

Settings writes are a full replace: an omitted field is cleared, not left alone.

## Errors

```python
try:
    client.retrieve("my-space", "query")
except AnonaError as exc:
    exc.status_code   # 404
    exc.code          # "space_not_found"
    exc.detail        # human-readable message
    exc.request_id    # for support
    exc.retry_after   # seconds, when the server sent one
```

Transport failures are translated too, so a timeout or a dropped connection
raises `AnonaError` rather than a raw `httpx` exception.

## Framework adapters

```python
from anona.integrations import MemoryBridge

bridge = MemoryBridge(api_key="anona_live_...", space_id="my-space", user_id="customer-42")
```

Then hand the bridge to the adapter for LangChain, CrewAI, LlamaIndex, Google
ADK, Microsoft Agent Framework or Strands. Install the matching extra first.
`anona.integrations.litellm` patches LiteLLM so any model call it makes carries
memory. `anona[mcp]` ships the `anona-mcp` stdio server.
