"""`anona guide` (also `anona help --llm`): the CLI described for an agent.

Compact, stable plain text -- or, with --json, a machine-readable command
tree. Everything structural (commands, positionals, flags, their help, the
global flags, the exit codes) is generated from the argparse parser and
exits.py, so it cannot drift from what the CLI accepts. Only the prose, one
example per command and the shape of each command's --json output are
written by hand, and a test fails if any command lacks either, or if an
example does not parse.
"""

from __future__ import annotations

import argparse
import json

from . import exits
from .introspect import Command, as_dict, tree, usage

PURPOSE = (
    "anona is the command line for Anona Memory, a hosted memory layer for AI agents. "
    "It records facts into a space, retrieves them by meaning, reasons over them, and "
    "manages spaces, memories, documents, jobs and per-space settings. MCP is the "
    "primary surface for an agent; this CLI is for scripts, terminals and harnesses "
    "without MCP. Every data command takes --json (one JSON document on stdout; "
    "nothing on stdout when it fails) and writes errors to stderr as "
    "`anona: <code>: <message> (request_id <id>)`."
)

# What bare `anona` prints: where to start, for a person or an agent that has
# just found the command. Static on purpose -- no network and no credential
# read -- so it answers instantly and identically everywhere. The full
# reference is `anona guide`; this only points at it.
ORIENTATION = """\
anona: the command line for Anona Memory, a hosted memory layer for AI agents.

Start here
  anona start               use memory now: a temporary profile, no account
  anona login               sign in (also claims a temporary profile)
  anona status              what this machine is signed in as

Use memory (a temporary profile needs no --space)
  anona record "TEXT" --space SPACE
  anona retrieve "QUERY" --space SPACE
  anona reason "QUESTION" --space SPACE
  anona spaces list

Learn the rest
  anona guide               every command, flag, exit code and recipe
                            (an agent should read this first; --json for a tree)
  anona help COMMAND        help for one command
  anona doctor              check the install, credentials and connection

Docs: https://docs.anonalabs.com/cli/overview
"""

EPILOG = (
    "New here, or an agent? Run `anona guide` for every command, flag, exit code "
    "and recipe, or `anona` alone for where to start."
)


CREDENTIALS = (
    "--api-key KEY on the command line (visible in the process list; prefer the variable)",
    "ANONA_API_KEY in the environment",
    "ANONA_API_KEY in ~/.anona/config.env (or the active profile's config.env)",
    "the command-line login from `anona login --cli` (refreshed automatically)",
    "a temporary profile from `anona start` (no account; its one space is `default`)",
)

CREDENTIAL_NOTES = (
    "An API key or the command-line login reaches an account, which can hold many spaces: "
    "name one with --space or ANONA_SPACE_ID.",
    "The MCP login (`anona login --mcp`) is for `anona mcp` only; the REST API refuses it.",
    "An API key from the environment or config.env is only sent to api.anonalabs.com or "
    "memory.anonalabs.com unless ANONA_BASE_URL is set in the same place.",
    "--profile NAME (or ANONA_PROFILE) switches to a separate set of credentials and "
    "config.env under ~/.anona/profiles/NAME.",
    "`anona doctor` says which credential wins and why, without printing it.",
)

COMMON_ERRORS = (
    ("exit 4, `no credential`", "a data command found nothing to send; run `anona login --cli`, set ANONA_API_KEY, or `anona start`"),
    ("exit 2, `an API key needs a space`", "pass --space NAME or set ANONA_SPACE_ID; `anona spaces list` shows them"),
    ("exit 4, `the stored `anona login` credential is for MCP`", "run `anona login --cli` to sign the command line in"),
    ("exit 4, `refusing to send the API key ... to <host>`", "pass --api-key and --base-url together, or unset ANONA_BASE_URL"),
    ("exit 3 from record", "nothing was extracted; re-record with a sentence of context"),
    ("exit 5, `space_not_found`", "check the id with `anona spaces list` (a shared space may need owner:name; a temporary profile has only `default`)"),
    ("exit 5, `job_not_found`", "jobs get / --wait: that id is not a job in this space; check --space"),
    (
        "exit 4, `temp_profile_single_space`",
        "a temporary profile has only the `default` space: it cannot create spaces or write elsewhere; "
        "sign in with `anona login` for more",
    ),
    ("exit 4, `space_owner_only` / `space_read_only`", "the space is shared with you without that right; ask its owner"),
    ("exit 6, `rate_limited`", "wait; the CLI already retried the 429"),
    (
        "exit 6, `credits_exhausted`",
        "the balance is spent: reads (retrieve, reason, memories list, documents list, profile) spend credits "
        "too and are refused at zero; check `anona usage`",
    ),
    ("exit 7, `could not reach`", "check the network and `anona doctor`; a write that timed out may still have been stored"),
    ("exit 8 from --wait", "the job is still running; check later with `anona jobs get JOB_ID`"),
    ("exit 1, `unsupported_file_type` / `file_too_large`", "documents upload: the server's message names the supported types and limits"),
    ("exit 1 from `anona status`, `Not logged in`", "nothing is stored; status only reports that. Run `anona start` or `anona login`"),
    ("exit 1 from a second `anona start`", "a temporary profile already exists here; keep using it (`anona status` shows its deadline)"),
    ("exit 1 from `anona logout`", "it will not discard an unclaimed temporary profile; claim it with `anona login` first"),
    ("exit 1, `memory_not_editable`", "only facts and experiences can be edited; a note is rebuilt from them, so edit a fact it came from"),
    ("exit 1, `job_not_cancellable`", "the job has already finished; `anona jobs get JOB_ID` shows how it ended"),
    ("exit 1, `unsupported_model`", "that model id is not offered; `anona llm-models` lists the ones that are"),
)

# One example per command. Each must parse (a test runs every one through
# the real parser).
EXAMPLES: dict[str, str] = {
    "start": "anona start",
    "record": 'anona record "The team ships on Tuesdays." --space notes',
    "retrieve": 'anona retrieve "when do we ship" --space notes --json',
    "reason": 'anona reason "What is our release process?" --space notes --depth thorough',
    "spaces list": "anona spaces list --json",
    "spaces create": 'anona spaces create notes --description "Team notes"',
    "spaces get": "anona spaces get notes",
    "spaces delete": "anona spaces delete old-notes --yes",
    "memories list": "anona memories list --space notes --member-id me",
    "memories edit": 'anona memories edit MEMORY_ID --text "The team ships on Wednesdays." --space notes',
    "memories delete": "anona memories delete MEMORY_ID --yes --space notes",
    "memories history": "anona memories history MEMORY_ID --space notes",
    "documents upload": "anona documents upload report.pdf notes.md --space notes --wait",
    "documents list": "anona documents list --space notes --source file",
    "documents get": "anona documents get DOCUMENT_ID --space notes",
    "documents delete": "anona documents delete DOCUMENT_ID --yes --space notes",
    "jobs get": "anona jobs get JOB_ID --space notes",
    "jobs cancel": "anona jobs cancel JOB_ID --yes --space notes",
    "profile": "anona profile --space notes",
    "model": "anona model MODEL_ID --space notes",
    "user-profile": "anona user-profile alice --space notes",
    "usage": "anona usage",
    "llm-models": "anona llm-models --json",
    "chat-settings get": "anona chat-settings get --space notes",
    "chat-settings set": "anona chat-settings set --memory-limit 5 --space notes",
    "chat-settings reset": "anona chat-settings reset --yes --space notes",
    "reason-settings get": "anona reason-settings get --space notes",
    "reason-settings set": "anona reason-settings set --depth thorough --space notes",
    "reason-settings reset": "anona reason-settings reset --yes --space notes",
    "extraction-settings get": "anona extraction-settings get --space notes",
    "extraction-settings set": "anona extraction-settings set --mode verbose --space notes",
    "extraction-settings reset": "anona extraction-settings reset --yes --space notes",
    "login": "anona login --cli",
    "logout": "anona logout --cli",
    "status": "anona status",
    "mcp": "anona mcp",
    "doctor": "anona doctor --space notes",
    "guide": "anona guide --json",
    "help": "anona help spaces delete",
    "completion": "anona completion bash",
}

# What --json prints, per command (the API's response unless noted).
JSON_SHAPES: dict[str, str] = {
    "start": "no --json; prints the profile's deadline",
    "record": '{"memory_id","memory_ids":[...],"status","message"?,"usage","routed_to"(null on an addressed write)}; --async: the same keys, with "job_id" set and "status":"processing"; '
    '--batch: {"job_id","job_ids"(null unless the batch was split),"status","accepted"}; with --wait: {"submitted":<that>,"jobs":[<jobs get>...]}',
    "retrieve": '{"results":[{"memory_id","content","relevance_score","memory_type","tags",...}]}; '
    '--format block: {"results":[...],"context","token_estimate","usage"}',
    "reason": '{"insights","sources":{"models","memories","notes"},"rules_applied":[...],"model","usage"}',
    "spaces list": '{"spaces":[{"space_id","name","description","qualified_id","shared_by",...}],"total"}',
    "spaces create": '{"space_id","name","description","created_at","space_type",...}',
    "spaces get": '{"space_id","name","description","created_at","qualified_id","shared_by"}',
    "spaces delete": '{"deleted":true,"space_id"} (written by the CLI; the API answers 204)',
    "memories list": '{"items":[{"id","content","type","state","tags","source_ids",...}],"total","limit","offset"}',
    "memories edit": '{"id","content","type","state",...} (the edited memory)',
    "memories delete": '{"deleted":true,"memory_id","space_id"} (written by the CLI; the API answers 204)',
    "memories history": '{"memory_id","history":[{"previous_content","changed_at","reason"}]}',
    "documents upload": '{"job_ids":[...]}; with --wait: {"submitted":<that>,"jobs":[<jobs get>...]}',
    "documents list": '{"documents":[{"document_id","source","created_at","memory_count","tags"}],"total","limit","offset"}',
    "documents get": "the document's metadata object, as the API returns it",
    "documents delete": '{"deleted":true,"document_id","space_id"} (written by the CLI; the API answers 204)',
    "jobs get": '{"job_id","status","created_at","completed_at","error","memory_count","memory_ids","items_submitted","zero_yield_items","progress"}; '
    "zero_yield_items = items extraction produced no memory from (non-zero is possible even when "
    "memory_count >= items_submitted: one item can make several memories and another none)",
    "jobs cancel": '{"job_id","status","cancelled_parts","running_parts"}',
    "profile": '{"space_id","name","mission","disposition":{...}}',
    "model": '{"id","space_id","name","query","content","tags",...}',
    "user-profile": '{"space_id","user_id","memory_count","first_seen","last_active","memories":[...]}',
    "usage": '{"credits_remaining","credits_limit","credits_used","credits_banked","rate_limit_per_min",...}',
    "llm-models": '{"object":"list","data":[{"id","display_name","alias","default","credits_per_1k_input",'
    '"credits_per_1k_output","provider","supports_memory_operations",...}]}',
    "chat-settings get": '{"space_id","memory_limit","memory_token_budget","auto_record","memory","chat_model"} (null = platform default)',
    "chat-settings set": "the stored settings after the write, same shape as get",
    "chat-settings reset": '{"reset":true,"space_id","settings"} (written by the CLI; the API answers 204)',
    "reason-settings get": '{"space_id","model","depth"} (null = platform default)',
    "reason-settings set": "the stored settings after the write, same shape as get",
    "reason-settings reset": '{"reset":true,"space_id","settings"} (written by the CLI; the API answers 204)',
    "extraction-settings get": '{"space_id","mode","guidance","custom_prompt","labels","free_form_entities"} (null = platform default)',
    "extraction-settings set": "the stored settings after the write, same shape as get",
    "extraction-settings reset": '{"reset":true,"space_id","settings"} (written by the CLI; the API answers 204)',
    "login": "no --json",
    "logout": "no --json",
    "status": "no --json",
    "mcp": "no --json; stdout is the MCP client's JSON-RPC stream",
    "doctor": '{"anona_version","python","profile","profile_dir","credential":{"kind","display","source","why","base_url",...},'
    '"health":{"ok","status"},"space":{"id","source","accessible","error"},'
    '"credits":{"remaining","limit","used","exhausted","error"},"ok"}',
    "guide": '{"purpose","credentials","global_options","commands":[...],"exit_codes":[...],"common_errors":[...]}',
    "help": "no --json without --llm; `help --llm --json` is `guide --json`",
    "completion": "no --json; prints a shell script",
}


def _flag_text(opt) -> str:
    flags = ", ".join(opt.flags)
    if opt.takes_value:
        flags += " " + ("{" + ",".join(opt.choices) + "}" if opt.choices else (opt.metavar or "VALUE"))
    return flags


def text(parser: argparse.ArgumentParser) -> str:
    root = tree(parser)
    out: list[str] = ["ANONA CLI REFERENCE (for agents)", "", PURPOSE, "", "CREDENTIALS (first match wins)"]
    out += [f"  {i}. {c}" for i, c in enumerate(CREDENTIALS, 1)]
    out += [f"  - {n}" for n in CREDENTIAL_NOTES]
    out += ["", "GLOBAL OPTIONS (before or after any command)"]
    out += [f"  {_flag_text(o)}  {o.help}" for o in root.options]
    out += ["", "COMMANDS"]
    for cmd in root.leaves():
        out.append(f"  {usage(cmd)}")
        if cmd.help:
            out.append(f"    {cmd.help}")
        for pos in cmd.positionals:
            choices = f" {{{','.join(pos.choices)}}}" if pos.choices else ""
            out.append(f"    {pos.name}{choices}  {pos.help}")
        for opt in cmd.options:
            out.append(f"    {_flag_text(opt)}  {opt.help}")
        out.append(f"    example: {EXAMPLES.get(cmd.name, '-')}")
        out.append(f"    --json: {JSON_SHAPES.get(cmd.name, '-')}")
    out += ["", "EXIT CODES"]
    out += [f"  {code}  {name}: {meaning}" for code, name, meaning in exits.EXIT_CODES]
    out += ["  141  the reader closed the pipe (e.g. `anona guide | head`); output stops quietly"]
    out += ["", "COMMON ERRORS"]
    out += [f"  {err}: {fix}" for err, fix in COMMON_ERRORS]
    return "\n".join(out)


def document(parser: argparse.ArgumentParser) -> dict:
    root = tree(parser)

    def leafify(cmd: Command) -> dict:
        d = as_dict(cmd)
        if not cmd.children:
            d["example"] = EXAMPLES.get(cmd.name)
            d["json_output"] = JSON_SHAPES.get(cmd.name)
        d["subcommands"] = [leafify(c) for c in cmd.children]
        return d

    return {
        "purpose": PURPOSE,
        "credentials": list(CREDENTIALS),
        "credential_notes": list(CREDENTIAL_NOTES),
        "global_options": [o.__dict__ for o in root.options],
        "commands": [leafify(c) for c in root.children],
        "exit_codes": [{"code": c, "name": n, "meaning": m} for c, n, m in exits.EXIT_CODES],
        "common_errors": [{"error": e, "fix": f} for e, f in COMMON_ERRORS],
        "broken_pipe_exit": 141,
    }


def run(parser: argparse.ArgumentParser, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(document(parser), ensure_ascii=True))
    else:
        print(text(parser))
    return 0
