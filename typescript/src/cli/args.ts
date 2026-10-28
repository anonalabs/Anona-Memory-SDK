/**
 * The `anona` grammar, as data.
 *
 * Every command is declared once in `COMMANDS`, and the parser, the help, the
 * command tree (`tree`), `anona guide` and `anona completion` all come from
 * that one table. It mirrors the pip package's argparse definitions flag for
 * flag -- the same names, help, validation and exit 2 for a usage error -- so
 * a command line that works with one `anona` works with the other, and the two
 * `guide --json` trees are the same document.
 *
 * The parser follows argparse where a script could tell the difference:
 * options before or after positionals, `--flag=value`, unambiguous prefixes
 * (`--desc` for `--description`), `--` ending the options, repeated flags (the
 * last wins, except an `append` flag, which adds up), mutually exclusive flags,
 * and a usage error printed as `usage: ...` plus `<prog>: error: <message>` on
 * stderr, redacted, exit 2.
 */
import { readFileSync } from "node:fs";
import { redact } from "./credential.js";
import { printable } from "./py.js";
import { addCommands, profileActions, spaceMembers, usageFlags, type Helpers } from "./resources.js";
import { BadProfile, checkProfile } from "./store.js";

/** A value converter's refusal, printed as `argument --x: <message>`. */
export class ArgTypeError extends Error {}

export type OptionKind = "value" | "append" | "store_true" | "bool_optional";

export interface OptionSpec {
  /** e.g. ["--limit"], or ["--quiet", "-q"]. A bool_optional also takes `--no-<flag>`. */
  flags: string[];
  dest: string;
  help: string;
  kind: OptionKind;
  /** Only when argparse was given one; otherwise the tree shows DEST. */
  metavar?: string;
  required?: boolean;
  choices?: readonly string[];
  /** Throw ArgTypeError for a message, or anything else for `invalid <typeName> value`. */
  type?: (value: string) => unknown;
  /** The converter's name in argparse's `invalid <name> value: 'x'`. */
  typeName?: string;
  /** Flags sharing a group may not be given together. */
  exclusive?: string;
  default?: unknown;
}

export interface PositionalSpec {
  dest: string;
  help: string;
  metavar?: string;
  /** "?" optional, "+" one or more, "*" any number; absent means exactly one. */
  nargs?: "?" | "+" | "*";
  choices?: readonly string[];
  type?: (value: string) => unknown;
  typeName?: string;
}

export interface CommandSpec {
  name: string;
  help: string;
  /** Printed under the usage line of this command's own --help. */
  description?: string;
  positionals?: PositionalSpec[];
  options?: OptionSpec[];
  /** Subcommands, chosen by the first positional (stored as `action`). */
  actions?: CommandSpec[];
  /** What argparse calls the subcommand choice in its messages; `action` unless a nested group says otherwise. */
  actionDest?: string;
}

// --- converters, as in the pip package's argparse types ------------------------

/** Python's `int()` of a command-line string. */
function pyInt(value: string): number {
  const t = value.trim();
  if (!/^[+-]?\d+(?:_\d+)*$/.test(t)) throw new Error("not an int");
  return Number(t.replace(/_/g, ""));
}

/** Python's `float()` of a command-line string. */
function pyFloat(value: string): number {
  const t = value.trim().replace(/(\d)_(?=\d)/g, "$1");
  if (/^[+-]?(inf|infinity)$/i.test(t)) return t.startsWith("-") ? -Infinity : Infinity;
  if (/^[+-]?nan$/i.test(t)) return NaN;
  if (!/^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/.test(t)) throw new Error("not a float");
  return Number(t);
}

/** argparse names this converter `parse` (it is the inner function of a factory). */
export function limitBetween(low: number, high: number): (value: string) => number {
  return (value) => {
    const n = pyInt(value);
    if (!(low <= n && n <= high)) throw new ArgTypeError(`must be between ${low} and ${high}`);
    return n;
  };
}

function offset(value: string): number {
  const n = pyInt(value);
  if (n < 0) throw new ArgTypeError("must be 0 or more");
  return n;
}

function minScore(value: string): number {
  const x = pyFloat(value);
  if (!(x >= 0 && Number.isFinite(x))) throw new ArgTypeError("must be a number, 0 or more");
  return x;
}

function profileName(value: string): string {
  try {
    return checkProfile(value);
  } catch (e) {
    if (e instanceof BadProfile) throw new ArgTypeError(e.message);
    throw e;
  }
}

function timeoutSeconds(value: string): number {
  let x: number;
  try {
    x = pyFloat(value);
  } catch {
    throw new ArgTypeError("must be a number of seconds");
  }
  if (!(x > 0 && Number.isFinite(x))) throw new ArgTypeError("must be more than 0");
  return x;
}

function retries(value: string): number {
  const n = pyInt(value);
  if (!(n >= 0 && n <= 10)) throw new ArgTypeError("must be between 0 and 10");
  return n;
}

function positive(value: string): number {
  const n = pyInt(value);
  if (n < 1) throw new ArgTypeError("must be 1 or more");
  return n;
}

function seconds(value: string): number {
  let x: number;
  try {
    x = pyFloat(value);
  } catch {
    throw new ArgTypeError("must be a number of seconds");
  }
  if (!(x >= 0 && x < Infinity)) throw new ArgTypeError("must be 0 or more");
  return x;
}

/**
 * A space or scope value. Empty or blank is a usage error (exit 2), never
 * sent: an empty scope is almost certainly a variable that was not set, and an
 * empty space reaches the server as no space at all.
 */
export function nonEmpty(value: string): string {
  if (!value.trim()) throw new ArgTypeError("must not be empty");
  return value;
}

// Node's error codes, in the names the pip package prints for the same failure.
const FS_ERROR_NAMES: Record<string, string> = {
  ENOENT: "FileNotFoundError",
  EISDIR: "IsADirectoryError",
  EACCES: "PermissionError",
  EPERM: "PermissionError",
  ENOTDIR: "NotADirectoryError",
};

/** The pip package's name for why a file could not be read as UTF-8 text. */
export function readErrorName(e: unknown): string {
  if (e instanceof TypeError) return "UnicodeDecodeError";
  return FS_ERROR_NAMES[(e as NodeJS.ErrnoException).code ?? ""] ?? "OSError";
}

/** A file's text, strictly UTF-8 like Python's `open(..., encoding="utf-8")`. */
export function readUtf8(path: string): string {
  return new TextDecoder("utf-8", { fatal: true }).decode(readFileSync(path));
}

/** `@path` reads the value from a file; anything else is the value itself. */
function readArgFile(value: string): string {
  if (!value.startsWith("@")) return value;
  try {
    return readUtf8(value.slice(1));
  } catch (e) {
    throw new ArgTypeError(`could not read ${printable(redact(value.slice(1)))} (${readErrorName(e)})`);
  }
}

/**
 * `--tag-groups`: a JSON array of tag-group expressions (one object is taken as
 * a one-element array), or `@path`. Only the shape is checked here; the grammar
 * and the limits are the server's.
 */
function tagGroups(value: string): unknown[] {
  const text = readArgFile(value);
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new ArgTypeError('must be JSON, e.g. \'[{"tags": ["project:alpha"], "match": "any"}]\'');
  }
  const isObj = (x: unknown) => x !== null && typeof x === "object" && !Array.isArray(x);
  if (isObj(parsed)) parsed = [parsed];
  if (!Array.isArray(parsed) || !parsed.length || !parsed.every(isObj)) {
    throw new ArgTypeError("must be a JSON array of one or more objects");
  }
  return parsed;
}

function textArg(value: string): string {
  return readArgFile(value);
}

function topics(value: string): string[] {
  const list = value.split(",").map((t) => t.trim());
  if (!list.every((t) => t)) throw new ArgTypeError("must be a comma-separated list of non-empty topics");
  return list;
}

function routingRules(value: string): unknown[] {
  const text = readArgFile(value.startsWith("@") ? value : "@" + value);
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new ArgTypeError("must hold a JSON array of routing rules");
  }
  if (!Array.isArray(parsed) || !parsed.every((g) => g !== null && typeof g === "object" && !Array.isArray(g))) {
    throw new ArgTypeError("must hold a JSON array of routing-rule objects");
  }
  return parsed;
}

function labels(value: string): unknown[] {
  const text = readArgFile(value.startsWith("@") ? value : "@" + value);
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new ArgTypeError("must hold a JSON array of label groups");
  }
  if (!Array.isArray(parsed) || !parsed.every((g) => g !== null && typeof g === "object" && !Array.isArray(g))) {
    throw new ArgTypeError("must hold a JSON array of label-group objects");
  }
  return parsed;
}

// datetime.fromisoformat (Python 3.11+), which the pip package uses as its check.
function digits(s: string, from: number, n: number): number {
  const part = s.slice(from, from + n);
  if (part.length !== n || !/^[0-9]+$/.test(part)) throw new Error("bad digits");
  return Number(part);
}

function daysIn(year: number, month: number): number {
  return new Date(Date.UTC(year, month, 0)).getUTCDate();
}

function isoWeekCheck(year: number, week: number, day: number): void {
  if (year < 1 || year > 9999) throw new Error("year");
  if (!(week > 0 && week < 53)) {
    let bad = true;
    if (week === 53) {
      // ISO years have 53 weeks when they start on a Thursday, or on a
      // Wednesday in a leap year.
      const first = new Date(Date.UTC(year, 0, 1)).getUTCDay();
      const leap = daysIn(year, 2) === 29;
      if (first === 4 || (first === 3 && leap)) bad = false;
    }
    if (bad) throw new Error("week");
  }
  if (!(day > 0 && day < 8)) throw new Error("weekday");
}

function separatorAt(s: string): number {
  const n = s.length;
  if (n === 7) return 7;
  const isDigit = (c: string | undefined) => c !== undefined && c >= "0" && c <= "9";
  if (s[4] === "-") {
    if (s[5] === "W") {
      if (n < 8) throw new Error("short");
      if (n > 8 && s[8] === "-") {
        if (n === 9) throw new Error("short");
        if (n > 10 && isDigit(s[10])) return 8;
        return 10;
      }
      return 8;
    }
    return 10;
  }
  if (s[4] === "W") {
    let idx = 7;
    while (idx < n && isDigit(s[idx])) idx++;
    if (idx < 9) return idx;
    return idx % 2 === 0 ? 7 : 8;
  }
  return 8;
}

function parseDate(d: string): void {
  if (![7, 8, 10].includes(d.length)) throw new Error("length");
  const year = digits(d, 0, 4);
  const sep = d[4] === "-";
  let pos = 4 + (sep ? 1 : 0);
  if (d[pos] === "W") {
    pos += 1;
    const week = digits(d, pos, 2);
    pos += 2;
    let day = 1;
    if (d.length > pos) {
      if ((d[pos] === "-") !== sep) throw new Error("dash");
      pos += sep ? 1 : 0;
      day = digits(d, pos, 1);
      if (d.length !== pos + 1) throw new Error("trailing");
    }
    isoWeekCheck(year, week, day);
    return;
  }
  const month = digits(d, pos, 2);
  pos += 2;
  if ((d[pos] === "-") !== sep) throw new Error("dash");
  pos += sep ? 1 : 0;
  const day = digits(d, pos, 2);
  if (d.length !== pos + 2) throw new Error("trailing");
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > daysIn(year, month)) throw new Error("range");
}

function parseHms(t: string): [number, number, number] {
  const comps = [0, 0, 0];
  let pos = 0;
  let hasSep = false;
  for (let comp = 0; comp < 3; comp++) {
    if (t.length - pos < 2) throw new Error("incomplete");
    comps[comp] = digits(t, pos, 2);
    pos += 2;
    const next = t[pos];
    if (comp === 0) hasSep = next === ":";
    if (next === undefined || comp >= 2) break;
    if (hasSep && next !== ":") throw new Error("separator");
    pos += hasSep ? 1 : 0;
  }
  if (pos < t.length) {
    if (t[pos] !== "." && t[pos] !== ",") throw new Error("fraction");
    pos += 1;
    const rest = t.slice(pos);
    const take = Math.min(rest.length, 6);
    if (take === 0 || !/^[0-9]+$/.test(rest.slice(0, take)) || !/^[0-9]*$/.test(rest.slice(take))) {
      throw new Error("fraction");
    }
  }
  return comps as [number, number, number];
}

function parseTime(t: string): void {
  if (t.length < 2) throw new Error("short");
  const dash = t.indexOf("-");
  const plus = t.indexOf("+");
  const z = t.indexOf("Z");
  const tzPos = dash >= 0 ? dash + 1 : plus >= 0 ? plus + 1 : z >= 0 ? z + 1 : 0;
  const timePart = tzPos > 0 ? t.slice(0, tzPos - 1) : t;
  const [h, m, s] = parseHms(timePart);
  if (h > 23 || m > 59 || s > 59) throw new Error("range");
  if (tzPos === t.length && t.endsWith("Z")) return;
  if (tzPos > 0) {
    const tz = t.slice(tzPos);
    if ([0, 1, 3].includes(tz.length)) throw new Error("tz");
    const [th, tm, ts] = parseHms(tz);
    // timezone() takes strictly less than a day.
    if (th * 3600 + tm * 60 + ts >= 86400) throw new Error("tz range");
  }
}

function fromIsoFormat(s: string): void {
  if (s.length < 7 || /[^\x00-\x7f]/.test(s)) throw new Error("short");
  const at = separatorAt(s);
  parseDate(s.slice(0, at));
  const t = s.slice(at + 1);
  // A separator with nothing after it is an error in CPython's C parser (the
  // one that runs), though not in its pure-Python fallback.
  if (s.length > at && !t) throw new Error("empty time");
  if (t) parseTime(t);
}

/**
 * Checked here so a typo is a usage error (exit 2) that sends nothing, rather
 * than a metered round trip that comes back 422. The string is sent exactly as
 * typed, trimmed: parsing is only the check.
 */
export function iso8601(value: string): string {
  const text = value.trim();
  try {
    fromIsoFormat(/[Zz]$/.test(text) ? text.slice(0, -1) + "+00:00" : text);
  } catch {
    throw new ArgTypeError("must be an ISO 8601 date or date-time, e.g. 2026-06-01 or 2026-06-01T09:00:00Z");
  }
  return text;
}

/** Comma-separated; an empty entry (`a,,b`, a trailing comma) is a typo, never a tag. */
function tags(value: string): string[] {
  const list = value.split(",").map((t) => t.trim());
  if (!list.every((t) => t)) throw new ArgTypeError("must be a comma-separated list of non-empty tags");
  return list;
}

function metadata(value: string): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new ArgTypeError('must be a JSON object, e.g. \'{"source": "cli"}\'');
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new ArgTypeError("must be a JSON object, not a list, string or number");
  }
  return parsed as Record<string, unknown>;
}

// --- the registry --------------------------------------------------------------

export const DEFAULT_BASE_URL_TEXT = "https://memory.anonalabs.com";
const DEFAULT_API_BASE_URL_TEXT = "https://api.anonalabs.com";
const JSON_HELP = "Print the API response as one JSON document on stdout";
export const TAGS_MATCH = ["any", "all", "any_strict", "all_strict", "exact"] as const;

// An account -- reached with an API key or the command-line login -- can hold
// many spaces and the server implies none, so both need one named. A temporary
// profile has exactly one space. The MCP login is refused outright.
const spaceHelp = (verb: string) =>
  `Space to ${verb}. Required with an API key or \`anona login\` unless ANONA_SPACE_ID is set; ` +
  "a temporary profile uses `default`";
const API_KEY_HELP =
  "API key for this command (else $ANONA_API_KEY, else ~/.anona/config.env). " +
  "Visible to other local users in the process list; prefer the variable";

/** Accepted before or after any command, as in the pip package. */
export const COMMON_OPTIONS: OptionSpec[] = [
  {
    flags: ["--base-url"],
    dest: "base_url",
    kind: "value",
    help:
      "Anona server. Default: $ANONA_BASE_URL; otherwise " +
      `${DEFAULT_API_BASE_URL_TEXT} for a command using an API key, and ` +
      `${DEFAULT_BASE_URL_TEXT} for login and start. A stored login or ` +
      "temporary profile always uses the server that issued it",
  },
  { flags: ["--no-browser"], dest: "no_browser", kind: "store_true", help: "Used by `anona login` only: print the approval URL instead of opening a browser" },
  {
    flags: ["--profile"],
    dest: "profile",
    kind: "value",
    type: profileName,
    typeName: "_profile_name",
    help: "Use a named profile: its own credentials and config.env under ~/.anona/profiles/NAME (default: $ANONA_PROFILE, else ~/.anona)",
  },
  {
    flags: ["--timeout"],
    dest: "timeout",
    kind: "value",
    type: timeoutSeconds,
    typeName: "_timeout",
    metavar: "SECONDS",
    help: "Give up on one HTTP request after this long (default 120)",
  },
  {
    flags: ["--retries"],
    dest: "retries",
    kind: "value",
    type: retries,
    typeName: "_retries",
    metavar: "N",
    help:
      "Retry a 429, or a 5xx on a request that is safe to repeat, up to N times " +
      "(default 2, honouring Retry-After). A write is never re-sent after a 5xx or a timeout",
  },
  {
    flags: ["--quiet", "-q"],
    dest: "quiet",
    kind: "store_true",
    help: "No progress or retry notices on stderr; results and errors still print",
  },
];

const VERSION_OPTION: OptionSpec = { flags: ["--version"], dest: "__version", kind: "store_true", help: "print the installed version and exit" };

// Printed under the top-level --help, as argparse prints the parser's epilog.
export const ROOT_EPILOG =
  "New here, or an agent? Run `anona guide` for every command, flag, exit code and recipe, or `anona` alone for where to start.";

export const ROOT_DESCRIPTION =
  "Anona Memory: sign in, connect MCP clients, record, retrieve or reason over memories, and manage spaces, memories and jobs.";

function creds(spaceVerb: string | null): OptionSpec[] {
  const out: OptionSpec[] = [];
  if (spaceVerb !== null) {
    out.push({ flags: ["--space"], dest: "space", kind: "value", type: nonEmpty, typeName: "non_empty", help: spaceHelp(spaceVerb) });
  }
  out.push({ flags: ["--api-key"], dest: "api_key", kind: "value", help: API_KEY_HELP });
  out.push({ flags: ["--json"], dest: "json", kind: "store_true", help: JSON_HELP });
  return out;
}

function scope(lead: string): OptionSpec[] {
  return ["user", "agent", "session"].map((s) => ({
    flags: [`--${s}-id`],
    dest: `${s}_id`,
    kind: "value" as const,
    type: nonEmpty,
    typeName: "non_empty",
    help: `${lead} this ${s}_id`,
  }));
}

const yes = (what: string): OptionSpec => ({
  flags: ["--yes"],
  dest: "yes",
  kind: "store_true",
  help: `Confirm the ${what}; without it nothing is sent`,
});

const ISO = { type: iso8601, typeName: "_iso8601" };
const OFFSET = { type: offset, typeName: "_offset" };

function waitFlags(): OptionSpec[] {
  return [
    {
      flags: ["--wait"],
      dest: "wait",
      kind: "store_true",
      help: "Poll the background job(s) until they finish; the exit code reports the outcome",
    },
    {
      flags: ["--wait-timeout"],
      dest: "wait_timeout",
      kind: "value",
      type: seconds,
      typeName: "_seconds",
      metavar: "SECONDS",
      help: "With --wait, give up after this long and exit 8 (default 300)",
    },
  ];
}

/**
 * The three per-space settings. Each PUT is a FULL REPLACE on the server, so
 * `set` reads, changes the named fields and writes every field back. The field
 * lists are the request models' fields exactly.
 */
export const SETTINGS_FIELDS: Record<string, readonly string[]> = {
  "chat-settings": ["memory_limit", "memory_token_budget", "auto_record", "memory", "chat_model"],
  "reason-settings": ["model", "depth"],
  "extraction-settings": ["mode", "guidance", "custom_prompt", "labels", "free_form_entities"],
  "routing-settings": ["charter", "topics", "rules", "auto_route_enabled", "shared_auto_route"],
};

/** The fields a `... set --clear` may unset: the two switches are booleans the API will not take as null. */
export const CLEARABLE: Record<string, readonly string[]> = Object.fromEntries(
  Object.entries(SETTINGS_FIELDS).map(([kind, fields]) => [kind, fields.filter((f) => f !== "auto_route_enabled" && f !== "shared_auto_route")]),
);

const SETTINGS_BLURBS: Record<string, string> = {
  "chat-settings": "Show or change a space's defaults for the drop-in LLM proxy",
  "reason-settings": "Show or change which model and depth `reason` uses in a space",
  "extraction-settings": "Show or change how a space turns text into memories",
  "routing-settings": "Show or change how writes and reads are routed between spaces",
};

function settingsFieldOptions(kind: string): OptionSpec[] {
  if (kind === "reason-settings") {
    return [
      { flags: ["--model"], dest: "model", kind: "value", help: "A model id or tier name from `anona llm-models`" },
      { flags: ["--depth"], dest: "depth", kind: "value", choices: ["fast", "thorough"], help: "How deep `reason` reads" },
    ];
  }
  if (kind === "chat-settings") {
    return [
      { flags: ["--memory-limit"], dest: "memory_limit", kind: "value", type: limitBetween(1, 20), typeName: "parse", help: "Memories injected per turn (1-20)" },
      {
        flags: ["--memory-token-budget"],
        dest: "memory_token_budget",
        kind: "value",
        type: limitBetween(1, 100_000),
        typeName: "parse",
        help: "Token cap on the injected block (1-100000)",
      },
      { flags: ["--auto-record"], dest: "auto_record", kind: "bool_optional", help: "Record each proxied turn" },
      { flags: ["--memory"], dest: "memory", kind: "bool_optional", help: "Inject memories into proxied calls" },
      { flags: ["--chat-model"], dest: "chat_model", kind: "value", help: "The LLM proxied calls use (see `anona llm-models`)" },
    ];
  }
  if (kind === "routing-settings") {
    return [
      { flags: ["--charter"], dest: "charter", kind: "value", type: textArg, typeName: "_text_arg", help: "What belongs in this space, in words; @FILE reads it" },
      { flags: ["--topics"], dest: "topics", kind: "value", type: topics, typeName: "_topics", help: "Comma-separated topics the space covers" },
      {
        flags: ["--rules-file"],
        dest: "rules",
        kind: "value",
        type: routingRules,
        typeName: "_rules",
        metavar: "PATH",
        help: 'A JSON array of routing rules, e.g. [{"field":"agent_id","op":"equals","value":"billing-bot","space_id":"billing"}]',
      },
      { flags: ["--auto-route"], dest: "auto_route_enabled", kind: "bool_optional", help: "Let writes with no space land by topic (--no-auto-route turns it off)" },
      { flags: ["--shared-auto-route"], dest: "shared_auto_route", kind: "bool_optional", help: "Let writes route into spaces shared with you" },
    ];
  }
  return [
    {
      flags: ["--mode"],
      dest: "mode",
      kind: "value",
      choices: ["concise", "verbose", "verbatim", "custom"],
      help: "How much of the text becomes memories",
    },
    { flags: ["--guidance"], dest: "guidance", kind: "value", type: textArg, typeName: "_text_arg", help: "Added to the extraction rules; @FILE reads it" },
    {
      flags: ["--custom-prompt"],
      dest: "custom_prompt",
      kind: "value",
      type: textArg,
      typeName: "_text_arg",
      help: "Replaces the rules (needs --mode custom); @FILE reads it",
    },
    {
      flags: ["--labels-file"],
      dest: "labels",
      kind: "value",
      type: labels,
      typeName: "_labels",
      metavar: "PATH",
      help:
        "A JSON array of label groups to classify memories by, e.g. " +
        '[{"key":"team","type":"value","values":[{"value":"billing"}]}]; ' +
        'each value is an object with "value", not a plain string',
    },
    {
      flags: ["--free-form-entities"],
      dest: "free_form_entities",
      kind: "bool_optional",
      help: "Keep entities outside the label groups (--no-... needs labels)",
    },
  ];
}

function settingsCommand(kind: string): CommandSpec {
  const fields = CLEARABLE[kind] as readonly string[];
  return {
    name: kind,
    help: SETTINGS_BLURBS[kind] as string,
    actions: [
      { name: "get", help: `Show the space's ${kind}`, options: creds("read") },
      {
        name: "set",
        help: "Change the named fields and keep the rest (read, modify, write back)",
        options: [
          ...creds("configure"),
          ...settingsFieldOptions(kind),
          {
            flags: ["--clear"],
            dest: "clear",
            kind: "append",
            choices: fields,
            metavar: "FIELD",
            help: `Unset a field so it follows the platform default; repeatable. One of: ${fields.join(", ")}`,
          },
        ],
      },
      { name: "reset", help: `Clear every ${kind} override (needs --yes)`, options: [yes("reset"), ...creds("reset")] },
    ],
  };
}

/** What resources.ts builds its grammar from. */
const HELPERS: Helpers = {
  creds,
  limit: limitBetween,
  offset,
  nonEmpty,
  textArg,
  refuse: (message) => {
    throw new ArgTypeError(message);
  },
};

const PROFILE_ACTIONS = profileActions(HELPERS);

const RESOURCE_COMMANDS: CommandSpec[] = [
  {
    name: "spaces",
    help: "List, create or show spaces",
    actions: [
      { name: "list", help: "List the spaces you can use", options: creds(null) },
      {
        name: "create",
        help: "Create a space",
        positionals: [{ dest: "name", help: "The new space's name, which is also its id" }],
        options: [{ flags: ["--description"], dest: "description", kind: "value", help: "What the space is for" }, ...creds(null)],
      },
      {
        name: "get",
        help: "Show one space",
        positionals: [{ dest: "space_id", help: "The space's id (owner:name for one shared with you)" }],
        options: creds(null),
      },
      {
        name: "delete",
        help: "Delete a space and every memory in it (needs --yes)",
        positionals: [{ dest: "space_id", help: "The space to delete" }],
        options: [yes("deletion"), ...creds(null)],
      },
      spaceMembers(HELPERS),
    ],
  },
  {
    name: "memories",
    help: "List, edit, delete or trace stored memories",
    actions: [
      {
        name: "list",
        help: "Page through a space's memories",
        options: [
          ...creds("list"),
          { flags: ["--limit"], dest: "limit", kind: "value", type: limitBetween(1, 200), typeName: "parse", help: "At most this many (1-200, default 50)" },
          { flags: ["--offset"], dest: "offset", kind: "value", ...OFFSET, help: "Skip this many first (default 0)" },
          { flags: ["--q"], dest: "q", kind: "value", help: "Only memories whose text or context contains this (case-insensitive)" },
          { flags: ["--type"], dest: "memory_type", kind: "value", help: "Only memories of this type: fact, experience, note or summary (facts a note covers are hidden unless --no-prefer-observations)" },
          {
            flags: ["--prefer-observations"],
            dest: "prefer_observations",
            kind: "bool_optional",
            help:
              "List a synthesized note instead of the facts behind it (the server default); " +
              "--no-prefer-observations lists both layers",
          },
          { flags: ["--member-id"], dest: "member_id", kind: "value", help: "Only memories this member wrote; `me` for your own (in a personal space every memory is yours, so `me` matches them all)" },
          {
            flags: ["--tag-groups"],
            dest: "tag_groups",
            kind: "value",
            type: tagGroups,
            typeName: "tag_groups",
            help: "Compound tag filter: a JSON array of {tags, match} / {and} / {or} / {not}; @FILE reads it",
          },
          ...scope("Only memories recorded with"),
        ],
      },
      {
        name: "edit",
        help: "Correct a fact's text in place (a note is rebuilt from its facts and cannot be edited)",
        positionals: [{ dest: "memory_id", help: "The memory to change" }],
        options: [
          { flags: ["--text"], dest: "text", kind: "value", required: true, help: "The corrected text" },
          { flags: ["--reason"], dest: "reason", kind: "value", help: "Why, kept on the memory's history" },
          ...creds("edit in"),
        ],
      },
      {
        name: "delete",
        help: "Delete one memory (needs --yes)",
        positionals: [{ dest: "memory_id", help: "The memory to delete" }],
        options: [yes("deletion"), ...creds("delete from")],
      },
      {
        name: "history",
        help: "Show every earlier version of one memory",
        positionals: [{ dest: "memory_id", help: "The memory to trace" }],
        options: creds("read"),
      },
    ],
  },
  {
    name: "documents",
    help: "Upload files into a space, list, show or delete them",
    actions: [
      {
        name: "upload",
        help: "Upload files to be read into memories",
        positionals: [{ dest: "paths", metavar: "PATH", nargs: "+", help: "The files to upload (at most 20 per call)" }],
        options: [
          ...creds("upload to"),
          { flags: ["--tags"], dest: "tags", kind: "value", help: "Comma-separated tags for every uploaded file" },
          { flags: ["--strategy"], dest: "strategy", kind: "value", help: "A named ingestion strategy (default: the space's)" },
          ...scope("Upload under"),
          ...waitFlags(),
        ],
      },
      {
        name: "list",
        help: "List a space's documents, newest first",
        options: [
          ...creds("list"),
          { flags: ["--limit"], dest: "limit", kind: "value", type: limitBetween(1, 500), typeName: "parse", help: "At most this many (1-500, default 100)" },
          { flags: ["--offset"], dest: "offset", kind: "value", ...OFFSET, help: "Skip this many first" },
          {
            flags: ["--source"],
            dest: "source",
            kind: "value",
            choices: ["file", "custom", "memory"],
            help: "Only this kind: file (uploaded), custom (your document_id), memory (implicit)",
          },
        ],
      },
      {
        name: "get",
        help: "Show one document",
        positionals: [{ dest: "document_id", help: "The document's id" }],
        options: creds("read"),
      },
      {
        name: "delete",
        help: "Delete a document and its memories (needs --yes)",
        positionals: [{ dest: "document_id", help: "The document to delete" }],
        options: [yes("deletion"), ...creds("delete from")],
      },
    ],
  },
  {
    name: "jobs",
    help: "Check or cancel a background ingestion job",
    actions: [
      {
        name: "get",
        help: "Show a job's status and the memories it made",
        positionals: [{ dest: "job_id", help: "The job id a background write returned" }],
        options: creds("read"),
      },
      {
        name: "cancel",
        help: "Cancel a job's queued parts (needs --yes)",
        positionals: [{ dest: "job_id", help: "The job to cancel" }],
        options: [yes("cancel"), ...creds("cancel in")],
      },
    ],
  },
  {
    name: "profile",
    help: "Show a space's profile (mission and disposition), or change it with set / reset",
    positionals: PROFILE_ACTIONS.positionals,
    options: [...creds("read or change"), ...PROFILE_ACTIONS.options],
  },
  {
    name: "model",
    help: "Show one memory model and its current content",
    positionals: [{ dest: "model_id", help: "The memory model's id" }],
    options: creds("read"),
  },
  {
    name: "user-profile",
    help: "Show what a space knows about one end user",
    positionals: [{ dest: "user_id", type: nonEmpty, typeName: "non_empty", help: "The user_id your writes are scoped with" }],
    options: [
      { flags: ["--limit"], dest: "limit", kind: "value", type: limitBetween(1, 200), typeName: "parse", help: "At most this many memories (1-200)" },
      { flags: ["--offset"], dest: "offset", kind: "value", ...OFFSET, help: "Skip this many memories first" },
      ...creds("read"),
    ],
  },
  {
    name: "usage",
    help: "Show your credits and rate limit, or your operation counts over time",
    options: [...creds(null), ...usageFlags(HELPERS)],
  },
  {
    name: "llm-models",
    help: "List the LLMs you can choose and what each costs (not the memory models: see `model`)",
    options: creds(null),
  },
  settingsCommand("chat-settings"),
  settingsCommand("reason-settings"),
  settingsCommand("extraction-settings"),
  settingsCommand("routing-settings"),
  ...addCommands(HELPERS),
];

/** `--cli` / `--mcp`. Neither: login asks for the one command-line approval, which `anona mcp` uses too; logout removes both. */
const which = (verb: string): OptionSpec[] => [
  {
    flags: ["--cli"],
    dest: "cli",
    kind: "store_true",
    exclusive: "which",
    help:
      verb === "approve"
        ? "approve the command-line credential only, with no fallback to MCP (record, retrieve, reason, the other data commands and `anona mcp`)"
        : `only ${verb} the command-line credential (record, retrieve, reason and the other data commands)`,
  },
  {
    flags: ["--mcp"],
    dest: "mcp",
    kind: "store_true",
    exclusive: "which",
    help: verb === "approve" ? "approve a separate MCP-only credential for `anona mcp` instead" : `only ${verb} the MCP credential (\`anona mcp\`)`,
  },
];

/** Every command, in the order the pip package's parser declares them. */
export const COMMANDS: CommandSpec[] = [
  { name: "start", help: "Create a temporary profile with no account" },
  {
    name: "record",
    help: "Store a memory (no MCP needed)",
    positionals: [{ dest: "content", nargs: "?", help: "The fact to store, as a complete sentence; `-` reads it from stdin" }],
    options: [
      { flags: ["--file"], dest: "file", kind: "value", metavar: "PATH", help: "Read the content from this file instead" },
      ...creds("write to"),
      {
        flags: ["--tags"],
        dest: "tags",
        kind: "append",
        type: tags,
        typeName: "_tags",
        help: "Comma-separated tags to file the memory under; repeating the flag adds more",
      },
      { flags: ["--timestamp"], dest: "timestamp", kind: "value", ...ISO, help: "When the event happened (ISO 8601), not when it is recorded" },
      { flags: ["--metadata"], dest: "metadata", kind: "value", type: metadata, typeName: "_metadata", help: "A JSON object stored with the memory" },
      {
        flags: ["--async"],
        dest: "background",
        kind: "store_true",
        help: "Queue the write and return a job id at once instead of waiting for extraction",
      },
      {
        flags: ["--batch"],
        dest: "batch",
        kind: "value",
        metavar: "FILE",
        help:
          'Record up to 100 memories from a JSON Lines file (`-` for stdin): one {"content": ...} object or JSON string per line. ' +
          "Always queued",
      },
      ...waitFlags(),
      ...scope("Record under"),
    ],
  },
  {
    name: "retrieve",
    help: "Search your memories (no MCP needed)",
    positionals: [{ dest: "query", help: "What to search for" }],
    options: [
      ...creds("search"),
      { flags: ["--limit"], dest: "limit", kind: "value", type: limitBetween(1, 100), typeName: "_limit", help: "At most this many memories (1-100, default 10)" },
      {
        flags: ["--min-score"],
        dest: "min_score",
        kind: "value",
        type: minScore,
        typeName: "_min_score",
        help: "Drop memories scoring below this. Scores span orders of magnitude and a correct match can score far below 0.01; see the docs before setting one",
      },
      ...scope("Only memories recorded with"),
      {
        flags: ["--tags"],
        dest: "tags",
        kind: "append",
        type: tags,
        typeName: "_tags",
        help: "Only memories carrying these comma-separated tags; repeating the flag adds more",
      },
      {
        flags: ["--tags-match"],
        dest: "tags_match",
        kind: "value",
        choices: TAGS_MATCH,
        help:
          "How --tags must match (default: any). any and all also return memories with no tags at all; " +
          "any_strict, all_strict and exact exclude untagged memories",
      },
      { flags: ["--as-of"], dest: "as_of", kind: "value", ...ISO, help: "Only memories recorded at or before this instant (ISO 8601)" },
      { flags: ["--occurred-after"], dest: "occurred_after", kind: "value", ...ISO, help: "Only memories about events at or after this (ISO 8601)" },
      { flags: ["--occurred-before"], dest: "occurred_before", kind: "value", ...ISO, help: "Only memories about events at or before this (ISO 8601)" },
      {
        flags: ["--tag-groups"],
        dest: "tag_groups",
        kind: "value",
        type: tagGroups,
        typeName: "tag_groups",
        help: "Compound tag filter: a JSON array of {tags, match} / {and} / {or} / {not}, AND-ed together and with any scope; @FILE reads it",
      },
      { flags: ["--member-id"], dest: "member_id", kind: "value", help: "Only memories this member wrote; `me` for your own (in a personal space every memory is yours, so `me` matches them all)" },
      {
        flags: ["--prefer-observations"],
        dest: "prefer_observations",
        kind: "bool_optional",
        help:
          "Return a synthesized note instead of the facts behind it (the server default); " +
          "--no-prefer-observations returns both layers",
      },
      {
        flags: ["--format"],
        dest: "format",
        kind: "value",
        choices: ["results", "block"],
        default: "results",
        help: "results: one line per memory (default); block: the prompt-ready context string",
      },
      { flags: ["--max-tokens"], dest: "max_tokens", kind: "value", type: positive, typeName: "_positive", help: "With --format block, cap the block at about this many tokens" },
    ],
  },
  {
    name: "reason",
    help: "Ask a question and get a synthesized answer",
    positionals: [{ dest: "query", help: "The question to answer from your memories" }],
    options: [
      ...creds("reason over"),
      {
        flags: ["--depth"],
        dest: "depth",
        kind: "value",
        choices: ["fast", "thorough"],
        help: "fast lets a current memory model answer alone; thorough always reads the memories under it",
      },
      ...scope("Reason only over memories recorded with"),
    ],
  },
  ...RESOURCE_COMMANDS,
  {
    name: "login",
    help: "Approve access in a browser, once, for the command line and MCP",
    // Claiming has no command of its own -- it is what `login` does when a
    // temporary profile is stored -- so `login --help` is where it must be said.
    description:
      "Approve access in a browser, once: the command line and `anona mcp` both use the login. " +
      "With a temporary profile stored (from `anona start`), approving also claims it: " +
      "the account you sign in with, which needs an email not yet used for Anona, " +
      "takes over the profile and everything in it.",
    options: which("approve"),
  },
  {
    name: "logout",
    help: "Delete the stored credentials",
    options: [
      ...which("remove"),
      { flags: ["--force"], dest: "force", kind: "store_true", help: "also abandon an unclaimed temporary profile (it cannot be recovered)" },
    ],
  },
  {
    name: "status",
    help: "Show whether you are signed in",
    options: [{ flags: ["--api-key"], dest: "api_key", kind: "value", help: "Describe this key's source instead of the usual lookup" }],
  },
  { name: "mcp", help: "Run the stdio MCP proxy for a client to launch" },
  {
    name: "doctor",
    help: "Check the install, which credential is used and why, and whether the server and space answer",
    options: [
      {
        flags: ["--space"],
        dest: "space",
        kind: "value",
        type: nonEmpty,
        typeName: "non_empty",
        help: "Space to check access to (default: the one a data command would use)",
      },
      { flags: ["--api-key"], dest: "api_key", kind: "value", help: API_KEY_HELP },
      { flags: ["--json"], dest: "json", kind: "store_true", help: JSON_HELP },
    ],
  },
  {
    name: "guide",
    help: "Print a compact reference for agents: every command, flag, exit code and output shape",
    options: [{ flags: ["--json"], dest: "json", kind: "store_true", help: "The command tree as JSON, generated from this parser" }],
  },
  {
    name: "help",
    help: "Show help for anona or one command (`help spaces delete`); `help --llm` prints the agent reference",
    positionals: [{ dest: "topic", nargs: "*", metavar: "COMMAND", help: "The command to describe, e.g. `record` or `spaces delete`" }],
    options: [
      { flags: ["--llm"], dest: "llm", kind: "store_true", help: "Print `anona guide` instead" },
      { flags: ["--json"], dest: "json", kind: "store_true", help: "With --llm, the command tree as JSON" },
    ],
  },
  {
    name: "completion",
    help: "Print a shell completion script",
    positionals: [{ dest: "shell", choices: ["bash", "zsh", "fish"], help: "Which shell" }],
  },
];

/**
 * `anona help spaces delete`: the spec and prog those words name, or the
 * choices at the first word that names nothing.
 */
export function findCommand(words: string[]): { spec: CommandSpec; prog: string } | { unknown: string[] } {
  let choices: CommandSpec[] | undefined = COMMANDS;
  let spec: CommandSpec | null = null;
  let prog = "anona";
  for (const word of words) {
    const next: CommandSpec | undefined = choices?.find((c) => c.name === word);
    if (!next) return { unknown: (choices ?? []).map((c) => c.name) };
    spec = next;
    prog = `${prog} ${word}`;
    choices = next.actions;
  }
  return { spec: spec as CommandSpec, prog };
}

/** The resource verbs (verbs.ts). */
export const RESOURCE_COMMAND_NAMES = new Set(RESOURCE_COMMANDS.map((c) => c.name));

// --- the command tree, as the pip package's introspect.py reads its parser ----

export interface TreeOption {
  flags: string[];
  help: string;
  takes_value: boolean;
  metavar: string | null;
  choices: string[] | null;
  required: boolean;
}

export interface TreePositional {
  name: string;
  help: string;
  many: boolean;
  optional: boolean;
  choices: string[] | null;
}

export interface TreeCommand {
  path: string[];
  help: string;
  options: TreeOption[];
  positionals: TreePositional[];
  children: TreeCommand[];
}

const collapse = (text: string) => text.split(/\s+/).filter(Boolean).join(" ");

function treeOption(o: OptionSpec): TreeOption {
  const takes = o.kind === "value" || o.kind === "append";
  return {
    flags: o.kind === "bool_optional" ? [...o.flags, ...o.flags.map((f) => "--no-" + f.slice(2))] : [...o.flags],
    help: collapse(o.help),
    takes_value: takes,
    metavar: o.metavar ?? (takes ? o.dest.toUpperCase() : null),
    choices: o.choices ? [...o.choices] : null,
    required: Boolean(o.required),
  };
}

function treePositional(p: PositionalSpec): TreePositional {
  return {
    name: p.metavar ?? p.dest.toUpperCase(),
    help: collapse(p.help),
    many: p.nargs === "+" || p.nargs === "*",
    optional: p.nargs === "?" || p.nargs === "*",
    choices: p.choices ? [...p.choices] : null,
  };
}

/** The whole command tree. Global flags appear on the root only. */
export function tree(): TreeCommand {
  const build = (c: CommandSpec, path: string[]): TreeCommand => ({
    path,
    help: collapse(c.help),
    options: (c.options ?? []).map(treeOption),
    positionals: (c.positionals ?? []).map(treePositional),
    children: (c.actions ?? []).map((a) => build(a, [...path, a.name])),
  });
  return {
    path: ["anona"],
    help: collapse(ROOT_DESCRIPTION),
    options: [...COMMON_OPTIONS, VERSION_OPTION].map(treeOption),
    positionals: [],
    children: COMMANDS.map((c) => build(c, [c.name])),
  };
}

export const commandName = (c: TreeCommand) => c.path.join(" ");

export function leaves(c: TreeCommand): TreeCommand[] {
  return c.children.length ? c.children.flatMap(leaves) : [c];
}

export function walk(c: TreeCommand): TreeCommand[] {
  return [c, ...c.children.flatMap(walk)];
}

// --- help ---------------------------------------------------------------------

// argparse shows a given metavar even when the option has choices.
const metavarOf = (o: OptionSpec) => o.metavar ?? (o.choices ? `{${o.choices.join(",")}}` : o.dest.toUpperCase());
const posName = (p: PositionalSpec) => p.metavar ?? p.dest;

function optInvocation(o: OptionSpec): string {
  if (o.kind === "store_true") return o.flags.join(", ");
  if (o.kind === "bool_optional") return `${o.flags[0]}, --no-${(o.flags[0] as string).slice(2)}`;
  return o.flags.map((f) => `${f} ${metavarOf(o)}`).join(", ");
}

function optUsage(o: OptionSpec): string {
  const flag = o.flags[0] as string;
  const body =
    o.kind === "store_true" ? flag : o.kind === "bool_optional" ? `${flag} | --no-${flag.slice(2)}` : `${flag} ${metavarOf(o)}`;
  return o.required ? body : `[${body}]`;
}

/** Greedy wrap; `words` are kept whole (a usage part like `[--space SPACE]` is one). */
function wrap(text: string | string[], width: number): string[] {
  const lines: string[] = [];
  let line = "";
  for (const word of Array.isArray(text) ? text : text.split(/\s+/).filter(Boolean)) {
    if (line && line.length + 1 + word.length > width) {
      lines.push(line);
      line = word;
    } else {
      line = line ? `${line} ${word}` : word;
    }
  }
  if (line) lines.push(line);
  return lines;
}

function helpRow(invocation: string, help: string): string {
  const col = 24;
  const lines = wrap(help, 80 - col);
  if (invocation.length + 2 <= col - 2) {
    const [first = "", ...rest] = lines;
    return [`  ${invocation.padEnd(col - 2)}${first}`.trimEnd(), ...rest.map((l) => " ".repeat(col) + l)].join("\n") + "\n";
  }
  return [`  ${invocation}`, ...lines.map((l) => " ".repeat(col) + l)].join("\n") + "\n";
}

function usageLine(prog: string, spec: CommandSpec | null): string {
  const parts = ["[-h]", ...COMMON_OPTIONS.map(optUsage)];
  if (spec) {
    parts.push(...(spec.options ?? []).map(optUsage));
    for (const p of spec.positionals ?? []) {
      const name = p.choices ? `{${p.choices.join(",")}}` : posName(p);
      parts.push(
        p.nargs === "?" ? `[${name}]` : p.nargs === "+" ? `${name} [${name} ...]` : p.nargs === "*" ? `[${name} ...]` : name,
      );
    }
    if (spec.actions) parts.push(`{${spec.actions.map((a) => a.name).join(",")}} ...`);
  } else {
    parts.push("[--version]", `{${COMMANDS.map((c) => c.name).join(",")}} ...`);
  }
  const head = `usage: ${prog} `;
  return head + wrap(parts, 80 - head.length).join("\n" + " ".repeat(head.length)) + "\n";
}

export function commandHelp(prog: string, spec: CommandSpec): string {
  let out = usageLine(prog, spec);
  if (spec.description) out += "\n" + wrap(spec.description, 79).join("\n") + "\n";
  const positionals = spec.positionals ?? [];
  if (positionals.length || spec.actions) {
    out += "\npositional arguments:\n";
    for (const p of positionals) out += helpRow(p.choices ? `{${p.choices.join(",")}}` : posName(p), p.help);
    if (spec.actions) {
      out += `  {${spec.actions.map((a) => a.name).join(",")}}\n`;
      for (const a of spec.actions) out += helpRow("  " + a.name, a.help);
    }
  }
  out += "\noptions:\n";
  out += helpRow("-h, --help", "show this help message and exit");
  for (const o of [...COMMON_OPTIONS, ...(spec.options ?? [])]) out += helpRow(optInvocation(o), o.help);
  return out;
}

export function topHelp(): string {
  let out = usageLine("anona", null);
  out += "\n" + wrap(ROOT_DESCRIPTION, 79).join("\n") + "\n";
  out += "\npositional arguments:\n";
  out += `  {${COMMANDS.map((c) => c.name).join(",")}}\n`;
  for (const c of COMMANDS) out += helpRow("  " + c.name, c.help);
  out += "\noptions:\n";
  out += helpRow("-h, --help", "show this help message and exit");
  for (const o of [...COMMON_OPTIONS, VERSION_OPTION]) out += helpRow(optInvocation(o), o.help);
  out += "\n" + wrap(ROOT_EPILOG, 79).join("\n") + "\n";
  return out;
}

// --- parsing --------------------------------------------------------------------

export interface Parsed {
  /** [command, action or null], e.g. ["memories", "list"] or ["record", null]. */
  verb: [string, string | null];
  values: Record<string, unknown>;
}

export type ParseOutcome = { ok: true; parsed: Parsed } | { ok: false; exit: number };

interface Out {
  stdout: { write(s: string): unknown };
  stderr: { write(s: string): unknown };
}

class UsageError extends Error {
  constructor(
    readonly prog: string,
    readonly spec: CommandSpec | null,
    message: string,
  ) {
    super(message);
  }
}

class Exit extends Error {
  constructor(readonly code: number) {
    super("exit");
  }
}

// argparse reads `-5` and `-.5` as values, since no option here looks like a
// negative number; anything else starting with `-` (but not `-` itself) is an
// option.
const looksLikeOption = (tok: string) => tok.startsWith("-") && tok !== "-" && !/^-\d+$|^-\d*\.\d+$/.test(tok);

/** argparse's name for an option in its messages: every spelling, joined by `/`. */
const optionName = (o: OptionSpec) =>
  (o.kind === "bool_optional" ? [...o.flags, ...o.flags.map((f) => "--no-" + f.slice(2))] : o.flags).join("/");

interface Lookup {
  spec: OptionSpec;
  negated: boolean;
  explicit?: string;
}

function findOption(options: OptionSpec[], token: string, prog: string, spec: CommandSpec | null): Lookup | null {
  const eq = token.startsWith("--") ? token.indexOf("=") : -1;
  const name = eq >= 0 ? token.slice(0, eq) : token;
  const explicit = eq >= 0 ? token.slice(eq + 1) : undefined;
  const all: Array<[string, OptionSpec, boolean]> = [];
  for (const o of options) {
    for (const f of o.flags) {
      all.push([f, o, false]);
      if (o.kind === "bool_optional") all.push(["--no-" + f.slice(2), o, true]);
    }
  }
  const exact = all.find(([f]) => f === name);
  if (exact) return { spec: exact[1], negated: exact[2], explicit };
  if (!name.startsWith("--")) return null;
  const prefixed = all.filter(([f]) => f.startsWith(name));
  if (prefixed.length === 1) {
    const [, o, neg] = prefixed[0] as [string, OptionSpec, boolean];
    return { spec: o, negated: neg, explicit };
  }
  if (prefixed.length > 1) {
    throw new UsageError(prog, spec, `ambiguous option: ${name} could match ${prefixed.map(([f]) => f).join(", ")}`);
  }
  return null;
}

const HELP_OPTION: OptionSpec = { flags: ["--help", "-h"], dest: "__help", kind: "store_true", help: "" };

/** Parse argv. Help, --version and usage errors are printed here; the outcome then carries the exit code. */
export function parseArgs(argv: string[], io: Out, version: string): ParseOutcome {
  try {
    return { ok: true, parsed: parseOrThrow(argv, io, version) };
  } catch (e) {
    if (e instanceof Exit) return { ok: false, exit: e.code };
    if (e instanceof UsageError) {
      io.stderr.write(redact(usageLine(e.prog, e.spec) + `${e.prog}: error: ${e.message}\n`));
      return { ok: false, exit: 2 };
    }
    throw e;
  }
}

function convert(
  raw: string,
  name: string,
  conv: { type?: (v: string) => unknown; typeName?: string; choices?: readonly string[] },
  prog: string,
  spec: CommandSpec | null,
): unknown {
  let value: unknown = raw;
  if (conv.type) {
    try {
      value = conv.type(raw);
    } catch (e) {
      if (e instanceof ArgTypeError) throw new UsageError(prog, spec, `argument ${name}: ${e.message}`);
      throw new UsageError(prog, spec, `argument ${name}: invalid ${conv.typeName ?? "value"} value: '${raw}'`);
    }
  }
  if (conv.choices && !conv.choices.includes(value as string)) {
    throw new UsageError(
      prog,
      spec,
      `argument ${name}: invalid choice: '${raw}' (choose from ${conv.choices.map((c) => `'${c}'`).join(", ")})`,
    );
  }
  return value;
}

function parseOrThrow(argv: string[], io: Out, version: string): Parsed {
  const values: Record<string, unknown> = {};
  const extras: string[] = [];
  const seenExclusive = new Map<string, OptionSpec>();
  let i = 0;

  const apply = (o: OptionSpec, lk: Lookup, prog: string, spec: CommandSpec | null): void => {
    const name = optionName(o);
    if (o.exclusive) {
      const other = seenExclusive.get(o.exclusive);
      if (other && other !== o) throw new UsageError(prog, spec, `argument ${o.flags[0]}: not allowed with argument ${other.flags[0]}`);
      seenExclusive.set(o.exclusive, o);
    }
    if (o.kind === "store_true" || o.kind === "bool_optional") {
      if (lk.explicit !== undefined) throw new UsageError(prog, spec, `argument ${name}: ignored explicit argument '${lk.explicit}'`);
      values[o.dest] = o.kind === "bool_optional" ? !lk.negated : true;
      return;
    }
    let raw: string;
    if (lk.explicit !== undefined) {
      raw = lk.explicit;
    } else {
      const next = argv[i + 1];
      if (next === undefined || looksLikeOption(next) || next === "--") {
        throw new UsageError(prog, spec, `argument ${name}: expected one argument`);
      }
      raw = next;
      i++;
    }
    const value = convert(raw, name, o, prog, spec);
    if (o.kind === "append") {
      const list = (values[o.dest] as unknown[] | undefined) ?? [];
      list.push(value);
      values[o.dest] = list;
    } else {
      values[o.dest] = value;
    }
  };

  // Top level: global flags until the command.
  const topOptions = [...COMMON_OPTIONS, HELP_OPTION, VERSION_OPTION];
  let command: CommandSpec | undefined;
  for (; i < argv.length; i++) {
    const tok = argv[i] as string;
    if (tok === "--") continue;
    if (looksLikeOption(tok)) {
      const lk = findOption(topOptions, tok, "anona", null);
      if (lk === null) {
        extras.push(tok);
        continue;
      }
      if (lk.spec === HELP_OPTION) {
        io.stdout.write(topHelp());
        throw new Exit(0);
      }
      if (lk.spec === VERSION_OPTION) {
        io.stdout.write(`anona ${version}\n`);
        throw new Exit(0);
      }
      apply(lk.spec, lk, "anona", null);
      continue;
    }
    command = COMMANDS.find((c) => c.name === tok);
    if (command === undefined) {
      throw new UsageError(
        "anona",
        null,
        `argument command: invalid choice: '${tok}' (choose from ${COMMANDS.map((c) => `'${c.name}'`).join(", ")})` +
          // The usual cause is a copy older than the docs that named the
          // command, and the list alone never says so.
          `. If the docs name \`${tok}\`, this copy (anona ${version}) is older than they are: ` +
          "npm install -g @anona-labs/memory",
      );
    }
    i++;
    break;
  }
  if (command === undefined) throw new UsageError("anona", null, "the following arguments are required: command");

  let spec: CommandSpec = command;
  let prog = `anona ${command.name}`;
  let action: string | null = null;
  let positionalIndex = 0;
  let onlyPositionals = false;
  // A `+` positional takes one unbroken run of values; an option ends the run.
  let manyOpen = false;

  for (; i < argv.length; i++) {
    const tok = argv[i] as string;
    if (!onlyPositionals && tok === "--") {
      onlyPositionals = true;
      continue;
    }
    if (!onlyPositionals && looksLikeOption(tok)) {
      // Before the action is chosen, only the group's own options apply.
      const options = [...COMMON_OPTIONS, HELP_OPTION, ...(spec.actions ? [] : (spec.options ?? []))];
      const lk = findOption(options, tok, prog, spec);
      if (manyOpen) {
        manyOpen = false;
        positionalIndex++;
      }
      if (lk === null) {
        extras.push(tok);
        continue;
      }
      if (lk.spec === HELP_OPTION) {
        io.stdout.write(commandHelp(prog, spec));
        throw new Exit(0);
      }
      apply(lk.spec, lk, prog, spec);
      continue;
    }
    if (spec.actions) {
      const chosen = spec.actions.find((a) => a.name === tok);
      if (!chosen) {
        throw new UsageError(
          prog,
          spec,
          `argument ${spec.actionDest ?? "action"}: invalid choice: '${tok}' (choose from ${spec.actions.map((a) => `'${a.name}'`).join(", ")})`,
        );
      }
      // A nested group (`spaces members list`) names its verb with every step.
      action = action === null ? chosen.name : `${action} ${chosen.name}`;
      spec = chosen;
      prog = `${prog} ${chosen.name}`;
      positionalIndex = 0;
      continue;
    }
    const p = (spec.positionals ?? [])[positionalIndex];
    if (p === undefined) {
      extras.push(tok);
      continue;
    }
    const value = convert(tok, p.metavar ?? p.dest, p, prog, spec);
    if (p.nargs === "+" || p.nargs === "*") {
      const list = (values[p.dest] as unknown[] | undefined) ?? [];
      if (list.length && !manyOpen) {
        extras.push(tok);
        continue;
      }
      list.push(value);
      values[p.dest] = list;
      manyOpen = true;
      continue;
    }
    values[p.dest] = value;
    positionalIndex++;
  }

  if (spec.actions) throw new UsageError(prog, spec, `the following arguments are required: ${spec.actionDest ?? "action"}`);
  const missing = [
    ...(spec.positionals ?? [])
      .filter((p) => p.nargs !== "?" && p.nargs !== "*" && values[p.dest] === undefined)
      .map((p) => p.metavar ?? p.dest),
    ...(spec.options ?? []).filter((o) => o.required && values[o.dest] === undefined).map((o) => optionName(o)),
  ];
  if (missing.length) throw new UsageError(prog, spec, `the following arguments are required: ${missing.join(", ")}`);
  if (extras.length) throw new UsageError("anona", null, `unrecognized arguments: ${extras.join(" ")}`);
  for (const p of spec.positionals ?? []) if (p.nargs === "*" && values[p.dest] === undefined) values[p.dest] = [];
  for (const o of spec.options ?? []) {
    if (values[o.dest] !== undefined) continue;
    if (o.kind === "store_true") values[o.dest] = false;
    else if (o.default !== undefined) values[o.dest] = o.default;
  }
  return { verb: [command.name, action], values };
}
