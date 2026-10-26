# Calling Anona

Four transports, same data. Use the first one that is available.

## 1. MCP tools (preferred)

If the host exposes them, these tools are already authenticated and there is
nothing to configure.

| Tool | Arguments | Returns |
| --- | --- | --- |
| `record` | `space_id`, `content`, optional `tags`, `user_id`, `agent_id`, `session_id` | the stored `memory_id` |
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

## 2. The `anona` CLI

pip `anona` 0.24.0 or later (`anona --version`). For a shell with no MCP tools.
Pass `--json` on every data command: stdout is then one JSON document, and on
failure stdout is empty with a one-line error on stderr.

This section covers what the memory loop needs. For anything else (file
uploads, batch ingest, settings, jobs) run `anona guide --json`, which lists
every command and flag the installed copy has, and see the `anona-cli` skill.

### Credentials

The data commands take the first of these that exists:

1. `--api-key` (visible in the process list; prefer the next two)
2. `ANONA_API_KEY` in the environment
3. `ANONA_API_KEY` in `~/.anona/config.env` (the CLI reads it; no need to source it)
4. the command-line login from `anona login` (`~/.anona/cli-credentials.json`)
5. a temporary profile from `anona start`

With 1-4 a space is required: `--space`, else `ANONA_SPACE_ID` (environment,
then `config.env`). Without one the command exits 2 and sends nothing. A
temporary profile has one space, `default`; omit `--space` there.

The MCP login (`~/.anona/credentials.json`, used by `anona mcp`) is never
used by these commands. With only that stored they exit 4.

`anona status` names the credential in use and where it came from, and when a
temporary profile expires. It never prints a token.

### Account and connection commands

```bash
anona start                 # temporary profile: no account, one space, 72 hours
anona login                 # a person approves in a browser: CLI, then MCP
anona login --cli           # only the command-line credential
anona login --mcp           # only the MCP credential
anona status
anona logout                # both; --cli or --mcp for one
anona mcp                   # stdio MCP server, using the MCP login
```

`anona login` claims a stored temporary profile, keeping its memories. To give
an MCP client the tools: `anona login --mcp`, then
`claude mcp add anona -- anona mcp`, then restart the client.

### Record, retrieve, reason

```bash
anona record "Deploys run Tuesdays at 10:00 UTC." --space ops --json \
  --tags ops,schedule --metadata '{"source": "claude-code"}'
echo "The release branch is cut on Mondays." | anona record - --space ops --json
anona record --file notes/decision.md --timestamp 2025-06-01 --space ops --json

anona retrieve "deploy schedule" --space ops --json --limit 5
anona retrieve "deploy schedule" --space ops --json --tags ops --tags-match all
anona retrieve "what changed" --space ops --json --occurred-after 2026-06-01

anona reason "how does this team ship?" --space ops --json
anona reason "how does this team ship?" --space ops --json --depth thorough
```

| Verb | Flags beyond `--space`, `--api-key`, `--base-url`, `--json` |
| --- | --- |
| `record CONTENT` (`-` = stdin) | `--file PATH`, `--tags a,b`, `--timestamp DATE`, `--metadata JSON`, `--user-id`, `--agent-id`, `--session-id` |
| `retrieve QUERY` | `--limit N` (1-100), `--min-score X`, `--tags a,b`, `--tags-match` (`any`, `all`, `any_strict`, `all_strict`, `exact`), `--as-of DATE`, `--occurred-after DATE`, `--occurred-before DATE`, `--user-id`, `--agent-id`, `--session-id` |
| `reason QUERY` | `--depth` (`fast`, `thorough`), `--user-id`, `--agent-id`, `--session-id` |

An unset flag is left out of the request. Dates are ISO 8601. `--json` output:
`record` gives `status`, `memory_id`, `memory_ids`; `retrieve` gives
`{"results": [...]}`; `reason` gives `{"insights", "sources", ...}`.

Leave `--min-score` unset: scores span orders of magnitude (about 1.1 for a
near-verbatim match, 0.1 down to 0.00001 for a correct paraphrase), so a floor
drops correct answers. `reason` takes tens of seconds; use `--depth thorough` only
when a `fast` answer reads stale.

### Spaces, memories, jobs

```bash
anona spaces list --json
anona spaces create my-project --description "Notes for my-project" --json
anona spaces get my-project --json
anona memories list --space my-project --json --limit 50 --q pnpm
anona memories edit MEMORY_ID --text "Corrected sentence." --reason "was wrong" --space my-project --json
anona memories history MEMORY_ID --space my-project --json
anona memories delete MEMORY_ID --space my-project --yes --json
anona jobs get JOB_ID --space my-project --json
anona jobs cancel JOB_ID --space my-project --yes --json
anona profile --space my-project --json
anona model MODEL_ID --space my-project --json
anona user-profile customer-42 --space my-project --json
anona usage --json
```

`memories delete`, `spaces delete`, `documents delete`, `jobs cancel` and the
settings `reset` commands exit 2 and send nothing without `--yes`. Only pass
`--yes` when the user asked for that deletion. A temporary profile
cannot create a second space (`temp_profile_single_space`).

### Exit codes

| Exit | Means |
| --- | --- |
| `0` | Done. An empty retrieve or list is still `0`. |
| `1` | Refused for another reason (a `409` or `422`), or a job `--wait` watched failed. stderr starts `anona: <error_code>:`. On pip 0.24.0, every failure. |
| `2` | Bad command, nothing sent: missing `--space`, empty content, a bad flag value, no `--yes`. |
| `3` | `record` only: accepted, nothing extracted, nothing stored. Re-record once with more context. |
| `4` | No usable credential (none, MCP login only, a login that could not be renewed), a refused host, or a `401`/`403`. |
| `5` | Not found: the space, memory or job does not exist. |
| `6` | Rate limited after retries, or out of credits. |
| `7` | Unreachable, a timeout or a `5xx`. A write may have been stored: do not re-send it blindly. |
| `8` | `--wait` gave up while the job was still running. |

Codes `5`-`8` arrived in the release after pip 0.24.0; older copies use `1`.

The npm package `@anona-labs/memory` also installs a binary called `anona`,
which may not have the data commands yet. If `anona record` answers
`invalid choice: 'record'`, the binary on PATH is the npm one or an old pip
copy: use `uvx --from anona anona <command>`.

## 3. REST with curl

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

When the key sees more than one space and `ANONA_SPACE_ID` is not set, let
Anona pick (see the skill's **Which space**). Replace `space_id` with `route`
and name a fallback:

```bash
curl -sS https://api.anonalabs.com/v1/retrieve \
  -H "Authorization: Bearer $ANONA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"route":"auto","fallback_space_id":"my-project","query":"how is auth wired","limit":5}'
```

The response carries `searched` (a write carries `routed_to`): the space or
spaces that were actually used. Sending both `space_id` and `route`, or neither,
is `422 route_conflict`. The MCP `record` and `retrieve` tools take the same
two fields; the `anona` CLI does not, so keep `--space` there.

A write that should not block the caller takes `"async": true` and answers with
a `job_id` instead of a `memory_id`. Poll it at
`GET /v1/spaces/{space_id}/jobs/{job_id}`.

## 4. Python SDK

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
