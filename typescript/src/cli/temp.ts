/**
 * `anona start`: make a temporary memory profile with no identity.
 *
 * The profile is deleted 72 hours after creation unless claimed with
 * `anona login`. It has no email address attached, so this CLI is the only
 * place its deadline can ever be shown: `start` prints it and `status` repeats it.
 *
 * The space behind it is created on first write, not here. Nothing in this
 * module claims it is ready to query.
 */
import { credentialsPath, isSignedIn, liveTempToken, loadCredentials, saveCredentials } from "./store.js";

export const TEMP_TOKEN_PREFIX = "anona_tmp_";

/** A clean, message-only exit. */
class StartError extends Error {}

const ISO = /^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?(Z|[+-]\d{2}(?::?\d{2})?)?$/i;

function parseDeadline(value: unknown): number {
  if (typeof value !== "string") throw new StartError("The server returned no expiry time, so nothing was saved.");
  const m = ISO.exec(value);
  // Date.parse reads a zone-less date-time as LOCAL time; Python reads it as UTC.
  let iso = value.replace(" ", "T");
  if (m && !m[1] && iso.includes("T")) iso += "Z";
  const ms = m ? Date.parse(iso) : NaN;
  if (Number.isNaN(ms)) throw new StartError("The server returned an expiry time that is not a date.");
  return ms / 1000;
}

const pad = (n: number) => String(n).padStart(2, "0");

/** 'expires 2026-10-06 12:51 UTC (in 71 hours)', or the past-tense form. */
export function describeDeadline(expiresAt: number, nowSeconds: number = Date.now() / 1000): string {
  const d = new Date(expiresAt * 1000);
  const when = `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`;
  const left = expiresAt - nowSeconds;
  if (left <= 0) return `expired ${when}; its memories have been or will shortly be deleted`;
  const total = Math.floor(left / 60);
  const hours = Math.floor(total / 60);
  const span = hours ? `${hours} hours` : `${total % 60} minutes`;
  return `expires ${when} (in ${span}), after which its memories are deleted`;
}

export async function runStart(
  baseUrl: string,
  io: { stdout: { write(s: string): unknown }; stderr: { write(s: string): unknown } },
): Promise<number> {
  const existing = loadCredentials();
  if (existing !== null && isSignedIn(existing)) {
    io.stderr.write(
      "anona start: you are already signed in. Run `anona logout` first if you want a temporary profile instead.\n",
    );
    return 1;
  }
  if (existing !== null && liveTempToken(existing) !== null) {
    // Creation is rate limited per address, and a second profile would orphan
    // the first one's memories.
    io.stdout.write("A temporary profile already exists; not creating another.\n");
    io.stdout.write(`It ${describeDeadline(existing.tempExpiresAt as number)}.\n`);
    io.stdout.write("Run `anona login` before then to keep it.\n");
    return 0;
  }
  let deadline: number;
  try {
    let resp: Response;
    try {
      resp = await fetch(baseUrl.replace(/\/+$/, "") + "/v1/temp/profiles", {
        method: "POST",
        signal: AbortSignal.timeout(30_000),
      });
    } catch (err) {
      throw new StartError(`Could not reach ${baseUrl}: ${(err as Error).name}.`);
    }
    if (resp.status === 429) throw new StartError("Too many temporary profiles from this address today. Try again tomorrow.");
    if (resp.status !== 201) throw new StartError(`The server refused to create a profile (${resp.status}).`);
    let body: unknown;
    try {
      body = await resp.json();
    } catch {
      body = null;
    }
    if (body === null || typeof body !== "object" || Array.isArray(body)) {
      throw new StartError("The server returned something that is not a JSON object.");
    }
    const b = body as { temp_token?: unknown; expires_at?: unknown };
    if (typeof b.temp_token !== "string" || !b.temp_token.startsWith(TEMP_TOKEN_PREFIX)) {
      throw new StartError("The server returned no usable token, so nothing was saved.");
    }
    deadline = parseDeadline(b.expires_at);
    try {
      saveCredentials({
        accessToken: "",
        refreshToken: "",
        expiresAt: 0,
        clientId: "",
        baseUrl,
        tempToken: b.temp_token,
        tempExpiresAt: deadline,
      });
    } catch (err) {
      throw new StartError(
        `could not save the profile to ${credentialsPath()}: ${(err as NodeJS.ErrnoException).code ?? "error"}.`,
      );
    }
  } catch (e) {
    if (e instanceof StartError) {
      io.stderr.write(`anona start: ${e.message}\n`);
      return 1;
    }
    throw e;
  }
  io.stdout.write(`Temporary profile created. It ${describeDeadline(deadline)}.\n`);
  io.stdout.write("Nothing warns you before then: run `anona status` to check, and\n");
  io.stdout.write("`anona login` to claim it and keep what it holds.\n");
  io.stdout.write("Its space is created by the first memory written to it.\n");
  return 0;
}
