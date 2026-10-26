# Anona Memory agent skills

Three [Agent Skills](https://docs.anonalabs.com/integrations/skills) that teach a
coding agent to use Anona Memory without being told to, in whichever harness you
already use.

| Skill | For | Fires when |
| --- | --- | --- |
| `anona-memory` | The agent's own memory. | A task starts, a task ends, or the user says remember, recall, or refers to an earlier session. |
| `anona-memory-sdk` | Writing code against Anona. | Anything touching the SDK, the REST API, scoping, or a framework adapter. |
| `anona-cli` | Driving the `anona` command line. | No MCP tools, a script or CI job, a file upload or batch ingest, managing spaces, jobs or settings, or an `anona` exit code. |

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/anonalabs/Anona-Memory-SDK/main/skills/install.sh | bash
```

The installer finds the agents already on the machine, copies the skills in,
stores a key at `~/.anona/config.env` (mode 600), and registers the MCP server
with Claude Code when the `claude` CLI is available. It edits no shell profile
and writes nothing outside the directories it prints.

```bash
install.sh --app claude --api-key anona_live_... --space my-project
install.sh --skill anona-memory --project     # into ./.claude/skills
install.sh --dir /somewhere/else/skills
install.sh --zip                              # build uploadable .zip files
install.sh --uninstall
install.sh --help
```

Other harnesses, and any harness this installer does not know:

```bash
npx skills add anonalabs/Anona-Memory-SDK
```

Claude Code plugin marketplace:

```
/plugin marketplace add anonalabs/Anona-Memory-SDK
/plugin install anona-memory@anona
```

By hand:

```bash
git clone https://github.com/anonalabs/Anona-Memory-SDK
cp -R Anona-Memory-SDK/skills/anona-memory ~/.claude/skills/
```

## Claude Desktop and claude.ai

Those two do not read a skills directory. They take a **ZIP upload** under
Customize > Skills, so build one:

```bash
curl -fsSL https://raw.githubusercontent.com/anonalabs/Anona-Memory-SDK/main/skills/install.sh | bash -s -- --zip
```

That writes `anona-memory.zip`, `anona-memory-sdk.zip` and `anona-cli.zip` into
the current directory, with the skill folder as the archive root, which is what
the upload expects. Every `name` and `description` is inside the caps the upload
enforces (64 and 200 characters).

There is no shell in that surface, so the CLI and `curl` fallbacks (and the
`anona-cli` skill) cannot run there. Give it tools by adding Anona as a **custom connector** under
Settings > Connectors:

```
https://memory.anonalabs.com/mcp
```

Sign in with OAuth when prompted. The Claude Code that runs *inside* Claude
Desktop is a different surface and reads `~/.claude/skills/` normally, so the
installer above already covers it.

## What the skill calls

In order of preference:

1. **MCP tools** (`record`, `retrieve`, `reason`, `list_spaces`) if the host
   exposes them. Nothing to configure.
   ```bash
   claude mcp add --transport http anona https://memory.anonalabs.com/mcp \
     --header "Authorization: Bearer $ANONA_API_KEY"
   ```
2. **The `anona` CLI** (pip `anona` 0.24.0+): `anona status` once, then
   `anona retrieve` / `record` / `reason` with `--json`, branching on the exit
   code, and `anona guide --json` before any command it has not used. It uses
   an API key, the command-line login from `anona login`, or a temporary
   profile, never the MCP login. The `anona-cli` skill teaches the rest of the
   CLI.
3. **`curl`** against `https://api.anonalabs.com`, reading `ANONA_API_KEY` from
   the environment or `~/.anona/config.env`.
4. **Nothing.** The skill may run `anona start` for a temporary profile, or
   says so once and gets on with the task. A memory layer that blocks work is
   worse than no memory layer.

## Hooks (Claude Code)

The Claude Code plugin ships hooks that keep Anona in sync without the agent
having to remember to:

| Event | What it does |
| --- | --- |
| `SessionStart` | Pulls what the space knows about the repo, branch and recent commits into context. |
| `PreCompact` | Pushes a redacted excerpt of the conversation about to be compacted (prompts and assistant prose, no tool output) as an async record. |
| `SessionStart` (`compact`) | Prompts the agent to record the facts the summary kept. |
| `SessionEnd` | Pushes a work log: task, branch, files changed, commits made. |

`hooks/anona_sync.py` is standard-library Python 3 and needs no key of its
own. With `ANONA_API_KEY` (environment or `~/.anona/config.env`) it calls the
REST API; without one it speaks MCP over stdio to `anona mcp`, the CLI's bridge,
which holds and refreshes the login from `anona login`. With neither it is a
silent no-op. It never blocks the session: recall is an async hook, and pushes
run in a detached child. With more than
one space visible and no `ANONA_SPACE_ID`, the pull sends `route: "auto"` with
the repository's space as the fallback. The pushes write only to an own space
(the repository's, once it exists, or `ANONA_SPACE_ID`), route only with
`ANONA_HOOK_ROUTE=auto`, and are skipped while the repository has no space of
its own: they carry raw session text, and routing or a bare name could file it
in a space other people read. Switches: `ANONA_HOOKS=off`,
`ANONA_HOOK_RECALL=0`, `ANONA_HOOK_DIGEST=0`, `ANONA_HOOK_WORKLOG=0`,
`ANONA_HOOK_ROUTE=auto|off`, `ANONA_HOOK_TRANSPORT=rest|mcp`.

The installer and `npx skills` do not register hooks; see
[the docs](https://docs.anonalabs.com/integrations/skills#hooks) to add them
by hand.

## Layout

```
skills/
  install.sh
  anona-memory/
    SKILL.md
    references/{usage,what-to-record,troubleshooting}.md
  anona-memory-sdk/
    SKILL.md
    references/{python,typescript,rest-and-errors,scoping}.md
  anona-cli/
    SKILL.md
    references/{commands,scripting,credentials}.md
```

`SKILL.md` carries YAML frontmatter with `name` and `description`. The
description is what decides whether the skill fires, so it names the trigger
words rather than summarizing the contents. Everything else loads only when the
agent opens it, which is why the depth lives under `references/`.

## Developing

Point the installer at a checkout instead of GitHub:

```bash
ANONA_SKILLS_SRC=$PWD/skills ./skills/install.sh --app claude --no-mcp
```

Restart the agent afterwards. Skills load at session start.
