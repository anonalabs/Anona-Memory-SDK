---
name: anona-memory-sdk
description: Building on Anona Memory: the SDK and REST surface, spaces, per-user scoping, error codes and framework adapters. Use when writing or reviewing code that calls Anona or adds memory to an agent.
license: MIT
---

# Building on Anona Memory

Anona is a managed memory layer reached over one HTTP API. Two official SDKs
wrap it: `anona` on PyPI and `@anona-labs/memory` on npm. This skill is the
shape of that surface, so code compiles the first time instead of being guessed.

Do not invent endpoints or arguments. If something is not in this skill or its
references, check `https://docs.anonalabs.com` before writing it.

## The model, in four terms

- **Space** - one memory store. A space's id *is* its name, so there is no
  separate id to assign. Names with spaces are legal and then need URL encoding.
- **Memory** - one recorded fact. Writing raw text is normal; the service
  extracts facts from it.
- **Scope** - `user_id` / `agent_id` / `session_id`. One space serves many end
  users without mixing them.
- **Credits** - one balance per organization, spent by every call. There is no
  separate operation quota.

## Install

```bash
pip install anona                      # Python
npm install @anona-labs/memory         # TypeScript
```

Framework adapters are extras: `pip install 'anona[langchain]'` and the same for
`crewai`, `llamaindex`, `adk`, `msagent`, `strands`, `litellm`, `mcp`.

## The three calls

```python
from anona import AnonaClient

client = AnonaClient(api_key=os.environ["ANONA_API_KEY"])

client.record("support-bot", "Acme is on the annual plan.", user_id="acme")
hits = client.retrieve("support-bot", "what plan is Acme on", limit=5, user_id="acme")
answer = client.reason("support-bot", "summarize Acme's account", user_id="acme")
```

```ts
import { Anona } from "@anona-labs/memory";

const anona = new Anona({ apiKey: process.env.ANONA_API_KEY! });

await anona.record({ spaceId: "support-bot", content: "Acme is on the annual plan.", userId: "acme" });
const hits = await anona.retrieve({ spaceId: "support-bot", query: "what plan is Acme on", userId: "acme" });
const answer = await anona.reason({ spaceId: "support-bot", query: "summarize Acme's account", userId: "acme" });
```

The Python class is `AnonaClient`; the TypeScript class is `Anona`. Python is
snake_case with the space id as the first positional argument, TypeScript is
camelCase with a single options object. Every Python method also has an
`async_` twin: `async_record`, `async_retrieve`, and so on.

Full method lists: `references/python.md`, `references/typescript.md`.

## Five things that bite

1. **A scoped read only sees scoped writes.** `retrieve(user_id="acme")` never
   returns a memory written without a `user_id`, and an unscoped retrieve never
   returns a scoped one. Adopting scoping mid-life hides everything written
   before it. Pick one convention per space and hold it. See
   `references/scoping.md`.
2. **`anona:` is a reserved tag prefix.** A customer tag starting with it is
   rejected with `422 reserved_tag`.
3. **Retrieve answers `results`, reason answers `insights`.** Different field
   names on the two responses, which is a common wrong `.get()`.
4. **`retrieve` never creates a space; a write does.** A typo'd space id on a
   read is `space_not_found`; the same typo on a write silently creates a new
   space. Call `list_spaces` when a read comes back empty and should not have.
5. **Reads are not free.** Every call spends credits and is rate limited per
   plan. `reason` is a multi-step synthesis over the whole space and can take
   tens of seconds; raise the client timeout for it rather than lowering it.

## Latency and blocking

`record` blocks on fact extraction, which is one model call. In a request path,
pass `background=True` (Python) or `background: true` (TypeScript) to get a
`job_id` back immediately, then poll `get_job` / `getJob`. `record_batch`
takes up to 100 items and is always queued.

`retrieve` has a `mode`: `"accurate"` reranks for relevance, `"fast"` skips that
pass for much lower latency.

## Errors

Every failure is `{"error": {"code": "...", "message": "..."}}` with a
lowercase snake_case code, and both SDKs raise `AnonaError` carrying `code`,
`status_code`, `request_id` and `retry_after`. Retry only 429 and 5xx, honouring
`Retry-After`. The full code table is in `references/rest-and-errors.md`.

## Frameworks

Every Python adapter wraps one `MemoryBridge`, so they configure identically:

```python
from anona.integrations import MemoryBridge

bridge = MemoryBridge(api_key="anona_live_...", space_id="my-space", user_id="customer-42")
```

| Framework | Extra | Import |
| --- | --- | --- |
| LangChain / LangGraph | `anona[langchain]` | `anona.integrations.langchain` |
| CrewAI | `anona[crewai]` | `anona.integrations.crewai` |
| LlamaIndex | `anona[llamaindex]` | `anona.integrations.llamaindex` |
| Google ADK | `anona[adk]` | `anona.integrations.google_adk` |
| Microsoft Agent Framework | `anona[msagent]` | `anona.integrations.ms_agent` |
| AWS Strands | `anona[strands]` | `anona.integrations.strands` |

TypeScript ships adapters for the Vercel AI SDK
(`@anona-labs/memory/vercel`) and OpenAI Agents
(`@anona-labs/memory/openai-agents`).

Memory failures never raise into the agent: the agent runs without memory and
the failure is logged.

## Adding memory with no integration code

The drop-in proxy speaks three wire formats at the same base URL, and recalls
and records around each turn by itself. Point an existing OpenAI or Anthropic
client at it and change nothing else:

- `POST /v1/chat/completions` - OpenAI Chat Completions
- `POST /v1/responses` - OpenAI Responses
- `POST /v1/messages` - Anthropic Messages

Tunables ride either the body or an `X-Anona-*` header, which is what lets a
stock SDK configure them once through `default_headers`.

## Reference

- `references/python.md` - the Python client, method by method.
- `references/typescript.md` - the TypeScript client, method by method.
- `references/rest-and-errors.md` - endpoints, request and response shapes, error codes.
- `references/scoping.md` - multi-tenant scoping, tags and the strict-match rule.
