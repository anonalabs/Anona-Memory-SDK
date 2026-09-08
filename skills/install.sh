#!/usr/bin/env bash
# Install the Anona Memory agent skills.
#
#   curl -fsSL https://raw.githubusercontent.com/anonalabs/Anona-Memory-SDK/main/skills/install.sh | bash
#
# Run --help for options. Nothing is installed outside the directories printed
# at the end, and no shell profile is edited.
set -euo pipefail

REPO="${ANONA_SKILLS_REPO:-anonalabs/Anona-Memory-SDK}"
REF="${ANONA_SKILLS_REF:-main}"
MCP_URL="https://memory.anonalabs.com/mcp"
API_URL="https://api.anonalabs.com"
CONFIG_DIR="${HOME}/.anona"
CONFIG_FILE="${CONFIG_DIR}/config.env"

ALL_SKILLS="anona-memory anona-memory-sdk"

# agent:config-root:skills-subpath. The agent is offered only when its config
# root already exists, so nothing is written into a directory no tool reads.
AGENTS="
claude:${HOME}/.claude:skills
codex:${HOME}/.codex:skills
opencode:${HOME}/.opencode:skills
cursor:${HOME}/.cursor:skills
hermes:${HOME}/.hermes:skills
"

APP=""
SKILLS=""
SCOPE="global"
TARGET_DIR=""
API_KEY="${ANONA_API_KEY:-}"
SPACE_ID="${ANONA_SPACE_ID:-}"
WITH_MCP=1
FORCE=0
UNINSTALL=0
ZIP_MODE=0
ZIP_DIR=""

say()  { printf '%s\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Install the Anona Memory agent skills.

Usage:
  install.sh [options]

Options:
  --app <name|all>     claude, codex, opencode, cursor, hermes, or all.
                       Default: every one already present on this machine.
  --skill <name>       anona-memory or anona-memory-sdk. Repeatable.
                       Default: both.
  --project            Install into ./.claude/skills instead of ~/.claude/skills.
  --dir <path>         Install into this directory and nowhere else.
  --api-key <key>      An anona_live_ or anona_test_ key. Prompted for if absent.
  --space <id>         Default space id to record into.
  --no-mcp             Do not register the MCP server.
  --ref <git-ref>      Branch or tag to install from. Default: main.
  --force              Overwrite an existing install without backing it up.
  --uninstall          Remove the skills instead of installing them.
  --zip [dir]          Build one .zip per skill instead of installing, for
                       upload to Claude Desktop or claude.ai under
                       Customize > Skills. Default dir: the current one.
  -h, --help           This text.

Other harnesses are covered by the community CLI:
  npx skills add anonalabs/Anona-Memory-SDK
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --app)       APP="${2:-}"; shift 2 ;;
    --skill)     SKILLS="${SKILLS} ${2:-}"; shift 2 ;;
    --project)   SCOPE="project"; shift ;;
    --dir)       TARGET_DIR="${2:-}"; shift 2 ;;
    --api-key)   API_KEY="${2:-}"; shift 2 ;;
    --space)     SPACE_ID="${2:-}"; shift 2 ;;
    --no-mcp)    WITH_MCP=0; shift ;;
    --ref)       REF="${2:-}"; shift 2 ;;
    --force)     FORCE=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --zip)       ZIP_MODE=1
                 case "${2:-}" in ""|-*) shift ;; *) ZIP_DIR="$2"; shift 2 ;; esac ;;
    -h|--help)   usage; exit 0 ;;
    *)           die "unknown option: $1 (try --help)" ;;
  esac
done

[ -n "${SKILLS// /}" ] || SKILLS="$ALL_SKILLS"
for s in $SKILLS; do
  case " $ALL_SKILLS " in *" $s "*) ;; *) die "unknown skill: $s" ;; esac
done

case "${TARGET_DIR}" in
  *[[:space:]]*) die "--dir cannot contain whitespace" ;;
esac

command -v curl >/dev/null 2>&1 || die "curl is required"
command -v tar  >/dev/null 2>&1 || die "tar is required"

# ── where to install ─────────────────────────────────────────────────────────

targets=""

if [ "$ZIP_MODE" = "1" ]; then
  # A zip is not installed anywhere, so nothing below about agents applies.
  ZIP_DIR="${ZIP_DIR:-$(pwd)}"
  case "$ZIP_DIR" in
    *[[:space:]]*) die "--zip directory cannot contain whitespace" ;;
  esac
fi

add_target() {
  case " $targets " in *" $1 "*) ;; *) targets="${targets} $1" ;; esac
}

if [ "$ZIP_MODE" = "1" ]; then
  :
elif [ -n "$TARGET_DIR" ]; then
  add_target "$TARGET_DIR"
elif [ "$SCOPE" = "project" ]; then
  add_target "$(pwd)/.claude/skills"
else
  for row in $AGENTS; do
    name="${row%%:*}"; rest="${row#*:}"
    root="${rest%%:*}"; sub="${rest#*:}"
    if [ -n "$APP" ] && [ "$APP" != "all" ] && [ "$APP" != "$name" ]; then
      continue
    fi
    if [ -d "$root" ] || [ "$APP" = "$name" ]; then
      add_target "${root}/${sub}"
    fi
  done
fi

if [ -z "${targets// /}" ] && [ "$ZIP_MODE" != "1" ]; then
  say "No supported agent found on this machine."
  say "Name one explicitly, for example:  install.sh --app claude"
  say "Or install anywhere with:          install.sh --dir /path/to/skills"
  exit 1
fi

# ── uninstall ────────────────────────────────────────────────────────────────

if [ "$UNINSTALL" = "1" ]; then
  for dir in $targets; do
    for s in $SKILLS; do
      if [ -d "${dir}/${s}" ]; then
        rm -rf "${dir:?}/${s}"
        info "removed ${dir}/${s}"
      fi
    done
  done
  say "Done. ${CONFIG_FILE} was left in place; delete it by hand if you want the key gone."
  exit 0
fi

# ── fetch ────────────────────────────────────────────────────────────────────

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

src=""
if [ -n "${ANONA_SKILLS_SRC:-}" ]; then
  # A local skills/ directory. Used for offline installs and for testing this
  # script against a checkout rather than against a published ref.
  [ -d "$ANONA_SKILLS_SRC" ] || die "ANONA_SKILLS_SRC is not a directory: ${ANONA_SKILLS_SRC}"
  src="$ANONA_SKILLS_SRC"
  say "Installing from ${src} ..."
else
  say "Fetching skills from ${REPO}@${REF} ..."
  if ! curl -fsSL "https://codeload.github.com/${REPO}/tar.gz/${REF}" \
       | tar -xz -C "$tmp" 2>/dev/null; then
    die "could not download ${REPO}@${REF}"
  fi
  for candidate in "$tmp"/*/skills; do
    if [ -d "$candidate" ]; then src="$candidate"; break; fi
  done
  [ -n "$src" ] || die "the downloaded archive has no skills/ directory"
fi

for s in $SKILLS; do
  [ -f "${src}/${s}/SKILL.md" ] || die "${s} is missing from ${REPO}@${REF}"
done

# ── zip ──────────────────────────────────────────────────────────────────────

if [ "$ZIP_MODE" = "1" ]; then
  mkdir -p "$ZIP_DIR"
  for s in $SKILLS; do
    out="${ZIP_DIR}/${s}.zip"
    rm -f "$out"
    # The archive root must be the skill folder itself, not a parent, or the
    # upload rejects it.
    if command -v zip >/dev/null 2>&1; then
      ( cd "$src" && zip -qr "$out" "$s" )
    elif command -v python3 >/dev/null 2>&1; then
      python3 - "$src" "$out" "$s" <<'PYZIP'
import pathlib, sys, zipfile
src, out, name = sys.argv[1:4]
root = pathlib.Path(src) / name
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for f in sorted(root.rglob("*")):
        if f.is_file():
            z.write(f, pathlib.Path(name) / f.relative_to(root))
PYZIP
    else
      die "either zip or python3 is required to build a zip"
    fi
    info "wrote ${out}"
  done
  say ""
  say "Upload these under Customize > Skills in Claude Desktop or claude.ai."
  say "For the tools they call, add the Anona connector in Settings > Connectors:"
  say "  ${MCP_URL}"
  exit 0
fi

# ── install ──────────────────────────────────────────────────────────────────

installed=""
backup=""
stamp="$(date +%Y%m%d%H%M%S)"

for dir in $targets; do
  mkdir -p "$dir"
  for s in $SKILLS; do
    dest="${dir}/${s}"
    if [ -e "$dest" ] && [ "$FORCE" != "1" ]; then
      # Not alongside it: an agent reads every directory under skills/, so a
      # backup left there loads as a second skill claiming the same name.
      backup="${CONFIG_DIR}/backups/${stamp}${dir}"
      mkdir -p "$backup"
      mv "$dest" "${backup}/${s}"
      info "previous ${s} moved to ${backup}/${s}"
    else
      rm -rf "$dest"
    fi
    cp -R "${src}/${s}" "$dest"
    installed="${installed} ${dest}"
    info "installed ${dest}"
  done
done

# ── credentials ──────────────────────────────────────────────────────────────

if [ -z "$API_KEY" ] && [ -r "$CONFIG_FILE" ]; then
  # shellcheck disable=SC1090
  . "$CONFIG_FILE" || true
  API_KEY="${ANONA_API_KEY:-}"
fi

# /dev/tty exists even with no controlling terminal, and only opening it says
# whether a human is there to answer. `curl | bash` holds the script on stdin,
# so stdin is never the test.
if [ -z "$API_KEY" ] && { exec 3</dev/tty; } 2>/dev/null; then
  say ""
  say "Paste an API key from https://memory.anonalabs.com (API keys), or press enter to skip."
  printf 'API key: '
  read -r API_KEY <&3 || API_KEY=""
  exec 3<&-
fi

key_ok=""
if [ -n "$API_KEY" ]; then
  case "$API_KEY" in
    anona_live_*|anona_test_*) ;;
    *) warn "that does not look like an Anona key (expected anona_live_ or anona_test_)" ;;
  esac
  status="$(curl -sS -o /dev/null -w '%{http_code}' \
              -H "Authorization: Bearer ${API_KEY}" \
              "${API_URL}/v1/spaces/" 2>/dev/null || true)"
  status="${status:-000}"
  case "$status" in
    200) key_ok=1; info "key verified" ;;
    401) warn "the API rejected that key (401). Installing anyway." ;;
    000) warn "could not reach ${API_URL} to verify the key. Installing anyway." ;;
    *)   warn "unexpected status ${status} verifying the key. Installing anyway." ;;
  esac

  umask 077
  mkdir -p "$CONFIG_DIR"
  {
    printf '# Written by the Anona skills installer. Mode 600.\n'
    printf 'ANONA_API_KEY=%s\n' "$API_KEY"
    if [ -n "$SPACE_ID" ]; then printf 'ANONA_SPACE_ID=%s\n' "$SPACE_ID"; fi
  } > "$CONFIG_FILE"
  chmod 600 "$CONFIG_FILE"
  info "credentials written to ${CONFIG_FILE}"
fi

# ── MCP ──────────────────────────────────────────────────────────────────────

mcp_done=0
if [ "$WITH_MCP" = "1" ] && [ -n "$API_KEY" ] && command -v claude >/dev/null 2>&1; then
  if claude mcp list 2>/dev/null | grep -q '^anona\b'; then
    info "MCP server 'anona' already registered with Claude Code"
    mcp_done=1
  elif claude mcp add --transport http anona "$MCP_URL" \
         --header "Authorization: Bearer ${API_KEY}" >/dev/null 2>&1; then
    info "registered the 'anona' MCP server with Claude Code"
    mcp_done=1
  else
    warn "could not register the MCP server automatically"
  fi
fi

# ── report ───────────────────────────────────────────────────────────────────

say ""
say "Anona Memory skills installed."
say ""
for p in $installed; do say "  ${p}"; done
say ""

if [ "$mcp_done" = "0" ] && [ "$WITH_MCP" = "1" ]; then
  say "To give the skill tools to call, register the MCP server."
  say ""
  say "  Claude Code:"
  say "    claude mcp add --transport http anona ${MCP_URL} \\"
  say "      --header \"Authorization: Bearer \$ANONA_API_KEY\""
  say ""
  say "  Hermes Agent, in ~/.hermes/config.yaml:"
  say "    mcp_servers:"
  say "      anona:"
  say "        url: \"${MCP_URL}\""
  say "        headers:"
  say "          Authorization: \"Bearer \${env:ANONA_API_KEY}\""
  say ""
  say "  Anything else: point your client at ${MCP_URL} with that same header."
  say ""
fi

if [ -z "$API_KEY" ]; then
  say "No API key was set. Get one at https://memory.anonalabs.com under API keys, then:"
  say "  install.sh --api-key anona_live_..."
  say ""
elif [ -z "$key_ok" ]; then
  say "The key could not be verified. Check it at https://memory.anonalabs.com under API keys."
  say ""
fi

say "Skills load at session start, so restart your agent once."
say "Docs: https://docs.anonalabs.com/integrations/skills"
