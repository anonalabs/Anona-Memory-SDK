---
name: anona-cli
description: Using Anona Memory from a shell with the anona CLI. Use with no MCP tools, in scripts or CI, to bulk upload files, batch-ingest JSON Lines, manage spaces, jobs or settings, or on any anona exit code.
license: MIT
---

# The anona CLI

`anona` is the command line for Anona Memory: record, retrieve and reason over
memories, and manage spaces, memories, documents, jobs and per-space settings.
This skill is how to drive it without guessing.

## When to use it

- **No MCP tools.** If Anona's MCP tools (`record`, `retrieve`, `reason`,
  `list_spaces`) are in your own tool list, use them instead. Only a tool you can
  call counts.
- **Shell work MCP has no tool for:** uploading files (`documents upload`),
  ingesting a JSON Lines export (`record --batch`), changing a space's settings,
  checking or cancelling a background job, deleting a memory or a space.
- **Scripts and CI**, where `--json` and the exit code are the interface.

## Rule 1: read the CLI's own reference before an unfamiliar command

Never guess a flag. The installed CLI describes itself. Start with `anona` on
its own (where to start: credentials, the three memory commands; sends
nothing, exits `0`) or go straight to `anona guide`:

```bash
anona                   # orientation: how to sign in, the memory verbs, where to read more
anona guide --json      # every command, flag, example, --json shape, exit code
anona <command> --help  # one command
```

`anona guide --json` is generated from the parser, so it lists exactly what
this copy has. Look a command up with jq:

```bash
anona guide --json | jq '.. | objects | select(.name? == "documents upload")'
```

An older copy answers a bare `anona` with a usage error (exit `2`) and
`anona guide` with `invalid choice: 'guide'`. There, use
`anona --help` and `anona <command> --help`, or run the current version with
`uvx --from anona anona <command>`. `anona --version` shows what is installed.

## Rule 2: always `--json`, branch on the exit code

With `--json`, stdout is exactly one JSON document, and empty on failure; the
error is one line on stderr, `anona: <error_code>: <message>`. Never parse the
plain-text output.

| Exit | Means | Do |
| --- | --- | --- |
| `0` | Done. An empty search is `{"results": []}`, still `0`. | Carry on. |
| `1` | Refused for another reason (`409`, `422`), or a waited-on job failed. | Read the code on stderr. Do not retry blindly. |
| `2` | Your command was wrong. **Nothing was sent or billed.** | Fix it once, using `anona <command> --help`. |
| `3` | `record` only: accepted, but nothing was extracted. **Not a failure.** | Re-record once with a sentence of context, or move on. |
| `4` | No usable credential, a refused host, `401` or `403`. | Do not retry. See **Credentials**. |
| `5` | `404`: space, memory, job or setting not found. | Check the id (`anona spaces list`). |
| `6` | Rate limited after retries, or out of credits (reads spend credits too). | Stop. `anona usage` shows credits. |
| `7` | `5xx`, timeout, or unreachable. **A write may have been stored.** | Never re-run a write without checking first. |
| `8` | `--wait` gave up; the job is still running. | Poll later with `anona jobs get JOB_ID --space S`. |

Older CLIs (pip 0.24.0, the npm package) report `5`-`7` as `1` and a `403` as
`1`: treat `1` like `7` for a write there, and check before re-sending.

## Rule 3: writes are not idempotent

The CLI never re-sends a write after a timeout or a `5xx`, because it may
already have landed. After exit `7` from `record` or `documents upload`, check
with `anona retrieve` or `anona memories list --q "<text>"` (or
`anona documents list`) before you run it again.

## Rule 4: destructive commands need `--yes`, and the user's say-so

`spaces delete`, `memories delete`, `documents delete`, `jobs cancel` and the
three settings `reset` commands exit `2` and send nothing without `--yes`. That
refusal message says exactly what would be destroyed. Show it to the user and
add `--yes` only once they confirm. Never add it on your own initiative.

## Credentials

The data commands take the first that exists:

1. `--api-key` (visible in the process list; prefer the next two)
2. `ANONA_API_KEY` in the environment
3. `ANONA_API_KEY` in `~/.anona/config.env`
4. the command-line login from `anona login`
5. a temporary profile from `anona start`

With 1-4 a space is required: `--space S` or `ANONA_SPACE_ID`, else exit `2`.
A temporary profile has one space, `default`: omit `--space`. The MCP login is
never used by these commands. `anona doctor --json` says which credential wins
and why, and whether the space is reachable, without printing a secret.

`anona login` needs a person at a browser: suggest it in one line, never run it
and wait. You may run `anona start` for a temporary profile (72 hours, no
account) and tell the user it expires and that `anona login` keeps it. Details:
`references/credentials.md`.

## Scoping

One space can serve many end users. Pass `--user-id` (or `--agent-id`,
`--session-id`) on **both** the write and the read: scoped reads are strict and
never return memories written without that scope, and the reverse. Pick one
convention per space and hold it. Uploads take the same flags.

## The everyday commands

```bash
anona retrieve "how is auth wired" --space my-project --json --limit 5
anona record "Prefers pytest over unittest." --space my-project --json
anona reason "what do we know about this user" --space my-project --json
anona documents upload spec.pdf notes.md --space my-project --wait --json
anona record --batch history.jsonl --space my-project --wait --json
anona jobs get JOB_ID --space my-project --json
```

More, by task: `references/commands.md`. JSON shapes, `--wait`, batch files,
retries: `references/scripting.md`.

## Failure is never the user's problem

If memory is optional to the task, a failed `anona` call must not stop it. Carry
on without memory and mention the failure once, at the end. Never retry in a
loop.
