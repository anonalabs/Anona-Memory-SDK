/**
 * Which API key the data commands use, and where it came from. Mirrors
 * anona/cli/credential.py.
 *
 * Highest first: `--api-key`, then ANONA_API_KEY in the environment, then
 * ANONA_API_KEY in ~/.anona/config.env. With none of them the commands fall
 * back to the command-line login (`anona login --cli`), then to the stored
 * temporary-profile token (data.ts owns that half).
 *
 * The MCP login is never one of these. It is MCP-scoped and the REST API
 * refuses it by design; MCP and the data commands hold separate credentials on
 * purpose.
 *
 * The source travels with the key because it decides where the key may be
 * sent (target.ts `forApiKey`) and is what `anona status` names. The key itself
 * is never printed: `displayKey` is the only form that reaches output.
 */
import type { ConfigEnv } from "./config-env.js";

export type Source = "flag" | "env" | "config.env";

export interface ApiKey {
  key: string;
  source: Source;
}

const PREFIXES = ["anona_live_", "anona_test_"];

/** The key's prefix and a few characters, never enough to use it. */
export function displayKey(k: ApiKey): string {
  const chars = Array.from(k.key);
  for (const prefix of PREFIXES) {
    if (k.key.startsWith(prefix)) {
      const rest = chars.slice(prefix.length);
      // A short key would give away a large share of itself.
      return prefix + (rest.length >= 16 ? rest.slice(0, 4).join("") : "") + "…";
    }
  }
  return (chars.length >= 24 ? chars.slice(0, 4).join("") : "") + "…";
}

export function findApiKey(flag: string | undefined, config: ConfigEnv): ApiKey | null {
  if (flag) return { key: flag, source: "flag" };
  const env = process.env.ANONA_API_KEY;
  if (env) return { key: env, source: "env" };
  if (config.ANONA_API_KEY) return { key: config.ANONA_API_KEY, source: "config.env" };
  return null;
}

/** [base URL, its source], or [undefined, undefined] when nothing names one. */
export function findBaseUrl(flag: string | undefined, config: ConfigEnv): [string | undefined, Source | undefined] {
  if (flag) return [flag, "flag"];
  const env = process.env.ANONA_BASE_URL;
  if (env) return [env, "env"];
  if (config.ANONA_BASE_URL) return [config.ANONA_BASE_URL, "config.env"];
  return [undefined, undefined];
}

export function findSpace(flag: string | undefined, config: ConfigEnv): string | undefined {
  return flag || process.env.ANONA_SPACE_ID || config.ANONA_SPACE_ID || undefined;
}

// Anything shaped like an Anona credential, wherever it turns up in text we did
// not write -- a usage error echoes the argument it could not parse, so a key
// pasted into the wrong flag would otherwise be printed straight back.
const CREDENTIAL = /\b(anona_(?:live|test|tmp)_)[A-Za-z0-9_-]+/g;

export function redact(text: string): string {
  return text.replace(CREDENTIAL, "$1…");
}
