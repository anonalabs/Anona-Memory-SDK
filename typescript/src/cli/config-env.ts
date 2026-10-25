/**
 * `~/.anona/config.env`, read as data. Mirrors anona/cli/config_env.py.
 *
 * The skill installer writes this file for the agent skill and for
 * `anona-mcp`: `NAME='value'` lines, single-quoted, with a quote inside a value
 * written as '\''. It is meant to be sourceable by a shell, which is exactly why
 * it must never be sourced or executed here: a value is whatever the installer
 * was given, and a hand-edited line can hold `$(...)` or backticks. So it is
 * parsed the way the installer's own reader parses it -- the three names below
 * and nothing else, quoted or unquoted, never expanded.
 *
 * Separate from the OAuth credential files (store.ts): this holds an API key,
 * which the REST API accepts, and `anona logout` never touches it.
 */
import { readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { anonaDir } from "./store.js";

export const NAMES = ["ANONA_API_KEY", "ANONA_SPACE_ID", "ANONA_BASE_URL"] as const;

export type ConfigEnv = Partial<Record<(typeof NAMES)[number], string>>;

export function configEnvPath(): string {
  // The active profile's: ~/.anona/config.env by default.
  return join(anonaDir(), "config.env");
}

// The installer's `shq`: the whole value in single quotes, each embedded quote
// as '\''. Older installers wrote it unquoted, which is taken as is.
function unquote(value: string): string {
  if (value.length >= 2 && value.startsWith("'") && value.endsWith("'")) {
    return value.slice(1, -1).split("'\\''").join("'");
  }
  return value;
}

// Python's str.splitlines() boundaries.
const LINE_BREAKS = /\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]/;

/**
 * The recognised names in `text`. The first assignment of a name wins, as in
 * the installer's reader; an empty value counts as absent.
 */
export function parseConfigEnv(text: string): ConfigEnv {
  const found: ConfigEnv = {};
  for (let line of text.split(LINE_BREAKS)) {
    line = line.replace(/\r+$/, "");
    if (line.startsWith("export ")) line = line.slice("export ".length);
    const eq = line.indexOf("=");
    if (eq < 0) continue;
    const name = line.slice(0, eq) as (typeof NAMES)[number];
    if (!(NAMES as readonly string[]).includes(name) || name in found) continue;
    const value = unquote(line.slice(eq + 1));
    if (value) found[name] = value;
  }
  return found;
}

/**
 * The recognised names in config.env, or {} when there is no file.
 *
 * Warns on stderr -- naming the file, never a value -- when the file is
 * readable by anyone but its owner: it holds a key with full data-plane reach.
 */
export function loadConfigEnv(stderr: { write(s: string): unknown }): ConfigEnv {
  const p = configEnvPath();
  let text: string;
  let mode: number;
  try {
    text = readFileSync(p, "utf8");
    mode = statSync(p).mode;
  } catch {
    return {};
  }
  if (mode & 0o077) {
    stderr.write(`anona: warning: ${p} is readable by other users; run \`chmod 600 ${p}\`.\n`);
  }
  return parseConfigEnv(text);
}
