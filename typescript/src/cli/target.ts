/**
 * Which deployment a command talks to, and whether a credential may travel there.
 *
 * A stored credential belongs to the deployment that issued it. `--base-url`
 * and ANONA_BASE_URL used to re-target it at any host, over plain HTTP, with no
 * warning, so one flag typed by an agent -- or injected into its instructions
 * by a page it read -- sent the token to whoever ran that server. A
 * temp token has full data-plane access and cannot be rotated.
 *
 * So: naming a different deployment than the stored credential's is refused
 * rather than obeyed (`anona login --base-url` is how you switch), and a
 * request carrying or receiving a credential must be https://, except to
 * loopback. The login path already pins the issuer to the host it asked
 * (oauth discover); this is the same rule for everything else. Mirrors
 * anona/cli/target.py.
 */
import { DEFAULT_BASE_URL } from "./store.js";

// Hostnames as WHATWG URL reports them: lowercased, IPv6 in brackets.
const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]"]);

/** Printed as `anona: <message>`; the command exits 1 without a request. */
export class TargetError extends Error {}

function same(a: string, b: string): boolean {
  return a.replace(/\/+$/, "") === b.replace(/\/+$/, "");
}

export function checkTransport(baseUrl: string): void {
  let u: URL | null = null;
  try {
    u = new URL(baseUrl);
  } catch {
    u = null;
  }
  if (u !== null && u.hostname) {
    if (u.protocol === "https:") return;
    if (u.protocol === "http:" && LOOPBACK.has(u.hostname)) return;
  }
  throw new TargetError(
    `refusing to use ${baseUrl}: a credential travels with every request, ` +
      "so the server must be https:// (plain http:// is allowed only for " +
      "localhost and 127.0.0.1).",
  );
}

/**
 * The deployment to talk to, or TargetError. `requested` is what the user
 * named -- the flag, else ANONA_BASE_URL -- or undefined. With a stored
 * credential the answer is always its own deployment; naming another is
 * refused, never followed.
 */
export function resolveTarget(requested: string | undefined, storedBaseUrl: string | undefined): string {
  let base: string;
  if (storedBaseUrl !== undefined) {
    if (requested && !same(requested, storedBaseUrl)) {
      throw new TargetError(
        `the stored credential is for ${storedBaseUrl}, not ${requested}. ` +
          `Run \`anona login --base-url ${requested}\` to switch.`,
      );
    }
    base = storedBaseUrl;
  } else {
    base = requested || DEFAULT_BASE_URL;
  }
  checkTransport(base);
  return base;
}

// --- an API key ---------------------------------------------------------------
//
// A key is not tied to a deployment the way a stored login is -- nothing in it
// says which server minted it -- so the rule above cannot apply. The risk is
// the same one, though: a key sitting in the environment or in config.env is
// picked up by every `anona` invocation, so a `--base-url` that an agent was
// talked into typing would carry it to whoever runs that host. So a key only
// goes to the Anona hosts, unless the server was named in the SAME place the
// key came from: both on the command line, both in the environment, or both in
// config.env. Naming the two together is a decision about where that key goes;
// naming only the host is not.

/** The SDK's own default: an API key is the SDK's credential. */
export const DEFAULT_API_BASE_URL = "https://api.anonalabs.com";
export const ANONA_HOSTS = new Set(["api.anonalabs.com", "memory.anonalabs.com"]);

type Source = "flag" | "env" | "config.env";

/** Where a value came from, in the words a person would type. */
export const SOURCE_NAMES: Record<Source, string> = {
  flag: "--api-key",
  env: "ANONA_API_KEY",
  "config.env": "~/.anona/config.env",
};
const URL_SOURCE_NAMES: Record<Source, string> = {
  flag: "--base-url",
  env: "ANONA_BASE_URL",
  "config.env": "ANONA_BASE_URL in ~/.anona/config.env",
};

export function isAnonaHost(baseUrl: string): boolean {
  let u: URL;
  try {
    u = new URL(baseUrl);
  } catch {
    return false;
  }
  // WHATWG drops a default port, so "" covers both no port and :443. Any `@`
  // in the authority is userinfo, which an Anona URL never carries.
  return (
    u.protocol === "https:" &&
    ANONA_HOSTS.has(u.hostname) &&
    u.port === "" &&
    !/^[^/]*\/\/[^/?#]*@/.test(baseUrl)
  );
}

/**
 * The deployment an API key may be sent to, or TargetError. `requested` is the
 * named base URL, if any, and `urlSource` where it came from; `keySource` is
 * where the key came from, in the same terms.
 */
export function forApiKey(requested: string | undefined, urlSource: Source | undefined, keySource: Source): string {
  const base = requested || DEFAULT_API_BASE_URL;
  checkTransport(base);
  if (isAnonaHost(base) || urlSource === keySource) return base;
  const fix =
    keySource === "flag"
      ? "A key given with --api-key goes to another server only when that server is given with --base-url on the same command."
      : `A key from ${SOURCE_NAMES[keySource]} goes only to api.anonalabs.com or memory.anonalabs.com, unless ANONA_BASE_URL ` +
        "is set in the same place. To use another server, pass the key with --api-key on the same command line.";
  throw new TargetError(
    `refusing to send the API key from ${SOURCE_NAMES[keySource]} to ${base} ` +
      `(named by ${urlSource ? URL_SOURCE_NAMES[urlSource] : "default"}). ${fix}`,
  );
}
