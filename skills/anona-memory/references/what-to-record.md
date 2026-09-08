# What earns a memory

The test: **would a future session need this, and could it not re-derive it from
the repository?** If it could just read the code, do not store it.

## Worth recording

| Situation | The memory |
| --- | --- |
| User corrects your approach | "Wants database migrations reviewed as their own PR, never bundled with feature code." |
| A decision with a reason | "Chose Redis over in-process caching because the API runs four workers and they must not disagree." |
| A bug that cost real time | "A secret-only change to the task definition leaves it byte-identical, so nothing restarts. Needs `--force-new-deployment`." |
| An undocumented constraint | "The staging database has no seed data. Integration tests must create their own fixtures." |
| A rejected option | "Rejected server-side rendering for the settings page: the data is per-user and uncacheable, so it bought nothing." |
| A standing preference | "Prefers terse commit subjects under 50 characters, body only when the reason is not obvious." |

## Not worth recording

| Looks tempting | Why not |
| --- | --- |
| "The project uses FastAPI and Postgres." | The repository says so. |
| "Fixed the failing test in `test_auth.py`." | Git history says so, in more detail. |
| "The user asked me to refactor the parser." | Narration, not a fact. |
| "Might be worth switching to uv later." | Speculation. Record it when it is decided. |
| "API key is anona_live_abc123." | Never. Secrets do not go in memory. |
| "The user seemed frustrated." | Not durable, and not yours to store. |

## Shape

One fact per memory. Two facts in one string means neither can be retrieved
cleanly, because the embedding averages them.

- Split: "Prefers pytest. Wants migrations in their own PR." becomes two writes.
- Self-contained: "the second one" or "that file" is meaningless in six months.
  Name the thing.
- Present tense for standing facts, past tense for outcomes.

## Timing

Record when something is settled, not when it is proposed. In practice that is:

- Immediately, when the user states a preference or corrects you.
- At the end of a task, for what the task established.
- Never mid-debug, while the cause is still a hypothesis.

## Backdating

Importing history? Pass `timestamp` (ISO 8601) as when the *event* happened, so
a memory about last June is dated last June and ranks accordingly. It does not
change when the memory was recorded.
