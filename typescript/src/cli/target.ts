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
