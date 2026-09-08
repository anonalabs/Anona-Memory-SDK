# Calling Anona

Three transports, same data. Use the first one that is available.

## 1. MCP tools (preferred)

If the host exposes them, these tools are already authenticated and there is
nothing to configure.

| Tool | Arguments | Returns |
| --- | --- | --- |
| `record` | `space_id`, `content`, optional `metadata`, `tags`, `user_id`, `agent_id`, `session_id` | the stored `memory_id` |
| `retrieve` | `space_id`, `query`, `limit`, optional `mode`, `user_id`, `agent_id`, `session_id` | ranked memories |
| `reason` | `space_id`, `query`, optional scope keys, `model` | a synthesized answer |
| `list_spaces` | none | the spaces this key can see |
| `get_profile` | `space_id` | the space's standing profile and memory models |
| `get_user_profile` | `space_id`, `user_id` | what the space knows about one end user |

Per-tool failures come back as tool errors, not broken connections. The session
stays alive; read the error and move on.

Not registered yet? One command:

```bash
claude mcp add --transport http anona https://memory.anonalabs.com/mcp \
  --header "Authorization: Bearer $ANONA_API_KEY"
```

MCP tools only load at session start. After adding a server, restart the client
or run `/mcp`.

## 2. REST with curl

Needs `ANONA_API_KEY`. The installer writes it to `~/.anona/config.env` with
mode 600; source that file if the variable is not already in the environment.

```bash
. ~/.anona/config.env 2>/dev/null || true
```

Recall:

```bash
curl -sS https://api.anonalabs.com/v1/retrieve \
  -H "Authorization: Bearer $ANONA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"space_id":"my-project","query":"how is auth wired","limit":5}'
```

```json
{"results": [{"memory_id": "...", "content": "...", "relevance_score": 1.4, "timestamp": "..."}]}
```

`relevance_score` can exceed 1.0. It is a ranking signal, not a probability.

Record:

```bash
curl -sS https://api.anonalabs.com/v1/record \
  -H "Authorization: Bearer $ANONA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"space_id":"my-project","content":"Prefers pytest over unittest.","metadata":{"source":"claude-code"}}'
```

Synthesize:

```bash
curl -sS https://api.anonalabs.com/v1/reason \
  -H "Authorization: Bearer $ANONA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"space_id":"my-project","query":"what do we know about this user"}'
```

```json
{"insights": "..."}
```

Note the field names differ by endpoint: `retrieve` answers with `results`,
`reason` answers with `insights`.

Scope a write and a read to one end user by adding `"user_id": "customer-42"`
to either body.

A write that should not block the caller takes `"async": true` and answers with
a `job_id` instead of a `memory_id`. Poll it at
`GET /v1/spaces/{space_id}/jobs/{job_id}`.

## 3. Python SDK

Only when the project already depends on it.

```python
from anona import AnonaClient

client = AnonaClient(api_key="anona_live_...")
client.record("my-project", "Prefers pytest over unittest.")
hits = client.retrieve("my-project", "test framework", limit=5)
answer = client.reason("my-project", "how does this user like to work")
```

## Cost

Every call spends credits, the same as a REST call from an application. Recall
is cheap and worth doing on every task. `reason` reads the whole space and is
not; use it for real synthesis questions, not as a search.
