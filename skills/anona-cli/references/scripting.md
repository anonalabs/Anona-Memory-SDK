# Scripting reference

## `--json` documents

One document on stdout; nothing on stdout on failure. `anona guide --json`
carries each command's shape in `json_output`.

| Command | Document |
| --- | --- |
| `record` | `{"memory_id", "memory_ids": [...], "status", "message"?, "usage", "routed_to"}` |
| `record --async` | `{"job_id", "status"}` |
| `record --batch` | `{"job_id", "job_ids", "status", "accepted"}` (`job_ids` null unless the batch was split) |
| any `--wait` | `{"submitted": <the submit document>, "jobs": [<jobs get>...]}` |
| `retrieve` | `{"results": [{"memory_id", "content", "relevance_score", "memory_type", "tags", ...}]}` |
| `retrieve --format block` | `{"results": [...], "context", "token_estimate", "usage"}` |
| `reason` | `{"insights", "sources": {"models", "memories", "notes"}, "rules_applied", "model", "usage"}` |
| `spaces list` | `{"spaces": [{"space_id", "name", "description", "qualified_id", "shared_by", ...}], "total"}` |
| `memories list` | `{"items": [{"id", "content", "type", "state", "tags", "source_ids", ...}], "total", "limit", "offset"}` |
| `documents upload` | `{"job_ids": [...]}` |
| `documents list` | `{"documents": [{"document_id", "source", "created_at", "memory_count", "tags"}], "total", "limit", "offset"}` |
| `jobs get` | `{"job_id", "status", "created_at", "completed_at", "error", "memory_count", "memory_ids", "items_submitted", "zero_yield_items", "progress"}` |
| `jobs cancel` | `{"job_id", "status", "cancelled_parts", "running_parts"}` |
| any delete | `{"deleted": true, ...ids}` (written by the CLI) |
| any settings `reset` | `{"reset": true, "space_id", "settings"}` |
| `doctor` | `{"credential": {"kind", "source", "why", ...}, "health": {"ok", ...}, "space": {"id", "accessible", ...}, "credits": {"remaining", "exhausted", ...}, "ok"}` |

Field names differ by command: `retrieve` answers `results`, `reason` answers
`insights`, `memories list` answers `items`.

A `record` that stored nothing exits `3` and still prints its document, with
`"status": "nothing_extracted"`.

## Waiting on jobs

`record --async`, `record --batch` and `documents upload` return job ids.
`--wait` polls them (free; backs off 1 s to 5 s) until they finish:

| Exit after `--wait` | Means |
| --- | --- |
| `0` | every job completed |
| `3` | `record` only: completed, nothing stored |
| `1` | a job failed or was cancelled |
| `5` | the server no longer knows a job |
| `8` | still running at `--wait-timeout` (default 300 s) |

`--wait` without `--async` or `--batch` on `record` exits `2` (a plain record
already waits). `--wait-timeout 0` is a single status check.

## Batch files (`record --batch FILE`, `-` for stdin)

JSON Lines, at most 100 items, one per line. A line is either a JSON string
(the content) or an object with `content` and optionally `context`,
`timestamp`, `metadata`, `tags`, `user_id`, `agent_id`, `session_id`. Nothing
else: an unknown key exits `2`. Blank lines are skipped.

```
"The release branch is cut on Mondays."
{"content": "Acme renewed on the annual plan.", "timestamp": "2026-03-02", "user_id": "acme"}
```

The whole file is validated before anything is sent; a bad line exits `2` and
names its line number. `--tags`, `--timestamp`, `--metadata` cannot be combined
with `--batch` (put them on each line); `--user-id` / `--agent-id` /
`--session-id` apply to every item. Split larger files:

```bash
split -l 100 big.jsonl part-
for f in part-*; do anona record --batch "$f" --space S --wait --json || break; done
```

## Retries and timeouts

| Option | Default | |
| --- | --- | --- |
| `--timeout SECONDS` | 120 | per HTTP request |
| `--retries N` | 2 | retries a `429`, and a `5xx` only on a request safe to repeat |
| `--quiet`, `-q` | off | no progress or retry notices on stderr |

A write is never re-sent after a `5xx` or timeout. These are global options;
they go before or after the command.

## Errors

stderr: `anona: <error_code>: <message> (request_id <id>)`. Match on the code,
which is lowercase snake_case: `space_not_found`, `space_ambiguous`,
`space_owner_only`, `space_read_only`, `temp_profile_single_space`, `job_not_found`,
`validation_error`, `reserved_tag`, `invalid_scope`, `rate_limited`,
`credits_exhausted`, `reason_timeout`, `write_status_unknown`,
`unsupported_file_type`, `file_too_large`. A failure to connect is
`anona: could not reach <host>: ...`, never an HTTP status.

`temp_profile_single_space` (exit `4`): a temporary profile has only the
`default` space; it cannot create spaces or write elsewhere. `credits_exhausted`
(exit `6`): reads (`retrieve`, `reason`, `memories list`, `documents list`,
`profile`) spend credits too and are refused at zero, like writes.

`write_status_unknown` means the write timed out server-side and **may have
landed**: read back before retrying. `reason_timeout` means ask something
narrower, or retry with `--depth fast`.
