/**
 * Where the OAuth credential lives on disk.
 *
 * Separate from `~/.anona/config.env`, which holds an API key for the skill and
 * for `anona-mcp`. The two are different credentials with different reach: a key
 * works against REST, this does not. Keeping them in separate files means
 * `anona logout` cannot silently destroy a key the user minted by hand.
 *
 * The file is shared with the Python CLI on purpose: same path, same keys, same
 * mode. Log in with one, run the other, and you are already signed in.
 */
import {
  chmodSync,
  closeSync,
  fchmodSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeSync,
} from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { randomBytes } from "node:crypto";

export const DEFAULT_BASE_URL = "https://memory.anonalabs.com";

export interface Credentials {
  accessToken: string;
  refreshToken: string;
  /** Unix seconds, fractional allowed: the Python side writes a float. */
  expiresAt: number;
  clientId: string;
  baseUrl: string;
  /**
   * An unclaimed profile made by `anona start`. A temp-only file has empty
   * OAuth fields (see `isSignedIn`). Never printed, never in a repr: the file
   * is its only copy.
   */
  tempToken?: string;
  /** Unix seconds. The deadline after which the server deletes the profile. */
  tempExpiresAt?: number;
}

/** True when an OAuth access token is stored. A temp-only file has an empty one. */
export function isSignedIn(c: Credentials): boolean {
  return c.accessToken !== "";
}

/**
 * The unclaimed-profile token, unless it is absent, undated or past its
 * deadline. An expired profile is already gone server-side, so presenting it
 * can only fail.
 */
export function liveTempToken(c: Credentials, nowSeconds: number = Date.now() / 1000): string | null {
  if (!c.tempToken) return null;
  if (c.tempExpiresAt === undefined || c.tempExpiresAt <= nowSeconds) return null;
  return c.tempToken;
}

export function credentialsPath(): string {
  return join(homedir(), ".anona", "credentials.json");
}

/**
 * The stored credential, or null when there is none that can be read.
 *
 * A corrupt or unreadable file reads as "logged out" rather than throwing. This
 * runs at the start of every `anona mcp` process, and a stack trace there is an
 * MCP client that fails to start with an error naming a JSON parser.
 */
export function loadCredentials(): Credentials | null {
  try {
    const raw = JSON.parse(readFileSync(credentialsPath(), "utf8"));
    if (raw === null || typeof raw !== "object") return null;
    const expiresAt = Number(raw.expires_at);
    if (
      typeof raw.access_token !== "string" ||
      typeof raw.refresh_token !== "string" ||
      typeof raw.client_id !== "string" ||
      raw.expires_at === null ||
      typeof raw.expires_at === "boolean" ||
      !Number.isFinite(expiresAt)
    ) {
      return null;
    }
    const c: Credentials = {
      accessToken: raw.access_token,
      refreshToken: raw.refresh_token,
      expiresAt,
      clientId: raw.client_id,
      baseUrl: typeof raw.base_url === "string" ? raw.base_url : DEFAULT_BASE_URL,
    };
    // A wrong-typed optional field reads as absent; it must not make the whole
    // credential unreadable.
    if (typeof raw.temp_token === "string") c.tempToken = raw.temp_token;
    if (typeof raw.temp_expires_at === "number" && Number.isFinite(raw.temp_expires_at)) {
      c.tempExpiresAt = raw.temp_expires_at;
    }
    return c;
  } catch {
    return null;
  }
}

/**
 * Write 0600, atomically.
 *
 * Atomic because refresh ROTATES: the server burns the old refresh token the
 * moment the new one is issued. A half-written file at that instant leaves the
 * user holding neither, and the only recovery is approving again.
 */
export function saveCredentials(c: Credentials): void {
  const file = credentialsPath();
  const dir = join(file, "..");
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  chmodSync(dir, 0o700);
  const doc: Record<string, unknown> = {
    access_token: c.accessToken,
    refresh_token: c.refreshToken,
    expires_at: c.expiresAt,
    client_id: c.clientId,
    base_url: c.baseUrl,
  };
  // Only when set, so the file an ordinary login writes is byte-for-byte what
  // it always was.
  if (c.tempToken !== undefined) {
    doc.temp_token = c.tempToken;
    doc.temp_expires_at = c.tempExpiresAt ?? null;
  }
  const payload = JSON.stringify(doc, null, 2);
  const tmp = join(dir, `.credentials-${randomBytes(6).toString("hex")}`);
  try {
    // "wx" so a stale temp file is never adopted; fchmod because the mode
    // argument to open is masked by the umask and 0600 must be exact.
    const fd = openSync(tmp, "wx", 0o600);
    try {
      fchmodSync(fd, 0o600);
      writeSync(fd, payload);
    } finally {
      closeSync(fd);
    }
    renameSync(tmp, file);
  } catch (err) {
    // Leave the previous file intact and remove the partial one, or a failed
    // save litters a 0700 directory with .credentials-XXXX.
    try {
      unlinkSync(tmp);
    } catch {
      /* nothing to remove */
    }
    throw err;
  }
}

export function clearCredentials(): void {
  try {
    unlinkSync(credentialsPath());
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code !== "ENOENT") throw err;
  }
}
