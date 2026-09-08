# Anona Memory agent skills

Two [Agent Skills](https://docs.anonalabs.com/integrations/skills) that teach a
coding agent to use Anona Memory without being told to, in whichever harness you
already use.

| Skill | For | Fires when |
| --- | --- | --- |
| `anona-memory` | The agent's own memory. | A task starts, a task ends, or the user says remember, recall, or refers to an earlier session. |
| `anona-memory-sdk` | Writing code against Anona. | Anything touching the SDK, the REST API, scoping, or a framework adapter. |

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

That writes `anona-memory.zip` and `anona-memory-sdk.zip` into the current
directory, with the skill folder as the archive root, which is what the upload
expects. Both `name` and `description` are inside the caps the upload enforces
(64 and 200 characters).

There is no shell in that surface, so the skill's `curl` fallback cannot run
there. Give it tools by adding Anona as a **custom connector** under
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
2. **`curl`** against `https://api.anonalabs.com`, reading `ANONA_API_KEY` from
   the environment or `~/.anona/config.env`.
3. **Nothing.** The skill says so once and gets on with the task. A memory layer
   that blocks work is worse than no memory layer.

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
