/**
 * `anona guide` (also `anona help --llm`): the CLI described for an agent.
 * Mirrors anona/cli/guide.py, and prints the same document.
 *
 * Compact, stable plain text -- or, with --json, a machine-readable command
 * tree. Everything structural (commands, positionals, flags, their help, the
 * global flags, the exit codes) is generated from the command registry
 * (args.ts) and exits.ts, so it cannot drift from what the CLI accepts. Only
 * the prose, one example per command and the shape of each command's --json
 * output are written by hand, and a test fails if any command lacks either.
 */
import { commandName, leaves, tree, type TreeCommand, type TreeOption } from "./args.js";
import { BROKEN_PIPE_EXIT, EXIT_CODES } from "./exits.js";
import { pyJson } from "./py.js";

export const PURPOSE =
  "anona is the command line for Anona Memory, a hosted memory layer for AI agents. " +
  "It records facts into a space, retrieves them by meaning, reasons over them, and " +
  "manages spaces, memories, documents, jobs and per-space settings. MCP is the " +
  "primary surface for an agent; this CLI is for scripts, terminals and harnesses " +
  "without MCP. Every data command takes --json (one JSON document on stdout; " +
  "nothing on stdout when it fails) and writes errors to stderr as " +
  "`anona: <code>: <message> (request_id <id>)`.";

// What bare `anona` prints: where to start, for a person or an agent that has
// just found the command. Static on purpose -- no network and no credential
// read -- so it answers instantly and identically everywhere. The full
// reference is `anona guide`; this only points at it. Same text as guide.py.
export const ORIENTATION =
  "anona: the command line for Anona Memory, a hosted memory layer for AI agents.\n" +
  "\n" +
  "Start here\n" +
  "  anona start               use memory now: a temporary profile, no account\n" +
  "  anona login               sign in (also claims a temporary profile)\n" +
  "  anona status              what this machine is signed in as\n" +
  "\n" +
  "Use memory (a temporary profile needs no --space)\n" +
  "  anona record \"TEXT\" --space SPACE\n" +
  "  anona retrieve \"QUERY\" --space SPACE\n" +
  "  anona reason \"QUESTION\" --space SPACE\n" +
  "  anona spaces list\n" +
  "\n" +
  "Learn the rest\n" +
  "  anona guide               every command, flag, exit code and recipe\n" +
  "                            (an agent should read this first; --json for a tree)\n" +
  "  anona help COMMAND        help for one command\n" +
  "  anona doctor              check the install, credentials and connection\n" +
  "\n" +
  "Docs: https://docs.anonalabs.com/cli/overview\n";

export const CREDENTIALS = [
  "--api-key KEY on the command line (visible in the process list; prefer the variable)",
  "ANONA_API_KEY in the environment",
  "ANONA_API_KEY in ~/.anona/config.env (or the active profile's config.env)",
  "the command-line login from `anona login --cli` (refreshed automatically)",
  "a temporary profile from `anona start` (no account; its one space is `default`)",
];

export const CREDENTIAL_NOTES = [
  "An API key or the command-line login reaches an account, which can hold many spaces: " +
    "name one with --space or ANONA_SPACE_ID.",
  "The MCP login (`anona login --mcp`) is for `anona mcp` only; the REST API refuses it.",
  "An API key from the environment or config.env is only sent to api.anonalabs.com or " +
    "memory.anonalabs.com unless ANONA_BASE_URL is set in the same place.",
  "--profile NAME (or ANONA_PROFILE) switches to a separate set of credentials and " +
    "config.env under ~/.anona/profiles/NAME.",
  "`anona doctor` says which credential wins and why, without printing it.",
];

export const COMMON_ERRORS: ReadonlyArray<readonly [string, string]> = [
  ["exit 4, `no credential`", "run `anona login --cli`, set ANONA_API_KEY, or `anona start`"],
  ["exit 2, `an API key needs a space`", "pass --space NAME or set ANONA_SPACE_ID; `anona spaces list` shows them"],
  ["exit 4, `the stored `anona login` credential is for MCP`", "run `anona login --cli` to sign the command line in"],
  ["exit 4, `refusing to send the API key ... to <host>`", "pass --api-key and --base-url together, or unset ANONA_BASE_URL"],
  ["exit 3 from record", "nothing was extracted; re-record with a sentence of context"],
  ["exit 5, `space_not_found`", "check the id with `anona spaces list` (a shared space may need owner:name; a temporary profile has only `default`)"],
  ["exit 5, `job_not_found`", "jobs get / --wait: that id is not a job in this space; check --space"],
  [
    "exit 4, `temp_profile_single_space`",
    "a temporary profile has only the `default` space: it cannot create spaces or write elsewhere; " +
      "sign in with `anona login` for more",
  ],
  ["exit 4, `space_owner_only` / `space_read_only`", "the space is shared with you without that right; ask its owner"],
  ["exit 6, `rate_limited`", "wait; the CLI already retried the 429"],
  [
    "exit 6, `credits_exhausted`",
    "the balance is spent: reads (retrieve, reason, memories list, documents list, profile) spend credits " +
      "too and are refused at zero; check `anona usage`",
  ],
  ["exit 7, `could not reach`", "check the network and `anona doctor`; a write that timed out may still have been stored"],
  ["exit 8 from --wait", "the job is still running; check later with `anona jobs get JOB_ID`"],
  ["exit 1, `unsupported_file_type` / `file_too_large`", "documents upload: the server's message names the supported types and limits"],
];

/** One example per command. Each must parse (a test runs every one through the parser). */
export const EXAMPLES: Record<string, string> = {
  start: "anona start",
  record: 'anona record "The team ships on Tuesdays." --space notes',
  retrieve: 'anona retrieve "when do we ship" --space notes --json',
  reason: 'anona reason "What is our release process?" --space notes --depth thorough',
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
  profile: "anona profile --space notes",
  model: "anona model MODEL_ID --space notes",
  "user-profile": "anona user-profile alice --space notes",
  usage: "anona usage",
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
  login: "anona login --cli",
  logout: "anona logout --cli",
  status: "anona status",
  mcp: "anona mcp",
  doctor: "anona doctor --space notes",
  guide: "anona guide --json",
  help: "anona help spaces delete",
  completion: "anona completion bash",
};

/** What --json prints, per command (the API's response unless noted). */
export const JSON_SHAPES: Record<string, string> = {
  start: "no --json; prints the profile's deadline",
  record:
    '{"memory_id","memory_ids":[...],"status","message"?,"usage"}; --async: {"job_id","status"}; ' +
    '--batch: {"job_id","job_ids","status","accepted"}; with --wait: {"submitted":<that>,"jobs":[<jobs get>...]}',
  retrieve:
    '{"results":[{"memory_id","content","relevance_score","memory_type","tags",...}]}; ' +
    '--format block: {"results":[...],"context","token_estimate"}',
  reason: '{"insights","sources":{"models","memories","notes"},"rules_applied":[...],"model","usage"}',
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
  "jobs get": '{"job_id","status","created_at","completed_at","error","memory_count","memory_ids","items_submitted","zero_yield_items","progress"}; ' +
    "zero_yield_items = items extraction produced no memory from (non-zero is possible even when " +
    "memory_count >= items_submitted: one item can make several memories and another none)",
  "jobs cancel": '{"job_id","status","cancelled_parts","running_parts"}',
  profile: '{"space_id","name","mission","disposition":{...}}',
  model: '{"id","space_id","name","query","content","tags",...}',
  "user-profile": '{"space_id","user_id","memory_count","first_seen","last_active","memories":[...]}',
  usage: '{"credits_remaining","credits_limit","credits_used","credits_banked","rate_limit_per_min",...}',
  "llm-models":
    '{"object":"list","data":[{"id","display_name","alias","default","credits_per_1k_input",' +
    '"credits_per_1k_output","provider","supports_memory_operations",...}]}',
  "chat-settings get": '{"space_id","memory_limit","memory_token_budget","auto_record","memory","chat_model"} (null = platform default)',
  "chat-settings set": "the stored settings after the write, same shape as get",
  "chat-settings reset": '{"reset":true,"space_id","settings"} (written by the CLI; the API answers 204)',
  "reason-settings get": '{"space_id","model","depth"} (null = platform default)',
  "reason-settings set": "the stored settings after the write, same shape as get",
  "reason-settings reset": '{"reset":true,"space_id","settings"} (written by the CLI; the API answers 204)',
  "extraction-settings get":
    '{"space_id","mode","guidance","custom_prompt","labels","free_form_entities"} (null = platform default)',
  "extraction-settings set": "the stored settings after the write, same shape as get",
  "extraction-settings reset": '{"reset":true,"space_id","settings"} (written by the CLI; the API answers 204)',
  login: "no --json",
  logout: "no --json",
  status: "no --json",
  mcp: "no --json; stdout is the MCP client's JSON-RPC stream",
  // The one legitimate difference from the pip package's guide: this CLI runs
  // on Node, so its doctor reports the Node version where pip's reports Python.
  doctor:
    '{"anona_version","node","profile","profile_dir","credential":{"kind","display","source","why","base_url",...},' +
    '"health":{"ok","status"},"space":{"id","source","accessible","error"},' +
    '"credits":{"remaining","limit","used","exhausted","error"},"ok"}',
  guide: '{"purpose","credentials","global_options","commands":[...],"exit_codes":[...],"common_errors":[...]}',
  help: "no --json without --llm; `help --llm --json` is `guide --json`",
  completion: "no --json; prints a shell script",
};

function flagText(o: TreeOption): string {
  let flags = o.flags.join(", ");
  if (o.takes_value) flags += " " + (o.choices ? `{${o.choices.join(",")}}` : (o.metavar ?? "VALUE"));
  return flags;
}

/** `anona <path> POSITIONALS required-options [options]`. */
export function usage(cmd: TreeCommand): string {
  const parts = ["anona", ...cmd.path];
  for (const pos of cmd.positionals) {
    const token = pos.name + (pos.many ? "..." : "");
    parts.push(pos.optional ? `[${token}]` : token);
  }
  for (const o of cmd.options) {
    if (o.required) parts.push(o.takes_value ? `${o.flags[0]} ${o.metavar}` : (o.flags[0] as string));
  }
  if (cmd.options.some((o) => !o.required)) parts.push("[options]");
  return parts.join(" ");
}

export function text(): string {
  const root = tree();
  const out: string[] = ["ANONA CLI REFERENCE (for agents)", "", PURPOSE, "", "CREDENTIALS (first match wins)"];
  CREDENTIALS.forEach((c, i) => out.push(`  ${i + 1}. ${c}`));
  for (const n of CREDENTIAL_NOTES) out.push(`  - ${n}`);
  out.push("", "GLOBAL OPTIONS (before or after any command)");
  for (const o of root.options) out.push(`  ${flagText(o)}  ${o.help}`);
  out.push("", "COMMANDS");
  for (const cmd of leaves(root)) {
    const name = commandName(cmd);
    out.push(`  ${usage(cmd)}`);
    if (cmd.help) out.push(`    ${cmd.help}`);
    for (const pos of cmd.positionals) {
      const choices = pos.choices ? ` {${pos.choices.join(",")}}` : "";
      out.push(`    ${pos.name}${choices}  ${pos.help}`);
    }
    for (const o of cmd.options) out.push(`    ${flagText(o)}  ${o.help}`);
    out.push(`    example: ${EXAMPLES[name] ?? "-"}`);
    out.push(`    --json: ${JSON_SHAPES[name] ?? "-"}`);
  }
  out.push("", "EXIT CODES");
  for (const [code, name, meaning] of EXIT_CODES) out.push(`  ${code}  ${name}: ${meaning}`);
  out.push(`  ${BROKEN_PIPE_EXIT}  the reader closed the pipe (e.g. \`anona guide | head\`); output stops quietly`);
  out.push("", "COMMON ERRORS");
  for (const [err, fix] of COMMON_ERRORS) out.push(`  ${err}: ${fix}`);
  return out.join("\n");
}

function asDict(cmd: TreeCommand): Record<string, unknown> {
  const d: Record<string, unknown> = {
    name: commandName(cmd),
    path: cmd.path,
    help: cmd.help,
    positionals: cmd.positionals,
    options: cmd.options,
    subcommands: null,
  };
  if (!cmd.children.length) {
    d.example = EXAMPLES[commandName(cmd)] ?? null;
    d.json_output = JSON_SHAPES[commandName(cmd)] ?? null;
  }
  d.subcommands = cmd.children.map(asDict);
  return d;
}

/** The --json document, key for key the pip package's. */
export function document(): Record<string, unknown> {
  const root = tree();
  return {
    purpose: PURPOSE,
    credentials: CREDENTIALS,
    credential_notes: CREDENTIAL_NOTES,
    global_options: root.options,
    commands: root.children.map(asDict),
    exit_codes: EXIT_CODES.map(([code, name, meaning]) => ({ code, name, meaning })),
    common_errors: COMMON_ERRORS.map(([error, fix]) => ({ error, fix })),
    broken_pipe_exit: BROKEN_PIPE_EXIT,
  };
}

export function runGuide(io: { stdout: { write(s: string): unknown } }, asJson: boolean): number {
  io.stdout.write((asJson ? pyJson(document()) : text()) + "\n");
  return 0;
}
