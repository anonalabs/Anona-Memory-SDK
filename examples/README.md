# Examples

One runnable script per scenario. Each is a complete program — no scaffolding,
no shared harness — so you can read one and ignore the rest.

```bash
export ANONA_API_KEY=anona_live_YOUR_KEY
export ANONA_SPACE_ID=my-space          # optional; each script has a default
python examples/memory_basics.py
```

Every script creates a throwaway space and deletes it on the way out, so
running them leaves nothing behind.

## Scenarios (SDK only)

The product, story by story. `pip install anona` is all these need.

| Script | The story |
| --- | --- |
| [`memory_basics.py`](memory_basics.py) | The core loop: `record`, `retrieve`, `reason`, and `get_context` for prompts. Start here. |
| [`support_bot_scoping.py`](support_bot_scoping.py) | One space, many customers — `user_id` keeps their memories strictly apart, on retrieve **and** on reason. |
| [`drop_in_proxy.py`](drop_in_proxy.py) | Your OpenAI code pointed at Anona: conversation two remembers conversation one, with no history passed. Also needs `pip install openai`. |
| [`document_qa.py`](document_qa.py) | A file becomes answerable memory: upload, poll the job, ask — with `document_id` provenance on the hits. |
| [`time_travel.py`](time_travel.py) | Dated backfill, then the three time controls: event-time windows, `as_of` point-in-time replay, and a moved "now". |
| [`user_profiles.py`](user_profiles.py) | Everything known about one end user — as data or a prompt block — and `ask_about_user` for questions about them. |
| [`multi_agent_shared_space.py`](multi_agent_shared_space.py) | A researcher and a writer share one space: `agent_id` separates their working sets, an unscoped read is the handoff. |
| [`background_ingestion.py`](background_ingestion.py) | High-volume writes: `background=True` and `record_batch`, with `get_job` polling for the stored ids. |
| [`knowledge_graph.py`](knowledge_graph.py) | The entity graph a space builds on its own: nodes, co-occurrence edges, and per-entity observations. |
| [`webhooks_and_settings.py`](webhooks_and_settings.py) | Configure a space: webhook lifecycle, proxy defaults, extraction settings. |

## Framework adapters

The same memory wired into an agent framework's own seam. These run a real
agent loop, so each also needs that framework's model credentials.

| Script | Install | Also needs |
| --- | --- | --- |
| [`langchain_agent.py`](langchain_agent.py) | `pip install 'anona[langchain]' langchain-openai` | `OPENAI_API_KEY` |
| [`crewai_crew.py`](crewai_crew.py) | `pip install 'anona[crewai]'` | `OPENAI_API_KEY` |
| [`llamaindex_agent.py`](llamaindex_agent.py) | `pip install 'anona[llamaindex]' llama-index-llms-openai aiosqlite` | `OPENAI_API_KEY` |
| [`google_adk_agent.py`](google_adk_agent.py) | `pip install 'anona[adk]'` | `GOOGLE_API_KEY` |
| [`ms_agent_framework.py`](ms_agent_framework.py) | `pip install 'anona[msagent]' agent-framework-openai` | `OPENAI_API_KEY` |
| [`strands_agent.py`](strands_agent.py) | `pip install 'anona[strands]'` | AWS credentials |

The model and provider in each script are only there to make it run — swap in
whatever your app uses. Nothing about the memory wiring depends on them.

### What to look for

Each framework script asks two questions in a row, where the second is only
answerable from what the first one stored. **Run it twice**: the second run
starts with the first run's memories already in the space, so it can answer the
opening question too.

They also differ on purpose, because the frameworks do:

- **LangChain** and **Microsoft Agent Framework** are fully automatic — one
  middleware or one context provider, and every turn is recalled and stored.
- **CrewAI** and **Strands** expose memory as *tools*, so the model chooses when
  to use them. Both scripts spend their prompt text telling it when; that
  wording is part of the integration, not decoration.
- **Google ADK** wires nothing on its own in either direction: `load_memory`
  gives the model a search tool, and an `after_agent_callback` stores the turn.
- **LlamaIndex** only flushes a turn to a memory block when its own short-term
  buffer overflows, so the last turn of a conversation never lands by itself —
  that script calls `bridge.remember(...)` explicitly for guaranteed capture.

### Scoping

Most framework scripts construct the bridge with `user_id="customer-42"`.
Memories written under one `user_id` are only ever returned to that same user,
so one space can back every customer of your app. Drop it and the space is
shared by everyone. (`support_bot_scoping.py` shows the same thing without a
framework.)

Google ADK is the exception: it supplies its own `user_id` / `app_name` /
session id per call, and the adapter forwards them.

### Failure behaviour

Every adapter fails open. If Anona is unreachable, the recall or the store is
logged and the agent runs on without memory — no exception reaches your code. To
see it, run any script with a wrong API key: it still answers, just without
remembering anything.
