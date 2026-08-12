# Examples

One runnable script per framework adapter. Each is a complete program — no
scaffolding, no shared harness — so you can read one and ignore the rest.

```bash
export ANONA_API_KEY=anona_live_YOUR_KEY
export ANONA_SPACE_ID=my-space          # optional; each script has a default
python examples/langchain_agent.py
```

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

## What to look for

Each script asks two questions in a row, where the second is only answerable
from what the first one stored. **Run it twice**: the second run starts with the
first run's memories already in the space, so it can answer the opening
question too.

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

## Scoping

Most scripts construct the bridge with `user_id="customer-42"`. Memories written
under one `user_id` are only ever returned to that same user, so one space can
back every customer of your app. Drop it and the space is shared by everyone.

Google ADK is the exception: it supplies its own `user_id` / `app_name` /
session id per call, and the adapter forwards them.

## Failure behaviour

Every adapter fails open. If Anona is unreachable, the recall or the store is
logged and the agent runs on without memory — no exception reaches your code. To
see it, run any script with a wrong API key: it still answers, just without
remembering anything.
