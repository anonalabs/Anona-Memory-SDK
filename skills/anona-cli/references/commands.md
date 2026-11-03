# Commands by task

Every data command takes `--space`, `--api-key`, `--json`. A flag you do not
give is left out of the request. Confirm any flag here against
`anona-memory <command> --help` before relying on it: an older install may lack it.

## Remember and recall

```bash
anona-memory record "The team ships on Tuesdays." --space S --json
anona-memory record - --space S --json < note.txt                    # content from stdin
anona-memory record --file decision.md --timestamp 2025-06-01 --space S --json
anona-memory record "..." --tags ops,schedule --metadata '{"source": "agent"}' --space S --json
anona-memory record "..." --user-id alice --space S --json            # scoped to one end user
anona-memory record "..." --agent-id triage --session-id s-42 --space S --json

anona-memory retrieve "deploy schedule" --space S --json --limit 5
anona-memory retrieve "what changed" --space S --json --occurred-after 2026-06-01
anona-memory retrieve "..." --space S --json --occurred-before 2026-07-01 --as-of 2026-08-01
anona-memory retrieve "..." --space S --json --tags ops --tags-match all
anona-memory retrieve "..." --space S --json --user-id alice
anona-memory retrieve "..." --space S --json --agent-id triage --session-id s-42
anona-memory retrieve "..." --space S --format block --max-tokens 800  # prompt-ready text

anona-memory reason "how does this team ship?" --space S --json
anona-memory reason "..." --space S --json --depth thorough            # slower, reads everything
anona-memory reason "..." --space S --json --user-id alice             # same scope flags as retrieve
```

- Write a memory as one complete, self-contained sentence. Short fragments
  often extract to nothing (exit `3`).
- `--timestamp` is when the event happened, for importing history.
- `--occurred-after` / `--occurred-before` filter on when the event happened;
  `--as-of` on when it was recorded ("what did we know on this date").
- Leave `--min-score` unset: scores span orders of magnitude (about 1.1 for a
  near-verbatim match, 0.1 down to 0.00001 for a correct paraphrase), so a
  floor drops correct answers.
- `--tag-groups JSON` (or `@file.json`) is a compound filter:
  `[{"or": [{"tags": ["project:alpha"]}, {"tags": ["project:beta"]}]}, {"not": {"tags": ["status:archived"]}}]`.
- `--member-id me` limits to what you wrote in a shared space. In a personal
  space every memory is yours, so it matches them all.
- `--tags` with the default `--tags-match any` (or `all`) also returns memories
  with no tags at all: untagged memories match every non-strict filter. Use
  `any_strict`, `all_strict` or `exact` to exclude them.
- `--prefer-observations` (the default) returns a synthesized note instead of
  the facts behind it; `--no-prefer-observations` returns both layers.
- `reason` takes tens of seconds. Use `retrieve` for lookups and `reason` only
  for questions no single memory answers.

## Background and bulk writes

```bash
anona-memory record "..." --async --space S --json                     # {"job_id", "status"}
anona-memory record "..." --async --wait --space S --json              # poll until done
anona-memory record --batch items.jsonl --space S --wait --json        # up to 100 items
anona-memory documents upload a.pdf b.md --space S --json              # {"job_ids": [...]}
anona-memory documents upload a.pdf --tags hr --user-id alice --space S --wait --json
anona-memory documents upload a.pdf --strategy NAME --space S --json   # a named ingestion strategy
anona-memory record --batch items.jsonl --space S --wait --wait-timeout 60 --json
```

`--wait-timeout SECONDS` (default 300) bounds `--wait`; past it the command
exits `8` and the job keeps running. Batch file format and `--wait` exit codes:
`scripting.md`.

## Jobs

```bash
anona-memory jobs get JOB_ID --space S --json      # status, memory_count, memory_ids, zero_yield_items
anona-memory jobs cancel JOB_ID --space S --yes    # queued parts only; needs the user's OK
```

Running statuses: `pending`, `processing`, `running`, `queued`. Final:
`completed`, `failed`, `cancelled`. An id the space does not hold exits `5`
with `job_not_found`. `zero_yield_items` counts items that produced no memory;
it can be non-zero even when `memory_count` is at or above `items_submitted`,
since one item can produce several memories and another none.

## Spaces

```bash
anona-memory spaces list --json                    # {"spaces": [...], "total"}
anona-memory spaces create NAME --description "..." --json
anona-memory spaces get NAME --json                # owner:name for a shared space
anona-memory spaces delete NAME --yes              # every memory in it; needs the user's OK
```

A space's id is its name. The first write to a missing space creates it; a read
never does (`space_not_found`, exit `5`). A bare name that is both yours and a
share answers `space_ambiguous`: use the `owner:name` form from `spaces list`.

## Browse and correct memories

```bash
anona-memory memories list --space S --json --limit 50 --q pnpm     # text or context, case-insensitive
anona-memory memories list --space S --json --member-id me
anona-memory memories list --space S --json --limit 50 --offset 50     # the next page
anona-memory memories list --space S --json --type note                # fact, note, experience, summary
anona-memory memories list --space S --json --type fact --no-prefer-observations  # facts a note covers are hidden by default
anona-memory memories list --space S --json --user-id alice            # also --agent-id, --session-id
anona-memory memories list --space S --json --no-prefer-observations   # notes and the facts behind them
anona-memory memories edit MEMORY_ID --text "Corrected sentence." --reason "was wrong" --space S --json
anona-memory memories history MEMORY_ID --space S --json
anona-memory memories delete MEMORY_ID --space S --yes      # needs the user's OK
```

## Documents

```bash
anona-memory documents list --space S --json --source file         # file, custom or memory
anona-memory documents list --space S --json --limit 100 --offset 100
anona-memory documents get DOCUMENT_ID --space S --json
anona-memory documents delete DOCUMENT_ID --space S --yes   # and its memories; needs the user's OK
```

Limits: 20 files per call, 25 MB per file (64 MB video), 50 MB per call.

## Space settings

Three groups, each with `get`, `set` and `reset --yes`:

```bash
anona-memory chat-settings get --space S --json
anona-memory chat-settings set --memory-limit 5 --memory-token-budget 800 --space S --json
anona-memory chat-settings set --auto-record --no-memory --chat-model MODEL_ID --space S --json
anona-memory chat-settings reset --space S --yes

anona-memory reason-settings get --space S --json
anona-memory reason-settings set --depth thorough --model MODEL_ID --space S --json
anona-memory reason-settings reset --space S --yes

anona-memory extraction-settings get --space S --json
anona-memory extraction-settings set --mode verbose --guidance @guidance.md --space S --json
anona-memory extraction-settings set --mode custom --custom-prompt @prompt.md --space S --json
anona-memory extraction-settings set --labels-file labels.json --no-free-form-entities --space S --json
anona-memory extraction-settings set --free-form-entities --space S --json
anona-memory extraction-settings set --clear guidance --space S --json
anona-memory extraction-settings reset --space S --yes
```

Boolean settings take a pair: `--auto-record` / `--no-auto-record`,
`--memory` / `--no-memory`, `--free-form-entities` / `--no-free-form-entities`.
`--labels-file` is a JSON array of label groups; one that cannot be read or is
not an array exits `2`. Each `values` entry is an object, not a plain string:

```json
[{"key":"team","description":"Which team owns it","type":"value","values":[{"value":"billing","description":"Billing team"}]}]
```

`type` is `value` or `multi-values` (both need `values`), or `text` or
`multi-text` (free-form, no `values`). `key` is lowercase letters, digits, `-`
or `_`, no colon. `"tag": true` also stamps `key:value` as a filterable tag.

`set` changes only the fields named and keeps the rest (it reads, modifies and
writes back). `--clear FIELD` returns a field to the platform default. `null`
in a `get` means "platform default". Owner only on a shared space.

## Memory models, profile, rules, webhooks, routing

Need pip 0.26.0 / npm 0.20.0. Deletes, `clear` and `reset` need `--yes`.

```bash
anona-memory models list --space S --json --limit 50 --offset 0 --tags ops
anona-memory models create "Build habits" --query "How does this team build?" --space S --json
anona-memory models create NAME --query Q --tags ops --mode delta --refresh-on-new-memories --memory-type fact --max-tokens 2000 --model-id build-habits --space S --json
anona-memory models get MODEL_ID --space S --json
anona-memory models edit MODEL_ID --name N --query Q --no-refresh-on-new-memories --space S --json
anona-memory models refresh MODEL_ID --space S --json               # background; poll `jobs get`
anona-memory models history MODEL_ID --space S --json
anona-memory models clear MODEL_ID --space S --yes                  # keeps the definition
anona-memory models delete MODEL_ID --space S --yes

anona-memory profile set --mission "What the space is for" --skepticism 3 --literalism 3 --empathy 3 --space S --json
anona-memory profile reset --space S --yes

anona-memory rules list --space S --json
anona-memory rules add "Never state revenue as committed." --name N --priority 10 --active --tags ops --space S --json
anona-memory rules edit RULE_ID --text "..." --active --priority 5 --no-active --space S --json
anona-memory rules delete RULE_ID --space S --yes

anona-memory webhooks list --space S --json
anona-memory webhooks create https://example.com/hook --events memory.created,memory.consolidated --enabled --space S --json
anona-memory webhooks edit WEBHOOK_ID --url https://example.com/new --no-enabled --space S --json
anona-memory webhooks deliveries WEBHOOK_ID --limit 20 --cursor C --space S --json
anona-memory webhooks delete WEBHOOK_ID --space S --yes

anona-memory routing-settings get --space S --json
anona-memory routing-settings set --charter "Billing" --topics billing,invoices --auto-route --space S --json
anona-memory routing-settings set --rules-file rules.json --shared-auto-route --space S --json
anona-memory routing-settings set --no-auto-route --no-shared-auto-route --clear topics --space S --json
anona-memory routing-settings reset --space S --yes
```

`profile set` keeps the fields you do not name. `webhooks create` returns the
signing secret once (`--json` includes `secret`); never log it. `--mission`,
`--charter` and `rules edit --text` take `@FILE`.

## Explore

```bash
anona-memory graph --space S --json --limit 20 --min-count 2
anona-memory graph draw "memories added per week" --model MODEL_ID --space S --json
anona-memory entities list --space S --json --limit 20 --offset 0
anona-memory entities get ENTITY_ID --space S --json
anona-memory receipts get REQUEST_ID --json                          # X-Request-ID of a retrieve
anona-memory receipts get REQUEST_ID --explain --memory-id MEMORY_ID --json
anona-memory ask-user alice "What does she prefer?" --depth thorough --model MODEL_ID --space S --json
anona-memory chat "What did we decide about releases?" --model MODEL_ID --space S --json
anona-memory chat "Hello" --no-memory --space S --json   # neither recalls nor records the turn
anona-memory space-types --json
```

## Organization and billing (needs `anona-memory login --cli`)

These read the control plane. An API key or a temporary profile exits `4` with
`session_required`: ask the user to run `anona-memory login --cli`. If they already
did, an API key (`--api-key`, `ANONA_API_KEY` or `~/.anona/config.env`) outranks
the login: unset it, or run the command without it.

```bash
anona-memory org --json
anona-memory orgs list --json
anona-memory projects list --json
anona-memory keys list --json                    # never a secret
anona-memory members list --json
anona-memory members me --json
anona-memory members invitations --json
anona-memory spaces members list SPACE --json    # owner or admin of the space's org
anona-memory invitations pending --json
anona-memory invitations accept TOKEN --json     # only with the user's OK
anona-memory invitations decline TOKEN --json
anona-memory billing --json
anona-memory billing topups --json
anona-memory usage --history --days 30 --json
anona-memory usage --breakdown --days 7 --json
anona-memory exports list --space S --json
anona-memory exports create --space S --json
anona-memory exports download JOB_ID --out FILE --space S --yes --json   # --yes overwrites FILE
```

These are done in the dashboard, not the CLI. Each prints one line with the
link to stderr, sends nothing and exits `4`. Hand the user the link.

```bash
anona-memory keys create NAME
anona-memory keys revoke KEY_ID
anona-memory members invite EMAIL
anona-memory members remove MEMBER_ID
anona-memory spaces members remove SPACE MEMBER_ID
anona-memory billing upgrade       # also checkout, topup, cancel, portal
anona-memory org rename            # also delete, transfer, leave
```

## Models, profiles, usage

```bash
anona-memory llm-models --json                     # the LLMs you can choose, with credit prices
anona-memory model MODEL_ID --space S --json       # one memory model: a standing question and its answer
anona-memory profile --space S --json              # the space's mission and disposition
anona-memory user-profile alice --space S --json   # what the space knows about one end user
anona-memory user-profile alice --space S --json --limit 20 --offset 20
anona-memory usage --json                          # credits remaining, rate limit
```

`llm-models` (LLMs) and `model` (memory models) are unrelated.

## Account and diagnostics

```bash
anona-memory status                 # which credentials are stored (local files only)
anona-memory doctor --json          # which credential wins and why; server, space, credits left
anona-memory start                  # temporary profile, no account, 72 hours
anona-memory login                  # a person approves in a browser; never run it and wait
anona-memory login --cli            # the same login, with no fallback (--mcp: a separate MCP-only login)
anona-memory login --no-browser     # print the approval URL instead of opening it
anona-memory logout --cli           # remove the command-line login only
anona-memory logout --mcp           # remove the MCP login only
anona-memory logout --force         # also abandon an unclaimed temporary profile; needs the user's OK
anona-memory mcp                    # stdio MCP server, for an MCP client to launch
anona-memory completion bash        # shell completion script (bash, zsh, fish)
anona-memory                 # where to start; reads no credential, sends nothing
anona-memory guide --json           # every command, flag and --json shape, from this copy's parser
anona-memory help spaces delete     # help for one command; `help --llm` is `guide`
```
