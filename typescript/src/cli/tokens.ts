/**
 * A valid access token for `anona mcp`, refreshed and persisted when needed.
 *
 * The access token lives an hour; the refresh token 30 days, renewed on every
 * use, so a credential that is used never expires. That rolling renewal is what
 * lets the product say "approve once". Refresh ROTATES: the server burns the old
 * refresh token the instant it issues the new one and treats a replay as theft.
 * Everything here exists so that rotation never strands the user.
 *
 * LOCKING, shared with the Python CLI. Node has no flock (no portable binding
 * without a native addon, and this package has zero dependencies), so this side
 * uses an exclusive-create lock file, `credentials.json.nodelock`. Python holds
 * `fcntl.flock` on `credentials.json.lock` AND ALSO takes this same `.nodelock`
 * file (anona/cli/tokens.py, `_node_lock`), so a Python and a Node `anona mcp`
 * refreshing in the same instant serialise instead of both rotating. That
 * matters: the server treats a replayed refresh token as theft and revokes every
 * refresh token for the client_id, which both implementations share, so a double
 * rotation would log BOTH out. Python's own `.lock` file stays out of this: it
 * is left in place forever, which would block an exclusive create on its name,
 * which is why the shared file is a separate one that both sides delete.
 * `login` and `logout` take the lock too (`withCredentialLock`), so a refresh
 * finishing just after cannot overwrite a fresh approval or undo a logout.
 */
import { closeSync, mkdirSync, openSync, statSync, unlinkSync, writeSync, chmodSync } from "node:fs";
import { dirname } from "node:path";
import { credentialsPath, loadCredentials, saveCredentials, type Credentials } from "./store.js";
import { discover, OAuthError } from "./oauth.js";

/** Refresh this long before expiry, so a token never lapses mid-request. */
export const SKEW_SECONDS = 60;

/** Tunable so the tests do not wait a minute; production uses the defaults. */
export const lockTuning = {
  /** A lock older than this belongs to a process that died holding it. */
  staleMs: 60_000,
  /** Give up waiting. Longer than a refresh can take (two 15s requests). */
  acquireTimeoutMs: 90_000,
  pollMs: 25,
};

/** No usable credential. The message is what the user should read. */
export class NotLoggedIn extends Error {
  constructor(message: string) {
    super(message);
    this.name = "NotLoggedIn";
  }
}

const RELOGIN = "Run `anona login` again.";

function fresh(c: Credentials): boolean {
  return c.expiresAt - SKEW_SECONDS > Date.now() / 1000;
}

function mustRefresh(c: Credentials, forceRefresh: boolean, rejected: string | undefined): boolean {
  if (!fresh(c)) return true;
  // The server refused a token we still believed in. Refresh only if what is
  // stored is still that token: if it differs, another process rotated while we
  // were waiting, and refreshing again would burn its new refresh token.
  return forceRefresh && (rejected === undefined || c.accessToken === rejected);
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

async function withLock<T>(lockPath: string, fn: () => Promise<T>): Promise<T> {
  const started = Date.now();
  for (;;) {
    try {
      const fd = openSync(lockPath, "wx", 0o600);
      try {
        writeSync(fd, String(process.pid));
      } finally {
        closeSync(fd);
      }
      break;
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== "EEXIST") throw err;
    }
    // Stale recovery: a holder that was killed never releases.
    try {
      if (Date.now() - statSync(lockPath).mtimeMs > lockTuning.staleMs) {
        unlinkSync(lockPath);
        continue;
      }
    } catch {
      continue; // released between our open and our stat: just retry
    }
    if (Date.now() - started > lockTuning.acquireTimeoutMs) {
      throw new OAuthError("Timed out waiting for another anona process to finish refreshing.");
    }
    await sleep(lockTuning.pollMs);
  }
  try {
    return await fn();
  } finally {
    try {
      unlinkSync(lockPath);
    } catch {
      /* already gone */
    }
  }
}

/** Run `fn` holding the credential lock. For writers that are not a refresh. */
export async function withCredentialLock<T>(fn: () => Promise<T> | T): Promise<T> {
  const file = credentialsPath();
  const dir = dirname(file);
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  chmodSync(dir, 0o700);
  return withLock(file + ".nodelock", async () => fn());
}

export async function accessToken(
  opts: { forceRefresh?: boolean; rejected?: string } = {},
): Promise<string> {
  const forceRefresh = opts.forceRefresh ?? false;
  const rejected = opts.rejected;
  let c = loadCredentials();
  if (c === null) throw new NotLoggedIn(`Not logged in. ${RELOGIN}`);
  if (!mustRefresh(c, forceRefresh, rejected)) return c.accessToken;

  const file = credentialsPath();
  const dir = dirname(file);
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  chmodSync(dir, 0o700);
  // Several MCP clients means several `anona mcp` processes, and they all see
  // the same expired token at the same moment. One must rotate; the rest must
  // wait and use its result. Hold the lock across read, refresh and write.
  return withLock(file + ".nodelock", async () => {
    // Re-read: whoever held the lock before us may have refreshed already.
    // Refreshing again would replay a refresh token that is now burned.
    const again = loadCredentials();
    if (again === null) throw new NotLoggedIn(`Not logged in. ${RELOGIN}`);
    if (!mustRefresh(again, forceRefresh, rejected)) return again.accessToken;
    const next = await refresh(again);
    // Persist before returning: the old refresh token is already dead, so a
    // crash after this point must find the new pair on disk.
    saveCredentials(next);
    return next.accessToken;
  });
}

async function refresh(c: Credentials): Promise<Credentials> {
  const meta = await discover(c.baseUrl);
  const resp = await fetch(meta.token_endpoint, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "refresh_token",
      refresh_token: c.refreshToken,
      client_id: c.clientId,
    }).toString(),
    signal: AbortSignal.timeout(15_000),
  });
  if (resp.status === 400 || resp.status === 401) {
    // Rejected: expired after 30 idle days, or revoked. Leave the file alone;
    // deleting it adds nothing and hides the evidence.
    throw new NotLoggedIn(RELOGIN);
  }
  // Anything else (5xx, network) is transient and says nothing about the
  // credential, so it must not read as a logout.
  if (!resp.ok) throw new OAuthError(`The token endpoint answered HTTP ${resp.status}.`);
  let body: unknown;
  try {
    body = await resp.json();
  } catch {
    throw new OAuthError("The token endpoint returned something that is not JSON.");
  }
  if (body === null || typeof body !== "object" || Array.isArray(body)) {
    throw new OAuthError("The token endpoint returned JSON that is not an object.");
  }
  const b = body as { access_token?: unknown; refresh_token?: unknown; expires_in?: unknown };
  // A rotated response without a refresh token must not be saved: it would
  // overwrite the only copy with nothing.
  if (typeof b.access_token !== "string" || !b.access_token) {
    throw new OAuthError("The token endpoint returned no access_token.");
  }
  if (typeof b.refresh_token !== "string" || !b.refresh_token) {
    throw new OAuthError("The token endpoint returned no refresh_token.");
  }
  if (typeof b.expires_in !== "number" || !Number.isFinite(b.expires_in) || b.expires_in <= 0) {
    throw new OAuthError("The token endpoint returned no usable expires_in.");
  }
  return {
    accessToken: b.access_token,
    refreshToken: b.refresh_token,
    expiresAt: Date.now() / 1000 + b.expires_in,
    clientId: c.clientId,
    baseUrl: c.baseUrl,
  };
}
