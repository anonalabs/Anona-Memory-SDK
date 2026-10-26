/**
 * The rest of the API as commands: memory models, rules, webhooks, the entity
 * graph, receipts, asking about one user, the proxy chat, exports, and the
 * account views (org, keys, members, invitations, billing, usage history).
 * Mirrors anona/cli/resources.py and prints the same lines.
 *
 * verbs.ts owns the machinery every resource verb shares -- the credential,
 * `--json`, the destructive-verb guard, the error rendering. This module is the
 * grammar and the table of what each additional verb sends and prints, so a
 * verb is one row rather than one function.
 *
 * Three families, told apart by who may call them:
 *
 * - space verbs (models, rules, webhooks, graph, entities, ask-user, chat,
 *   exports, ...) work with an API key, the command-line login or a temporary
 *   profile, as the server decides;
 * - account verbs (org, orgs, projects, keys list, members, invitations,
 *   billing, usage history, exports) are dashboard-session routes that the
 *   command-line login is allowed to call. With an API key or a temporary
 *   profile the server answers `session_required` (exit 4); nothing is
 *   pre-refused here, so the server stays the single judge;
 * - dashboard-only verbs (creating or revoking a key, every billing change, org
 *   and member management) are never sent. They print one line naming the
 *   dashboard page and exit 4.
 *
 * Nothing here imports verbs.ts, args.ts or memory.ts as a value: args.ts
 * builds its command table from this module, so those would be cycles.
 */
import { randomBytes } from "node:crypto";
import { constants as fsc, lstatSync, promises as fsp } from "node:fs";
import { dirname, join } from "node:path";
import { AnonaError } from "../errors.js";
import type { CommandSpec, OptionSpec, PositionalSpec } from "./args.js";
import * as exits from "./exits.js";
import { count, d, given, LINK_REFUSED, or, out, p, renderModel, renderProfile, rows, type Render, type Values, type Writer } from "./fmt.js";
import type { CliClient, DataIO } from "./memory.js";
import { isObject, printable, printableBlock, truthy } from "./py.js";

export type Verb = [string, string | null];
const key = (v: Verb) => `${v[0]} ${v[1] ?? ""}`.trim();
const seg = encodeURIComponent;

export const DASHBOARD = "https://memory.anonalabs.com";

/** Verb -> [what, dashboard path]. `what` completes "anona: <what> is done in the dashboard: <URL>". */
export const DASHBOARD_ONLY: Record<string, readonly [string, string]> = {
  "keys create": ["creating an API key", "/dashboard/keys"],
  "keys revoke": ["revoking an API key", "/dashboard/keys"],
  "billing upgrade": ["upgrading your plan", "/dashboard/billing"],
  "billing checkout": ["checking out a plan", "/dashboard/billing"],
  "billing topup": ["buying credits", "/dashboard/billing"],
  "billing cancel": ["cancelling your subscription", "/dashboard/billing"],
  "billing portal": ["managing payment details", "/dashboard/billing"],
  "org rename": ["renaming the organization", "/dashboard/profile"],
  "org delete": ["deleting the organization", "/dashboard/profile"],
  "org transfer": ["transferring the organization", "/dashboard/profile"],
  "org leave": ["leaving the organization", "/dashboard/profile"],
  "members invite": ["inviting a member", "/dashboard/members"],
  "members remove": ["removing a member", "/dashboard/members"],
  "spaces members remove": ["removing a space member", "/dashboard/spaces"],
};

/** Verbs that address one space, and so need one named with an API key. */
export const SPACE_VERBS: ReadonlySet<string> = new Set([
  ...["list", "create", "get", "edit", "delete", "refresh", "clear", "history"].map((a) => `models ${a}`),
  "profile set",
  "profile reset",
  ...["list", "add", "edit", "delete"].map((a) => `rules ${a}`),
  ...["list", "create", "edit", "delete", "deliveries"].map((a) => `webhooks ${a}`),
  "graph",
  "graph draw",
  "entities list",
  "entities get",
  "ask-user",
  "chat",
  ...["list", "create", "download"].map((a) => `exports ${a}`),
]);

/**
 * The verb a command line names. `profile`, `graph`, `org` and `billing` take
 * their action as an optional positional, and `usage` as a flag, so the
 * parser's own verb is only the command there.
 */
export function verbOf(verb: Verb, args: Values): Verb {
  const [cmd] = verb;
  if (cmd === "profile" || cmd === "graph" || cmd === "org" || cmd === "billing") {
    return [cmd, typeof args.action === "string" ? args.action : null];
  }
  if (cmd === "usage") return [cmd, args.history ? "history" : args.breakdown ? "breakdown" : null];
  return verb;
}

export const isDashboardOnly = (verb: Verb) => key(verb) in DASHBOARD_ONLY;

/** A path segment escaped as the pip package's `quote(value, safe="")` escapes it. */
const pySeg = (value: string) => encodeURIComponent(value).replace(/[!'()*]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`);

export function refuseDashboard(io: DataIO, verb: Verb, args: Values = {}): number {
  const [what, base] = DASHBOARD_ONLY[key(verb)] as readonly [string, string];
  const words = Array.isArray(args.ignored) ? (args.ignored as string[]) : [];
  // The space's own page, where its members are managed.
  const path = key(verb) === "spaces members remove" && words.length ? `${base}/${pySeg(words[0] as string)}` : base;
  io.stderr.write(`anona: ${what} is done in the dashboard: ${DASHBOARD}${path}\n`);
  return exits.AUTH;
}

// --- grammar --------------------------------------------------------------------

/** What args.ts lends this module: its option builders and converters. */
export interface Helpers {
  creds(spaceVerb: string | null): OptionSpec[];
  limit(low: number, high: number): (value: string) => number;
  offset(value: string): number;
  nonEmpty(value: string): string;
  textArg(value: string): string;
  /** Throws the converter's refusal for a message. */
  refuse(message: string): never;
}

const MEMORY_TYPES = ["fact", "experience", "note"] as const;
const WEBHOOK_EVENTS_HELP = "Comma-separated events: memory.created, memory.consolidated, security.policy_triggered";

export function addCommands(h: Helpers): CommandSpec[] {
  const csv = (value: string): string[] => {
    const items = value.split(",").map((t) => t.trim());
    if (!items.every((t) => t)) h.refuse("must be a comma-separated list of non-empty values");
    return items;
  };
  const CSV = { type: csv, typeName: "_csv" };
  const EVENTS = { type: csv, typeName: "_events" };
  const LIMIT = (lo: number, hi: number) => ({ type: h.limit(lo, hi), typeName: "parse" });
  const OFFSET = { type: h.offset, typeName: "_offset" };
  const yes = (help: string): OptionSpec => ({ flags: ["--yes"], dest: "yes", kind: "store_true", help });
  const confirm = yes("Confirm; without it nothing is sent");
  const ignored: PositionalSpec = { dest: "ignored", metavar: "ARG", nargs: "*", help: "Ignored" };
  // A dashboard-only verb accepts and ignores `--yes`: a script that confirms
  // out of habit still learns where it is done, not a usage error.
  const ignoredYes = yes("Ignored");
  const leaf = (name: string, help: string, spaceVerb: string | null, extra: Partial<CommandSpec> = {}): CommandSpec => ({
    name,
    help,
    ...extra,
    options: [...h.creds(spaceVerb), ...(extra.options ?? [])],
  });
  const group = (name: string, help: string, actions: CommandSpec[]): CommandSpec => ({ name, help, actions });
  const dashboardLeaf = (name: string, help: string) => leaf(name, help, null, { positionals: [ignored], options: [ignoredYes] });

  const modelFlags = (): OptionSpec[] => [
    { flags: ["--tags"], dest: "tags", kind: "value", ...CSV, help: "Comma-separated tags that scope the model to matching memories" },
    { flags: ["--mode"], dest: "mode", kind: "value", choices: ["full", "delta"], help: "How a refresh rewrites it: full regenerates, delta edits in place" },
    { flags: ["--refresh-on-new-memories"], dest: "refresh_on_new_memories", kind: "bool_optional", help: "Refresh automatically when new memories are consolidated" },
    { flags: ["--memory-type"], dest: "memory_type", kind: "append", choices: MEMORY_TYPES, help: "Build the model only from this memory type; repeatable" },
    { flags: ["--max-tokens"], dest: "max_tokens", kind: "value", ...LIMIT(256, 8192), help: "Ceiling on the content length (256-8192)" },
  ];
  const modelId = (help: string): PositionalSpec => ({ dest: "model_id", metavar: "ID", help });

  const models = group("models", "List, create, edit, refresh or delete a space's memory models", [
    leaf("list", "List a space's memory models", "list", {
      options: [
        { flags: ["--limit"], dest: "limit", kind: "value", ...LIMIT(1, 200), help: "At most this many (1-200, default 50)" },
        { flags: ["--offset"], dest: "offset", kind: "value", ...OFFSET, help: "Skip this many first" },
        { flags: ["--tags"], dest: "tags", kind: "value", ...CSV, help: "Only models scoped to these comma-separated tags" },
      ],
    }),
    leaf("create", "Create a memory model: a question a space keeps answered", "create in", {
      positionals: [{ dest: "name", metavar: "NAME", help: "The model's name" }],
      options: [
        { flags: ["--query"], dest: "query", kind: "value", required: true, help: "The question the model answers from the space's memories" },
        ...modelFlags(),
        { flags: ["--model-id"], dest: "custom_id", kind: "value", help: "A stable id: lowercase letters, digits and hyphens (default: generated)" },
      ],
    }),
    leaf("get", "Show one memory model and its current content", "read", { positionals: [modelId("The memory model's id")] }),
    leaf("edit", "Change a memory model's name, question or refresh behaviour", "edit in", {
      positionals: [modelId("The memory model to change")],
      options: [
        { flags: ["--name"], dest: "name", kind: "value", help: "The new name" },
        { flags: ["--query"], dest: "query", kind: "value", help: "The new question" },
        ...modelFlags(),
      ],
    }),
    leaf("delete", "Delete a memory model (needs --yes)", "delete from", { positionals: [modelId("The memory model to delete")], options: [confirm] }),
    leaf("refresh", "Rewrite a memory model's content now", "refresh in", { positionals: [modelId("The memory model to refresh")] }),
    leaf("clear", "Empty a memory model's content and keep its definition (needs --yes)", "clear in", {
      positionals: [modelId("The memory model to clear")],
      options: [confirm],
    }),
    leaf("history", "Show a memory model's earlier versions", "read", { positionals: [modelId("The memory model to trace")] }),
  ]);

  const priority = (help: string): OptionSpec => ({ flags: ["--priority"], dest: "priority", kind: "value", ...LIMIT(0, 1000), help });
  const rules = group("rules", "List, add, edit or delete the rules every answer from a space must follow", [
    leaf("list", "List a space's rules", "list"),
    leaf("add", "Add a rule", "add to", {
      positionals: [{ dest: "text", metavar: "TEXT", help: "The rule, as an instruction (up to 2000 characters)" }],
      options: [
        { flags: ["--name"], dest: "name", kind: "value", help: "A short label (default: the first 60 characters of TEXT)" },
        priority("Higher comes first in the prompt (0-1000, default 0)"),
        { flags: ["--active"], dest: "active", kind: "bool_optional", help: "Apply the rule (--no-active stores it switched off)" },
        { flags: ["--tags"], dest: "tags", kind: "value", ...CSV, help: "Comma-separated tags that scope the rule" },
      ],
    }),
    leaf("edit", "Change a rule; only the flags given change", "edit in", {
      positionals: [{ dest: "rule_id", metavar: "ID", help: "The rule to change" }],
      options: [
        { flags: ["--name"], dest: "name", kind: "value", help: "The new label" },
        { flags: ["--text"], dest: "text", kind: "value", type: h.textArg, typeName: "_text_arg", help: "The new rule text; @FILE reads it" },
        priority("The new priority (0-1000)"),
        { flags: ["--active"], dest: "active", kind: "bool_optional", help: "Switch the rule on or off" },
        { flags: ["--tags"], dest: "tags", kind: "value", ...CSV, help: "Replace the tags (comma-separated)" },
      ],
    }),
    leaf("delete", "Delete a rule (needs --yes)", "delete from", {
      positionals: [{ dest: "rule_id", metavar: "ID", help: "The rule to delete" }],
      options: [confirm],
    }),
  ]);

  const webhookId = (help: string): PositionalSpec => ({ dest: "webhook_id", metavar: "ID", help });
  const webhooks = group("webhooks", "List, create, edit or delete a space's webhooks, and read their deliveries", [
    leaf("list", "List a space's webhooks", "list"),
    leaf("create", "Register an HTTPS endpoint to be called when something happens", "create in", {
      positionals: [{ dest: "url", metavar: "URL", help: "The HTTPS endpoint to POST events to" }],
      options: [
        { flags: ["--events"], dest: "events", kind: "value", ...EVENTS, help: WEBHOOK_EVENTS_HELP + " (default: memory.created)" },
        { flags: ["--enabled"], dest: "enabled", kind: "bool_optional", help: "Deliver events (--no-enabled creates it paused)" },
      ],
    }),
    leaf("edit", "Change a webhook; only the flags given change", "edit in", {
      positionals: [webhookId("The webhook to change")],
      options: [
        { flags: ["--url"], dest: "url", kind: "value", help: "The new endpoint" },
        { flags: ["--events"], dest: "events", kind: "value", ...EVENTS, help: WEBHOOK_EVENTS_HELP },
        { flags: ["--enabled"], dest: "enabled", kind: "bool_optional", help: "Resume or pause deliveries" },
      ],
    }),
    leaf("delete", "Delete a webhook (needs --yes)", "delete from", { positionals: [webhookId("The webhook to delete")], options: [confirm] }),
    leaf("deliveries", "Show a webhook's recent delivery attempts, newest first", "read", {
      positionals: [webhookId("The webhook to inspect")],
      options: [
        { flags: ["--limit"], dest: "limit", kind: "value", ...LIMIT(1, 200), help: "At most this many (1-200, default 50)" },
        { flags: ["--cursor"], dest: "cursor", kind: "value", help: "Continue from a previous page's cursor" },
      ],
    }),
  ]);

  const graph: CommandSpec = {
    name: "graph",
    help: "Show a space's entity graph, or draw a chart from its memories",
    positionals: [
      { dest: "action", nargs: "?", choices: ["draw"], help: "`draw` asks for a chart instead of the graph" },
      { dest: "prompt", metavar: "PROMPT", nargs: "?", help: "With draw: what to chart, in plain language" },
    ],
    options: [
      ...h.creds("read"),
      { flags: ["--limit"], dest: "limit", kind: "value", ...LIMIT(1, 2000), help: "At most this many entities (1-2000, default 500)" },
      { flags: ["--min-count"], dest: "min_count", kind: "value", ...LIMIT(1, 1_000_000), help: "Only entities mentioned at least this many times" },
      { flags: ["--model"], dest: "model", kind: "value", help: "With draw: the LLM that draws it (see `anona llm-models`)" },
    ],
  };

  const entities = group("entities", "List the entities extracted from a space, or show one", [
    leaf("list", "List entities, most mentioned first", "list", {
      options: [
        { flags: ["--limit"], dest: "limit", kind: "value", ...LIMIT(1, 500), help: "At most this many (1-500, default 100)" },
        { flags: ["--offset"], dest: "offset", kind: "value", ...OFFSET, help: "Skip this many first" },
      ],
    }),
    leaf("get", "Show one entity and what has been learned about it", "read", {
      positionals: [{ dest: "entity_id", metavar: "ID", help: "The entity's id" }],
    }),
  ]);

  const receipts = group("receipts", "Show what a call put in the prompt, and what it left out", [
    leaf("get", "Show the context receipt of a request that asked for one", null, {
      positionals: [{ dest: "request_id", metavar: "REQUEST_ID", help: "The request's id (the X-Request-ID of a retrieve or proxied call)" }],
      options: [
        { flags: ["--explain"], dest: "explain", kind: "store_true", help: "Explain why one memory was or was not included" },
        { flags: ["--memory-id"], dest: "memory_id", kind: "value", help: "With --explain: the memory to explain" },
      ],
    }),
  ]);

  const askUser = leaf("ask-user", "Ask a question about one end user, answered from what the space knows of them", "ask in", {
    positionals: [
      { dest: "user_id", metavar: "USER", type: h.nonEmpty, typeName: "non_empty", help: "The user_id your writes are scoped with" },
      { dest: "question", metavar: "QUESTION", help: "The question" },
    ],
    options: [
      { flags: ["--model"], dest: "model", kind: "value", help: "The LLM that answers (see `anona llm-models`; default: the space's)" },
      { flags: ["--depth"], dest: "depth", kind: "value", choices: ["fast", "thorough"], help: "How deep it reads" },
    ],
  });

  const spaceTypes = leaf("space-types", "List the space types a new space can start from", null);

  const chat = leaf("chat", "Send one message through the memory-backed chat proxy and print the reply", "chat in", {
    positionals: [{ dest: "message", metavar: "MESSAGE", help: "What to say" }],
    options: [
      { flags: ["--model"], dest: "model", kind: "value", help: "The LLM to use (see `anona llm-models`; default: the space's)" },
      { flags: ["--no-memory"], dest: "no_memory", kind: "store_true", help: "Neither recall memories into this turn nor record it" },
    ],
  });

  const org = leaf("org", "Show your organization", null, {
    positionals: [
      { dest: "action", nargs: "?", choices: ["rename", "delete", "transfer", "leave"], help: "These four are done in the dashboard; nothing is sent" },
      ignored,
    ],
    options: [ignoredYes],
  });
  const orgs = group("orgs", "List the organizations you belong to", [leaf("list", "List the organizations you belong to", null)]);
  const projects = group("projects", "List your projects", [leaf("list", "List your projects", null)]);
  const keys = group("keys", "List API keys, or create or revoke one (in the dashboard)", [
    leaf("list", "List your API keys (never their secrets)", null),
    dashboardLeaf("create", "Create an API key (done in the dashboard)"),
    dashboardLeaf("revoke", "Revoke an API key (done in the dashboard)"),
  ]);
  const members = group("members", "List your organization's members and invitations", [
    leaf("list", "List the members of your organization", null),
    leaf("me", "Show who you are signed in as", null),
    leaf("invitations", "List invitations your organization has sent", null),
    dashboardLeaf("invite", "Invite a member (done in the dashboard)"),
    dashboardLeaf("remove", "Remove a member (done in the dashboard)"),
  ]);
  const invitations = group("invitations", "See, accept or decline space and organization invitations sent to you", [
    leaf("pending", "List invitations waiting on you", null),
    leaf("accept", "Accept an invitation", null, { positionals: [{ dest: "token", metavar: "TOKEN", help: "The invitation's token (see `invitations pending`)" }] }),
    leaf("decline", "Decline an invitation", null, { positionals: [{ dest: "token", metavar: "TOKEN", help: "The invitation's token" }] }),
  ]);
  const billing = leaf("billing", "Show your plan and credits, or your top-up history", null, {
    positionals: [
      {
        dest: "action",
        nargs: "?",
        choices: ["topups", "upgrade", "checkout", "topup", "cancel", "portal"],
        help: "`topups` lists purchases; the others are done in the dashboard and nothing is sent",
      },
      ignored,
    ],
    options: [ignoredYes],
  });
  const exportsCmd = group("exports", "Export everything a space holds", [
    leaf("list", "List a space's exports", "list exports of"),
    leaf("create", "Start an export of a space", "export"),
    leaf("download", "Download a finished export", "download from", {
      positionals: [{ dest: "job_id", metavar: "JOB_ID", help: "The export's id (see `exports list`)" }],
      options: [
        { flags: ["--out"], dest: "out", kind: "value", metavar: "FILE", help: "Where to save it (default: anona-export-JOB_ID.zip)" },
        yes("Overwrite FILE if it exists"),
      ],
    }),
  ]);

  return [models, rules, webhooks, graph, entities, receipts, askUser, spaceTypes, chat, org, orgs, projects, keys, members, invitations, billing, exportsCmd];
}

/** `profile [set|reset]` and its flags, laid onto the existing `profile` command. */
export function profileActions(h: Helpers): { positionals: PositionalSpec[]; options: OptionSpec[] } {
  const dial = (name: string, low: string): OptionSpec => ({
    flags: [`--${name}`],
    dest: name,
    kind: "value",
    type: h.limit(1, 5),
    typeName: "parse",
    help: `With set: 1 = ${low}, 5 = the opposite`,
  });
  return {
    positionals: [
      { dest: "action", nargs: "?", choices: ["set", "reset"], help: "`set` changes the profile; `reset` returns it to the defaults (needs --yes)" },
    ],
    options: [
      { flags: ["--mission"], dest: "mission", kind: "value", type: h.textArg, typeName: "_text_arg", help: "With set: what the space is for; @FILE reads it" },
      dial("skepticism", "takes statements at face value"),
      dial("literalism", "reads intent loosely"),
      dial("empathy", "ignores emotional context"),
      { flags: ["--yes"], dest: "yes", kind: "store_true", help: "With reset: confirm; without it nothing is sent" },
    ],
  };
}

export function usageFlags(h: Helpers): OptionSpec[] {
  return [
    { flags: ["--history"], dest: "history", kind: "store_true", help: "Daily operation counts instead of the credit balance (needs `anona login`)" },
    { flags: ["--breakdown"], dest: "breakdown", kind: "store_true", help: "Operation counts per space instead of the credit balance (needs `anona login`)" },
    {
      flags: ["--days"],
      dest: "days",
      kind: "value",
      type: h.limit(1, 365),
      typeName: "parse",
      help: "With --history or --breakdown: how many days back (1-365, default 30)",
    },
  ];
}

/** `spaces members list|remove`, under the existing `spaces` command. */
export function spaceMembers(h: Helpers): CommandSpec {
  return {
    name: "members",
    help: "List the people a space is shared with",
    actionDest: "sub_action",
    actions: [
      {
        name: "list",
        help: "List a space's members",
        positionals: [{ dest: "space_id", metavar: "SPACE", help: "The space (owner:name for one shared with you)" }],
        options: h.creds(null),
      },
      {
        name: "remove",
        help: "Remove a member from a space (done in the dashboard)",
        positionals: [{ dest: "ignored", metavar: "ARG", nargs: "*", help: "Ignored" }],
        options: [...h.creds(null), { flags: ["--yes"], dest: "yes", kind: "store_true", help: "Ignored" }],
      },
    ],
  };
}

// --- usage checks, before any credential is looked up ---------------------------

const no = (io: DataIO, message: string): number => {
  io.stderr.write(`anona: ${message} Nothing was sent.\n`);
  return exits.USAGE;
};
const blank = (io: DataIO, what: string, value: unknown): number | null =>
  String(value).trim() ? null : no(io, `the ${what} is empty.`);

const defaultOut = (jobId: string) => `anona-export-${jobId}.zip`;
const outPath = (args: Values) => (truthy(args.out) ? (args.out as string) : defaultOut(args.job_id as string));

function lexists(path: string): boolean {
  try {
    lstatSync(path);
    return true;
  } catch {
    return false;
  }
}

export function precheck(io: DataIO, verb: Verb, args: Values): number | null {
  const [cmd, act] = verb;
  const k = key(verb);
  if (k === "models create") return blank(io, "model name", args.name) ?? blank(io, "question", args.query);
  if (k === "models edit" && !Object.keys(modelBody(args, false)).length) {
    return no(io, "name at least one field to change (see `anona models edit --help`).");
  }
  if (k === "rules add") return blank(io, "rule text", args.text);
  if (k === "rules edit" && !Object.keys(ruleEditBody(args)).length) {
    return no(io, "name at least one field to change (see `anona rules edit --help`).");
  }
  if (k === "webhooks edit" && !Object.keys(given({ url: args.url, event_types: args.events, enabled: args.enabled })).length) {
    return no(io, "name at least one field to change (see `anona webhooks edit --help`).");
  }
  if (cmd === "profile") {
    const flags = (
      [
        ["--mission", args.mission],
        ["--skepticism", args.skepticism],
        ["--literalism", args.literalism],
        ["--empathy", args.empathy],
      ] as const
    )
      .filter(([, v]) => v !== undefined)
      .map(([f]) => f);
    if (act !== "set" && flags.length) return no(io, `${flags.join(", ")} needs \`profile set\`.`);
    if (act === "set" && !flags.length) return no(io, "name at least one field to change (see `anona profile --help`).");
  }
  if (cmd === "graph") {
    if (act === "draw") {
      if (args.prompt === undefined) return no(io, "graph draw needs a PROMPT.");
      return blank(io, "prompt", args.prompt);
    }
    if (args.prompt !== undefined || args.model !== undefined) return no(io, "a PROMPT or --model needs `graph draw`.");
  }
  if (cmd === "usage") {
    if (args.history && args.breakdown) return no(io, "give --history or --breakdown, not both.");
    if (args.days !== undefined && !(args.history || args.breakdown)) return no(io, "--days needs --history or --breakdown.");
  }
  if (k === "receipts get") {
    if (args.memory_id !== undefined && !args.explain) return no(io, "--memory-id needs --explain.");
    if (args.explain && args.memory_id === undefined) return no(io, "--explain needs --memory-id.");
  }
  if (k === "ask-user") return blank(io, "question", args.question);
  if (k === "chat") return blank(io, "message", args.message);
  if (k === "exports download") {
    const target = outPath(args);
    if (lexists(target) && !args.yes) {
      io.stderr.write(`anona: ${p(target)} already exists. Pass --yes to overwrite it. Nothing was sent.\n`);
      return exits.USAGE;
    }
  }
  return null;
}

/** What a destructive verb would do, or null when it is not one of these. */
export function destroyMessage(verb: Verb, args: Values, space: string | undefined): string | null {
  const where = `in space '${p(space || "default")}'`;
  switch (key(verb)) {
    case "models delete":
      return `delete memory model ${p(args.model_id)} ${where}`;
    case "models clear":
      return `clear the content of memory model ${p(args.model_id)} ${where}`;
    case "profile reset":
      return `reset the profile of space '${p(space || "default")}' to the platform defaults`;
    case "rules delete":
      return `delete rule ${p(args.rule_id)} ${where}`;
    case "webhooks delete":
      return `delete webhook ${p(args.webhook_id)} ${where}`;
    default:
      return null;
  }
}

// --- requests -------------------------------------------------------------------

function trigger(args: Values): Record<string, unknown> | null {
  const t = given({ mode: args.mode, refresh_on_new_memories: args.refresh_on_new_memories, memory_type: args.memory_type });
  return Object.keys(t).length ? t : null;
}

function modelBody(args: Values, create: boolean): Record<string, unknown> {
  return given({
    name: args.name,
    query: args.query,
    model_id: create ? args.custom_id : undefined,
    tags: args.tags,
    max_tokens: args.max_tokens,
    trigger: trigger(args),
  });
}

const ruleEditBody = (args: Values) =>
  given({ name: args.name, content: args.text, priority: args.priority, is_active: args.active, tags: args.tags });

// Python's str.split() splits on these.
const PY_SPACE = /[\t\n\v\f\r\x1c-\x1f \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/;
const words = (text: string) => text.split(PY_SPACE).filter(Boolean);
const head = (text: string, n: number) => Array.from(text).slice(0, n).join("");

const ruleName = (args: Values) => (args.name !== undefined ? (args.name as string) : head(words(args.text as string).join(" "), 60));

function sp(space: string | undefined): string {
  if (space === undefined) throw new Error("this command needs a space");
  return `/v1/spaces/${seg(space)}`;
}

type Req = [method: string, path: string, query: Record<string, unknown> | undefined, body: unknown];
const REQUESTS: Record<string, (a: Values, s: string | undefined) => Req> = {
  "models list": (a, s) => ["GET", `${sp(s)}/models`, given({ limit: a.limit, offset: a.offset, tags: a.tags }), undefined],
  "models create": (a, s) => ["POST", `${sp(s)}/models`, undefined, modelBody(a, true)],
  "models get": (a, s) => ["GET", `${sp(s)}/models/${seg(a.model_id as string)}`, undefined, undefined],
  "models edit": (a, s) => ["PATCH", `${sp(s)}/models/${seg(a.model_id as string)}`, undefined, modelBody(a, false)],
  "models delete": (a, s) => ["DELETE", `${sp(s)}/models/${seg(a.model_id as string)}`, undefined, undefined],
  "models refresh": (a, s) => ["POST", `${sp(s)}/models/${seg(a.model_id as string)}/refresh`, undefined, undefined],
  "models clear": (a, s) => ["POST", `${sp(s)}/models/${seg(a.model_id as string)}/clear`, undefined, undefined],
  "models history": (a, s) => ["GET", `${sp(s)}/models/${seg(a.model_id as string)}/history`, undefined, undefined],
  "profile reset": (_a, s) => ["DELETE", `${sp(s)}/profile`, undefined, undefined],
  "rules list": (_a, s) => ["GET", `${sp(s)}/rules`, undefined, undefined],
  "rules add": (a, s) => [
    "POST",
    `${sp(s)}/rules`,
    undefined,
    { name: ruleName(a), content: a.text, ...given({ priority: a.priority, is_active: a.active, tags: a.tags }) },
  ],
  "rules edit": (a, s) => ["PATCH", `${sp(s)}/rules/${seg(a.rule_id as string)}`, undefined, ruleEditBody(a)],
  "rules delete": (a, s) => ["DELETE", `${sp(s)}/rules/${seg(a.rule_id as string)}`, undefined, undefined],
  "webhooks list": (_a, s) => ["GET", `${sp(s)}/webhooks`, undefined, undefined],
  "webhooks create": (a, s) => ["POST", `${sp(s)}/webhooks`, undefined, { url: a.url, ...given({ event_types: a.events, enabled: a.enabled }) }],
  "webhooks edit": (a, s) => [
    "PATCH",
    `${sp(s)}/webhooks/${seg(a.webhook_id as string)}`,
    undefined,
    given({ url: a.url, event_types: a.events, enabled: a.enabled }),
  ],
  "webhooks delete": (a, s) => ["DELETE", `${sp(s)}/webhooks/${seg(a.webhook_id as string)}`, undefined, undefined],
  "webhooks deliveries": (a, s) => [
    "GET",
    `${sp(s)}/webhooks/${seg(a.webhook_id as string)}/deliveries`,
    given({ limit: a.limit, cursor: a.cursor }),
    undefined,
  ],
  graph: (a, s) => ["GET", `${sp(s)}/graph`, given({ limit: a.limit, min_count: a.min_count }), undefined],
  "graph draw": (a, s) => ["POST", `${sp(s)}/graph`, undefined, { request: a.prompt, ...given({ model: a.model }) }],
  "entities list": (a, s) => ["GET", `${sp(s)}/entities`, given({ limit: a.limit, offset: a.offset }), undefined],
  "entities get": (a, s) => ["GET", `${sp(s)}/entities/${seg(a.entity_id as string)}`, undefined, undefined],
  "ask-user": (a, s) => [
    "POST",
    `${sp(s)}/users/${seg(a.user_id as string)}/ask`,
    undefined,
    { query: a.question, ...given({ model: a.model, depth: a.depth }) },
  ],
  "space-types": () => ["GET", "/v1/space-types", undefined, undefined],
  chat: (a, s) => [
    "POST",
    "/v1/chat/completions",
    undefined,
    {
      messages: [{ role: "user", content: a.message }],
      space_id: s,
      ...given({ model: a.model }),
      // `memory` gates only recall; the turn is recorded unless `auto_record` is false too.
      ...(a.no_memory ? { memory: false, auto_record: false } : {}),
    },
  ],
  org: () => ["GET", "/v1/orgs/me", undefined, undefined],
  "orgs list": () => ["GET", "/v1/orgs", undefined, undefined],
  "projects list": () => ["GET", "/v1/projects", undefined, undefined],
  "keys list": () => ["GET", "/v1/api-keys", undefined, undefined],
  "members list": () => ["GET", "/v1/members", undefined, undefined],
  "members me": () => ["GET", "/v1/members/me", undefined, undefined],
  "members invitations": () => ["GET", "/v1/members/invitations", undefined, undefined],
  "invitations pending": () => ["GET", "/v1/invitations/pending", undefined, undefined],
  "invitations accept": (a) => ["POST", "/v1/invitations/accept", undefined, { token: a.token }],
  "invitations decline": (a) => ["POST", "/v1/invitations/decline", undefined, { token: a.token }],
  billing: () => ["GET", "/v1/billing", undefined, undefined],
  "billing topups": () => ["GET", "/v1/billing/topups", undefined, undefined],
  "usage history": (a) => ["GET", "/v1/usage", given({ days: a.days }), undefined],
  "usage breakdown": (a) => ["GET", "/v1/usage/breakdown", given({ days: a.days }), undefined],
  "exports list": (_a, s) => ["GET", `${sp(s)}/exports`, undefined, undefined],
  "exports create": (_a, s) => ["POST", `${sp(s)}/exports`, undefined, undefined],
  "receipts get": (a) => [
    "GET",
    `/v1/receipts/${seg(a.request_id as string)}` + (a.explain ? "/explain" : ""),
    a.explain ? given({ memory_id: a.memory_id }) : undefined,
    undefined,
  ],
  "spaces members list": (a) => ["GET", `/v1/spaces/${seg(a.space_id as string)}/members`, undefined, undefined],
};

/** Routes that answer 204; the CLI writes the document `--json` prints. */
const NO_CONTENT: Record<string, (a: Values, s: string | undefined) => unknown> = {
  "models delete": (a, s) => ({ deleted: true, model_id: a.model_id, space_id: s }),
  "profile reset": (_a, s) => ({ reset: true, space_id: s, settings: "profile" }),
  "rules delete": (a, s) => ({ deleted: true, rule_id: a.rule_id, space_id: s }),
  "webhooks delete": (a, s) => ({ deleted: true, webhook_id: a.webhook_id, space_id: s }),
  "invitations decline": () => ({ declined: true }),
};

const SPECIAL = new Set(["profile set", "exports download"]);

export const handles = (verb: Verb) => key(verb) in REQUESTS || SPECIAL.has(key(verb));

export async function call(client: CliClient, verb: Verb, args: Values, space: string | undefined): Promise<unknown> {
  const k = key(verb);
  if (k === "profile set") return profileSet(client, args, space);
  if (k === "exports download") return download(client, args, space);
  if (k === "models edit" && trigger(args)) return modelsEdit(client, args, space);
  const [method, path, query, body] = (REQUESTS[k] as (typeof REQUESTS)[string])(args, space);
  const got = await client.raw({ method, path, query, body, expectNoContent: k in NO_CONTENT });
  const written = NO_CONTENT[k];
  return written ? written(args, space) : got;
}

/**
 * A trigger write replaces the whole trigger, so read the model, lay the named
 * trigger flags over its current trigger, and send all of it:
 * `--no-refresh-on-new-memories` must not turn a delta model into a full one or
 * drop its memory-type filter.
 */
async function modelsEdit(client: CliClient, args: Values, space: string | undefined): Promise<unknown> {
  const path = `${sp(space)}/models/${seg(args.model_id as string)}`;
  const raw = d(await client.raw({ method: "GET", path })).trigger;
  const current = isObject(raw) ? raw : {};
  const merged: Record<string, unknown> = {};
  for (const k of ["mode", "refresh_on_new_memories", "memory_type"]) {
    if (current[k] !== undefined && current[k] !== null) merged[k] = current[k];
  }
  Object.assign(merged, trigger(args) ?? {});
  const body = modelBody(args, false);
  body.trigger = merged;
  return client.raw({ method: "PATCH", path, body });
}

/**
 * The PUT replaces the whole profile, so read it, change the named fields, and
 * write every field back: `--empathy 4` must not clear the mission or the other
 * two dials.
 */
async function profileSet(client: CliClient, args: Values, space: string | undefined): Promise<unknown> {
  const path = `${sp(space)}/profile`;
  const current = d(await client.raw({ method: "GET", path }));
  const raw = current.disposition;
  const disposition = isObject(raw) ? raw : {};
  const dials: Record<string, unknown> = {};
  for (const k of ["skepticism", "literalism", "empathy"]) dials[k] = disposition[k] ?? null;
  Object.assign(dials, given({ skepticism: args.skepticism, literalism: args.literalism, empathy: args.empathy }));
  const mission = args.mission !== undefined ? args.mission : truthy(current.mission) ? current.mission : null;
  return client.raw({ method: "PUT", path, body: { mission, disposition: dials } });
}

// --- exports download -------------------------------------------------------------

function failure(status: number, code: string, message: string): AnonaError {
  return new AnonaError({ statusCode: status, message, code, detail: { error: { code, message } } });
}

/** A failure of the connection itself, named as the pip package's HTTP library names it. */
class NetError extends Error {
  constructor(readonly pyName: string) {
    super(pyName);
  }
}

/** The error a transport failure is reported as: exit 7, the name beside the API host. */
function transportFailure(e: NetError): AnonaError {
  return new AnonaError({ statusCode: 503, message: e.pyName, code: "download_failed", detail: new Error(e.pyName) });
}

/** An API error response, the way the SDK reads one. */
async function apiError(resp: Response): Promise<AnonaError> {
  const text = await resp.text();
  let detail: unknown = text;
  let code: string | undefined;
  let message = text || resp.statusText;
  try {
    const parsed = JSON.parse(text) as { error?: { code?: string; message?: string } };
    detail = parsed;
    if (parsed?.error) {
      code = parsed.error.code;
      message = parsed.error.message ?? message;
    }
  } catch {
    /* not JSON: the raw text is the detail, as for an edge response */
  }
  return new AnonaError({ statusCode: resp.status, message, code, requestId: resp.headers.get("x-request-id") ?? undefined, detail });
}

const FS_NAMES: Record<string, string> = {
  ENOENT: "FileNotFoundError",
  EISDIR: "IsADirectoryError",
  EACCES: "PermissionError",
  EPERM: "PermissionError",
  ENOTDIR: "NotADirectoryError",
  EEXIST: "FileExistsError",
};

const writeFailed = (target: string, e: unknown) =>
  failure(400, "write_failed", `could not write ${printable(target)} (${FS_NAMES[(e as NodeJS.ErrnoException).code ?? ""] ?? "OSError"}).`);

/**
 * Write to a fresh, privately named file beside the target and move it into
 * place, so a download that breaks half way never leaves a truncated bundle
 * under the real name, and a file or link someone planted at a guessable
 * name is never opened. Only filesystem failures are `write_failed`; whatever
 * the stream throws is the caller's to report.
 */
async function save(target: string, chunks: AsyncIterable<Uint8Array>): Promise<number> {
  const part = join(dirname(target), `.anona-export-${randomBytes(8).toString("hex")}.part`);
  const flags = fsc.O_CREAT | fsc.O_EXCL | fsc.O_WRONLY | (fsc.O_NOFOLLOW ?? 0);
  let fh: Awaited<ReturnType<typeof fsp.open>>;
  try {
    fh = await fsp.open(part, flags, 0o600);
  } catch (e) {
    throw writeFailed(target, e);
  }
  let total = 0;
  try {
    for await (const chunk of chunks) {
      try {
        await fh.write(chunk);
      } catch (e) {
        throw writeFailed(target, e);
      }
      total += chunk.byteLength;
    }
    await fh.close();
    try {
      await fsp.rename(part, target);
    } catch (e) {
      throw writeFailed(target, e);
    }
  } finally {
    await fh.close().catch(() => undefined);
    await fsp.unlink(part).catch(() => undefined);
  }
  return total;
}

/**
 * One request whose clock restarts at every step, as the pip package's
 * per-read timeout does: a slow but steady transfer is not cut off, a stalled
 * one is.
 */
async function idleFetch(client: CliClient, url: string, init: RequestInit): Promise<{ resp: Response; body: () => AsyncGenerator<Uint8Array>; stop: () => void }> {
  const link = client.link as NonNullable<CliClient["link"]>;
  const ctl = new AbortController();
  let fired = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const arm = () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      fired = true;
      ctl.abort();
    }, link.timeoutMs);
    timer.unref?.();
  };
  const stop = () => clearTimeout(timer);
  arm();
  let resp: Response;
  try {
    resp = await link.rawFetch(url, { ...init, signal: ctl.signal });
  } catch {
    stop();
    throw transportFailure(new NetError(fired ? "ReadTimeout" : "ConnectError"));
  }
  async function* body(): AsyncGenerator<Uint8Array> {
    const reader = resp.body?.getReader();
    try {
      while (reader) {
        arm();
        let step: ReadableStreamReadResult<Uint8Array>;
        try {
          step = await reader.read();
        } catch {
          throw transportFailure(new NetError(fired ? "ReadTimeout" : "RemoteProtocolError"));
        }
        if (step.done) return;
        yield step.value;
      }
    } finally {
      stop();
      reader?.cancel().catch(() => undefined);
    }
  }
  return { resp, body, stop };
}

/** Fetch a download link with no credential: it is a presigned storage URL, and the API credential must not be sent to it. */
async function fetchLink(client: CliClient, url: string, target: string): Promise<number> {
  if (!/^https?:\/\//i.test(url)) throw failure(502, "download_failed", "the download link is not an http(s) URL.");
  const { resp, body, stop } = await idleFetch(client, url, { method: "GET", redirect: "follow" });
  if (!resp.ok) {
    stop();
    // The storage host answered, not the API: its status says nothing about
    // the credential, so it is not mapped as if it did (a 403 here is an
    // expired link, not exit 4).
    throw new AnonaError({ statusCode: resp.status, message: LINK_REFUSED, code: LINK_REFUSED, detail: new Error(LINK_REFUSED) });
  }
  return save(target, body());
}

async function download(client: CliClient, args: Values, space: string | undefined): Promise<unknown> {
  const target = outPath(args);
  const path = `${sp(space)}/exports/${seg(args.job_id as string)}/download`;
  const link = client.link as NonNullable<CliClient["link"]>;
  const here = link.baseUrl + path;
  const { resp, body, stop } = await idleFetch(client, here, { method: "GET", headers: { authorization: `Bearer ${link.token}` }, redirect: "manual" });
  let size: number;
  const location = resp.headers.get("location");
  if (resp.status >= 300 && resp.status < 400 && location) {
    stop();
    size = await fetchLink(client, new URL(location, here).toString(), target);
  } else {
    if (!resp.ok) {
      try {
        throw await apiError(resp);
      } finally {
        stop();
      }
    }
    if ((resp.headers.get("content-type") ?? "").toLowerCase().includes("json")) {
      const parts: Uint8Array[] = [];
      for await (const chunk of body()) parts.push(chunk);
      let doc: unknown;
      try {
        doc = JSON.parse(Buffer.concat(parts).toString("utf8"));
      } catch {
        doc = null;
      }
      const url = isObject(doc) ? doc.download_url : undefined;
      if (typeof url !== "string" || !url) throw failure(502, "download_failed", "the server sent no download link.");
      size = await fetchLink(client, new URL(url, here).toString(), target);
    } else {
      size = await save(target, body());
    }
  }
  return { job_id: args.job_id, path: target, bytes: size };
}

// --- plain-text output ------------------------------------------------------------

/** Rows from a wrapped document or a bare list. */
function items(doc: unknown, field: string): Record<string, unknown>[] {
  return Array.isArray(doc) ? doc.filter(isObject) : rows(doc, field);
}

const commas = (values: unknown) => (Array.isArray(values) && values.length ? values.map(p).join(",") : "-");

type R = (io: Writer, doc: unknown, args: Values, space: string | undefined) => void;

const rModelsList: R = (io, doc, _a, space) => {
  const r = items(doc, "items");
  if (!r.length) out(io, `No memory models in space '${p(space)}'.`);
  for (const m of r) out(io, `${p(m.id)}  ${p(m.name)}  ${p(m.query)}`);
};

const rModelsCreate: R = (io, doc, args) => {
  const x = d(doc);
  out(io, `Creating memory model ${p(or(x.model_id, args.custom_id))} (job ${p(x.job_id)}).`);
};

const rModelsRefresh: R = (io, doc, args) => {
  const x = d(doc);
  out(io, `Refreshing ${p(or(x.model_id, args.model_id))} (job ${p(x.job_id)}).`);
};

const rModelsHistory: R = (io, doc, args) => {
  const r = items(doc, "entries");
  if (!r.length) out(io, `No earlier versions of memory model ${p(args.model_id)}.`);
  for (const h of r) out(io, `${p(h.changed_at)}  ${head(printable(truthy(h.previous_content) ? h.previous_content : ""), 80)}`);
};

const rRulesList: R = (io, doc, _a, space) => {
  const r = items(doc, "items");
  if (!r.length) out(io, `No rules in space '${p(space)}'.`);
  for (const x of r) out(io, `${p(x.id)}  ${p(x.content)}`);
};

const rWebhooksList: R = (io, doc, _a, space) => {
  const r = items(doc, "items");
  if (!r.length) out(io, `No webhooks in space '${p(space)}'.`);
  for (const w of r) out(io, `${p(w.id)}  ${p(w.url)}  ${commas(w.event_types)}`);
};

const rWebhookCreated: R = (io, doc) => {
  const x = d(doc);
  out(io, `Created webhook ${p(x.id)}.`);
  if (truthy(x.secret)) {
    out(io, `Signing secret: ${p(x.secret)}`);
    out(io, "Save this secret now; it is not shown again.");
  }
};

const rDeliveries: R = (io, doc, args) => {
  const x = d(doc);
  const r = items(x, "items");
  if (!r.length) out(io, `No deliveries for webhook ${p(args.webhook_id)}.`);
  for (const e of r) out(io, `${p(or(e.delivered_at, e.last_attempt_at, e.created_at))}  ${p(e.status)}  ${p(e.event_type)}`);
  if (truthy(x.next_cursor)) out(io, `Next cursor: ${p(x.next_cursor)}`);
};

const rGraph: R = (io, doc) => {
  const x = d(doc);
  out(io, `${p(x.total_entities)} entities, ${p(x.total_edges)} links`);
  for (const n of items(x, "nodes").slice(0, 50)) out(io, `${p(n.label)} (${p(n.mention_count)})`);
};

const rChart: R = (io, doc) => {
  const x = d(doc);
  if (truthy(x.text)) out(io, printableBlock(x.text));
  const source = or(x.mermaid, x.diagram);
  if (truthy(source)) {
    if (truthy(x.text)) out(io, "");
    out(io, printableBlock(source));
  }
  if (!truthy(x.text) && !truthy(source)) out(io, "No chart was drawn.");
};

const rEntities: R = (io, doc, _a, space) => {
  const r = items(doc, "items");
  if (!r.length) out(io, `No entities in space '${p(space)}'.`);
  for (const e of r) out(io, `${p(e.id)}  ${p(e.name)}  ${p(e.mention_count)}`);
};

const rEntity: R = (io, doc, args) => {
  const x = d(doc);
  out(io, `${p(x.name)} (${p(or(x.id, args.entity_id))})`);
  out(io, `Mentions: ${p(x.mention_count)}`);
  const r = items(x, "observations");
  if (!r.length) out(io, "No observations yet.");
  for (const o of r) out(io, `${p(o.mentioned_at)}  ${p(o.text)}`);
};

const rReceipt: R = (io, doc, args) => {
  const x = d(doc);
  if (args.explain) {
    out(io, `Memory ${p(or(x.memory_id, args.memory_id))}: ${p(x.outcome)}`);
    if (truthy(x.stage)) out(io, `Stage: ${p(x.stage)}`);
    if (truthy(x.detail)) out(io, `Detail: ${p(x.detail)}`);
    return;
  }
  out(io, `Receipt ${p(or(x.request_id, args.request_id))} for space '${p(x.space_id)}'`);
  out(io, `Included: ${items(x, "included").length}, excluded: ${items(x, "excluded").length}`);
  const acct = x.token_accounting;
  if (isObject(acct)) {
    const shown = Object.entries(acct)
      .filter(([, v]) => !(Array.isArray(v) || isObject(v)))
      .map(([k, v]) => `${printable(k)}=${p(v)}`);
    if (shown.length) out(io, "Tokens: " + shown.join(", "));
  }
};

const rAsk: R = (io, doc) => {
  const text = d(doc).insights;
  out(io, typeof text === "string" && text.trim() ? printableBlock(text) : "No answer.");
};

const rSpaceTypes: R = (io, doc) => {
  for (const t of items(doc, "items")) out(io, `${p(t.space_type)}  ${p(t.name)}`);
};

const rChat: R = (io, doc) => {
  const choices = d(doc).choices;
  const first = Array.isArray(choices) && choices.length && isObject(choices[0]) ? choices[0] : {};
  const content = d(first.message).content;
  out(io, typeof content === "string" ? printableBlock(content) : "No reply.");
};

const rOrg: R = (io, doc) => {
  const x = d(doc);
  out(io, `${p(x.org_id)}  ${p(x.name)}  ${p(x.slug)}`);
};

const rOrgs: R = (io, doc) => {
  for (const o of items(doc, "orgs")) out(io, `${p(or(o.org_id, o.id))}  ${p(o.name)}  ${p(o.role)}`);
};

const rProjects: R = (io, doc) => {
  for (const x of items(doc, "projects")) out(io, `${p(or(x.project_id, x.id))}  ${p(x.name)}`);
};

const rKeys: R = (io, doc) => {
  const r = items(doc, "keys");
  if (!r.length) out(io, "No API keys.");
  for (const k of r) {
    // The public prefix, not redacted like an id: redaction would turn it into
    // "anona_live_..." and the ellipsis below would double. Capped at the
    // column's width, so nothing longer than a prefix can be printed here.
    const prefix = head(printable(truthy(k.key_prefix) ? k.key_prefix : "-"), 20);
    out(io, `${prefix}…  ${p(k.name)}  ${p(k.created_at)}  ${p(k.last_used_at)}`);
  }
};

const memberRow = (m: Record<string, unknown>) => `${p(or(m.member_id, m.id))}  ${p(m.email)}  ${p(m.role)}`;

const rMembers: R = (io, doc) => {
  for (const m of items(doc, "members")) out(io, memberRow(m));
};

const rMember: R = (io, doc) => out(io, memberRow(d(doc)));

const rMemberInvitations: R = (io, doc) => {
  const r = items(doc, "invitations");
  if (!r.length) out(io, "No pending invitations.");
  for (const i of r) out(io, `${p(i.invitation_id)}  ${p(i.email)}  ${p(i.role)}`);
};

const rPending: R = (io, doc) => {
  const r = items(doc, "invitations");
  if (!r.length) out(io, "No invitations are waiting on you.");
  for (const i of r) out(io, `${p(i.token)}  ${p(i.kind)}  ${p(or(i.space_id, i.org_name))}  ${p(i.role)}`);
};

const rAccept: R = (io, doc) => {
  const x = d(doc);
  out(io, `Joined ${p(or(x.org_name, x.space_id, x.org_id))}.`);
};

const rBilling: R = (io, doc) => {
  const x = d(doc);
  out(io, `Plan: ${p(or(x.plan_name, x.plan_id))}`);
  let line = `Credits: ${p(x.credits_remaining)} remaining of ${p(x.credits_limit)} (${p(x.credits_used)} used`;
  if (truthy(x.credits_banked)) line += `, ${p(x.credits_banked)} banked`;
  out(io, line + ").");
  out(io, `Period ends: ${p(x.credits_reset_at)}`);
};

const rTopups: R = (io, doc) => {
  const r = items(doc, "items");
  if (!r.length) out(io, "No top-ups.");
  for (const t of r) out(io, `${p(t.id)}  ${p(t.purchased_at)}  ${p(t.pack)}  ${p(t.credits)} credits  ${p(t.status)}`);
};

const rHistory: R = (io, doc) => {
  const x = d(doc);
  const r = items(x, "series");
  if (!r.length) out(io, `No usage in the last ${p(x.period_days)} days.`);
  for (const e of r) out(io, `${p(e.date)}  ${p(e.ops)} ops`);
};

const rBreakdown: R = (io, doc) => {
  const x = d(doc);
  const r = items(x, "breakdown");
  if (!r.length) out(io, `No usage in the last ${p(x.period_days)} days.`);
  for (const e of r) out(io, `${p(e.space_id)}  ${p(e.memories_added)} added  ${p(e.searches)} searches  ${p(e.insights)} insights`);
};

const rExports: R = (io, doc, _a, space) => {
  const r = items(doc, "items");
  if (!r.length) out(io, `No exports of space '${p(space)}'.`);
  for (const j of r) out(io, `${p(j.job_id)}  ${p(j.status)}  ${p(j.created_at)}`);
};

const rDownloaded: R = (io, doc) => {
  const x = d(doc);
  out(io, `Saved ${count(x.bytes)} bytes to ${p(x.path)}.`);
};

const rSpaceMembers: R = (io, doc, args) => {
  const r = items(doc, "members");
  if (!r.length) out(io, `No members in space '${p(args.space_id)}'.`);
  for (const m of r) out(io, memberRow(m));
};

const say =
  (template: string, attr: string): R =>
  (io, _doc, args) =>
    out(io, template.replace("{}", () => p(args[attr])));

export const RENDER: Record<string, Render> = {
  "models list": rModelsList,
  "models create": rModelsCreate,
  "models get": renderModel,
  "models edit": say("Updated memory model {}.", "model_id"),
  "models delete": say("Deleted memory model {}.", "model_id"),
  "models refresh": rModelsRefresh,
  "models clear": say("Cleared {}; its definition is kept.", "model_id"),
  "models history": rModelsHistory,
  "profile set": renderProfile,
  "profile reset": (io, _doc, _args, space) => out(io, `Reset the profile of space '${p(space)}'.`),
  "rules list": rRulesList,
  "rules add": (io, doc) => out(io, `Added rule ${p(d(doc).id)}.`),
  "rules edit": say("Updated rule {}.", "rule_id"),
  "rules delete": say("Deleted rule {}.", "rule_id"),
  "webhooks list": rWebhooksList,
  "webhooks create": rWebhookCreated,
  "webhooks edit": say("Updated webhook {}.", "webhook_id"),
  "webhooks delete": say("Deleted webhook {}.", "webhook_id"),
  "webhooks deliveries": rDeliveries,
  graph: rGraph,
  "graph draw": rChart,
  "entities list": rEntities,
  "entities get": rEntity,
  "receipts get": rReceipt,
  "ask-user": rAsk,
  "space-types": rSpaceTypes,
  chat: rChat,
  org: rOrg,
  "orgs list": rOrgs,
  "projects list": rProjects,
  "keys list": rKeys,
  "members list": rMembers,
  "members me": rMember,
  "members invitations": rMemberInvitations,
  "invitations pending": rPending,
  "invitations accept": rAccept,
  "invitations decline": (io) => out(io, "Declined."),
  billing: rBilling,
  "billing topups": rTopups,
  "usage history": rHistory,
  "usage breakdown": rBreakdown,
  "exports list": rExports,
  "exports create": (io, doc) => out(io, `Export ${p(d(doc).job_id)} started.`),
  "exports download": rDownloaded,
  "spaces members list": rSpaceMembers,
};
