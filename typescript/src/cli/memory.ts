/**
 * `anona record`, `anona retrieve` and `anona reason`: the non-MCP path, plus
 * the credential, transport and error rendering every data command shares.
 * Mirrors anona/cli/memory.py.
 *
 * MCP is the primary surface for an agent. These exist for a script, a quick
 * check from a terminal, somebody who will not enable MCP, and the session
 * before a client restart has loaded the MCP tools.
 *
 * Three credentials work here, highest first. An API key -- from `--api-key`,
 * ANONA_API_KEY or ~/.anona/config.env, resolved in credential.ts and bound to
 * a host in target.ts `forApiKey`. Then the command-line login (`anona login
 * --cli`, ~/.anona/cli-credentials.json), refreshed when due and once more
 * after a 401. Both reach an account, which can hold many spaces with none
 * implied, so both need a space named. Last, the stored temporary-profile
 * token, where `--space` defaults to the profile's one space, "default".
 *
 * The MCP login (~/.anona/credentials.json) is deliberately NOT one of them,
 * and is never a fallback: the data plane refuses it, so that an MCP
 * credential cannot be replayed against the REST API.
 */
import type { Readable } from "node:stream";
import { Anona } from "../client.js";
import { AnonaError } from "../errors.js";
import type { RequestOptions } from "../http.js";
import { readErrorName, readUtf8 } from "./args.js";
import { redact } from "./credential.js";
import * as exits from "./exits.js";
import { given, LINK_REFUSED } from "./fmt.js";
import { isObject, printable, printableBlock, pyJson, pyStr, truthy, g3 } from "./py.js";
import { cliCredentialsPath, isSignedIn, liveTempToken, loadCredentials } from "./store.js";
import { accessToken, CLI_RELOGIN, NotLoggedIn } from "./tokens.js";

export interface DataIO {
  stdin: Readable;
  stdout: { write(s: string): unknown };
  stderr: { write(s: string): unknown };
}

export const AUTH_EXIT = exits.AUTH;
export const USAGE_EXIT = exits.USAGE;
export const NOTHING_EXTRACTED_EXIT = exits.NOTHING_STORED;

/**
 * Set once by main from the global flags, read by every client this module
 * builds: one command per process, so the options are process-wide by nature.
 */
export const OPTIONS: { timeout: number | null; maxRetries: number | null; quiet: boolean; stderr: DataIO["stderr"] | null } = {
  timeout: null,
  maxRetries: null,
  quiet: false,
  stderr: null,
};

/** A progress line on stderr, unless --quiet. Never an error: those always print. */
export function notice(io: { stderr: DataIO["stderr"] }, text: string): void {
  if (!OPTIONS.quiet) io.stderr.write(text + "\n");
}

const KEY_ROUTE =
  "an API key: pass --api-key, set ANONA_API_KEY, or put ANONA_API_KEY in " +
  "~/.anona/config.env (create a key in the dashboard under API keys)";

const NO_CREDENTIAL =
  "anona: no credential. Run `anona login --cli` to sign in, or use " +
  `${KEY_ROUTE};\n` +
  "or run `anona start` for a temporary profile with no account.";

// --- transport ----------------------------------------------------------------
//
// The SDK client does the request and the error envelope; retrying is done
// here, below it, with the pip package's policy, because the CLI has to say
// that it is waiting while it waits. Without that the command sits silent for
// the whole backoff -- two rate-limit waits read as a hung command, and the
// obvious reaction is Ctrl-C, which throws away the attempt about to succeed.

const DEFAULT_RETRIES = 2;
const MAX_WAIT_SECONDS = 60;
/** Per attempt. A loaded `reason` legitimately runs 70-115s server-side. */
const DEFAULT_TIMEOUT_S = 120;
// A 5xx may mean the request executed, so only these are re-sent after one.
const IDEMPOTENT_METHODS = new Set(["GET", "HEAD", "OPTIONS", "PUT", "DELETE"]);

class TimedOut extends Error {
  constructor() {
    super("the request timed out");
    this.name = "TimeoutError";
  }
}

function retryHint(headers: Headers, body: unknown): number | undefined {
  const header = headers.get("retry-after");
  if (header) {
    const seconds = Number(header.trim());
    if (header.trim() === "" || Number.isNaN(seconds)) return undefined;
    return Number.isFinite(seconds) ? Math.max(0, Math.trunc(seconds)) : undefined;
  }
  const error = isObject(body) ? body.error : undefined;
  if (!isObject(error)) return undefined;
  for (const key of ["retry_after", "window_seconds"]) {
    const v = error[key];
    if (typeof v === "number" && Number.isFinite(v)) return Math.max(0, Math.trunc(v));
  }
  return undefined;
}

/** fetch, with the pip package's retry policy, per-attempt timeout and notices. */
export function retryingFetch(stderr: DataIO["stderr"], inner: typeof fetch = globalThis.fetch.bind(globalThis)): typeof fetch {
  return async (input, init) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const maxRetries = OPTIONS.maxRetries ?? DEFAULT_RETRIES;
    const timeoutMs = (OPTIONS.timeout ?? DEFAULT_TIMEOUT_S) * 1000;
    // A multipart body is never re-sent, as in the pip package.
    const multipart = typeof FormData !== "undefined" && init?.body instanceof FormData;
    for (let attempt = 0; ; attempt++) {
      const controller = new AbortController();
      let timedOut = false;
      const timer = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, timeoutMs);
      // Left running past a successful return so it also bounds the body read;
      // unref'd so it never holds the process open.
      timer.unref?.();
      const outer = init?.signal;
      if (outer) {
        if (outer.aborted) controller.abort();
        else outer.addEventListener("abort", () => controller.abort(), { once: true });
      }
      let response: Response;
      try {
        response = await inner(input, { ...init, signal: controller.signal });
      } catch (err) {
        clearTimeout(timer);
        // A transport failure is never retried: the request may have arrived.
        if (timedOut) throw new TimedOut();
        throw err;
      }
      const status = response.status;
      if (multipart || attempt >= maxRetries || !(status === 429 || (status >= 500 && IDEMPOTENT_METHODS.has(method)))) {
        return response;
      }
      let body: unknown;
      try {
        body = JSON.parse(await response.clone().text());
      } catch {
        body = undefined;
      }
      // `credits_exhausted` rides a 429 too, but it is a balance, not a window.
      const error = isObject(body) ? body.error : undefined;
      if (status === 429 && isObject(error) && error.code === "credits_exhausted") return response;
      clearTimeout(timer);
      const hinted = retryHint(response.headers, body);
      const delay =
        hinted !== undefined
          ? Math.min(hinted, MAX_WAIT_SECONDS)
          : Math.min(0.5 * 2 ** attempt + Math.random() * 0.25, MAX_WAIT_SECONDS);
      try {
        const why = status === 429 ? "rate limited" : `server error ${status}`;
        notice({ stderr }, `anona: ${why}, retrying in ${delay.toFixed(0)} s (${attempt + 1}/${maxRetries})`);
      } catch {
        /* a notice must never fail the request it describes */
      }
      await new Promise((r) => setTimeout(r, delay * 1000));
    }
  };
}

/** The SDK client, plus a raw call for the routes where the CLI sends exactly what the pip package sends. */
export class CliClient extends Anona {
  /**
   * What a download needs to talk to the API outside the SDK's JSON path: the
   * credential, the server, and the same retrying transport. Set by makeClient.
   */
  link: { token: string; baseUrl: string; fetch: typeof fetch; rawFetch: typeof fetch; timeoutMs: number } | null = null;

  raw<T = unknown>(options: RequestOptions): Promise<T> {
    return this.http.request<T>(options);
  }
}

/** Swappable in tests; production uses the global fetch. */
export const transport: { fetch?: typeof fetch } = {};

export function makeClient(token: string, baseUrl: string, stderr: DataIO["stderr"]): CliClient {
  const fetchImpl = retryingFetch(stderr, transport.fetch);
  const client = new CliClient({
    apiKey: token,
    baseUrl,
    // Retries happen in retryingFetch, which is also where the per-attempt
    // timeout lives; the client's own timer is only a backstop.
    maxRetries: 0,
    timeoutMs: 2_000_000_000,
    fetch: fetchImpl,
  });
  // rawFetch has no timer of its own: a download's body is bounded by the idle clock in resources.ts.
  const rawFetch = transport.fetch ?? globalThis.fetch.bind(globalThis);
  client.link = { token, baseUrl: baseUrl.replace(/\/+$/, ""), fetch: fetchImpl, rawFetch, timeoutMs: (OPTIONS.timeout ?? DEFAULT_TIMEOUT_S) * 1000 };
  return client;
}

// --- credentials --------------------------------------------------------------

/**
 * What a data command presents, and how to renew it after a 401. `renew` is
 * set only for the command-line login, the one credential here that can be
 * refreshed. It takes the refused token, so the refresh happens only if what
 * is stored is still that token, and returns the new one or the exit code
 * after printing why it could not.
 */
export interface Bearer {
  token: string;
  space: string | undefined;
  renew?: (rejected: string) => Promise<string | number>;
}

function errorName(e: unknown): string {
  return e instanceof Error ? e.name || e.constructor.name : typeof e;
}

/** A refresh that never got an HTTP answer: the network, not the credential. */
function refreshExit(e: unknown): number {
  const name = errorName(e);
  return e instanceof TypeError || name === "TimeoutError" || name === "AbortError" ? exits.UNAVAILABLE : exits.FAILED;
}

async function renewCli(io: DataIO, rejected: string): Promise<string | number> {
  try {
    return await accessToken({ forceRefresh: true, rejected, file: cliCredentialsPath(), relogin: CLI_RELOGIN });
  } catch (e) {
    if (e instanceof NotLoggedIn) {
      io.stderr.write(
        "anona: the server refused the command-line login and it could not be renewed. " +
          "Run `anona login --cli` to sign in again.\n",
      );
      return AUTH_EXIT;
    }
    io.stderr.write(`anona: could not renew the command-line login (${errorName(e)}).\n`);
    return refreshExit(e);
  }
}

/** The command-line login, refreshed when due; null when there is none. */
async function cliBearer(io: DataIO, space: string | undefined): Promise<Bearer | number | null> {
  const c = loadCredentials(cliCredentialsPath());
  if (c === null || !isSignedIn(c)) return null;
  let token: string;
  try {
    token = await accessToken({ file: cliCredentialsPath(), relogin: CLI_RELOGIN });
  } catch (e) {
    if (e instanceof NotLoggedIn) {
      io.stderr.write(`anona: the command-line login could not be renewed. ${e.message}\n`);
      return AUTH_EXIT;
    }
    io.stderr.write(`anona: could not refresh the command-line login (${errorName(e)}).\n`);
    return refreshExit(e);
  }
  return { token, space, renew: (rejected) => renewCli(io, rejected) };
}

/**
 * The bearer for a command without an API key, or the exit code after
 * printing why it cannot proceed: the command-line login first, then the
 * stored temporary profile. The MCP login is never one of them.
 */
export async function tokenAndSpace(io: DataIO, space: string | undefined): Promise<Bearer | number> {
  const cli = await cliBearer(io, space);
  if (cli !== null) return cli;
  const c = loadCredentials();
  if (c === null) {
    io.stderr.write(NO_CREDENTIAL + "\n");
    return AUTH_EXIT;
  }
  if (isSignedIn(c)) {
    // Signed in for MCP only. Refused here rather than sent: the server would
    // answer `401 invalid_token`, and this says what to do instead. No refresh
    // first -- rotating a token only to refuse it is wasted work on the
    // credential MCP clients depend on.
    io.stderr.write(
      "anona: the stored `anona login` credential is for MCP (`anona mcp`) only; the REST API refuses it.\n" +
        `Run \`anona login --cli\` to sign the command line in too, or use ${KEY_ROUTE};\n` +
        "or a temporary profile (`anona start`).\n",
    );
    return AUTH_EXIT;
  }
  const temp = liveTempToken(c);
  if (temp === null) {
    if (c.tempToken) {
      io.stderr.write("anona: The temporary profile has expired. Run `anona start` for a new one, or `anona login`.\n");
    } else {
      io.stderr.write(NO_CREDENTIAL + "\n");
    }
    return AUTH_EXIT;
  }
  // A temp profile's only space, fixed server-side.
  return { token: temp, space: space !== undefined ? space : "default" };
}

async function credential(io: DataIO, space: string | undefined, apiKey: string | undefined): Promise<Bearer | number> {
  if (apiKey !== undefined) {
    // The caller resolved the key, bound its host and required the space.
    if (space === undefined) throw new Error("an API key needs a space");
    return { token: apiKey, space };
  }
  return tokenAndSpace(io, space);
}

// --- one call, and how its failure is told ------------------------------------

export type Invoked<T> = { done: true; value: T } | { done: false; exit: number };

/**
 * Run one SDK call. A 401 on the command-line login renews it once and retries
 * once -- never a loop. A 401 means the request was not processed, so the
 * retry cannot double a write.
 */
export async function invoke<T>(
  io: DataIO,
  baseUrl: string,
  bearer: Bearer,
  fn: (client: CliClient) => Promise<T>,
): Promise<Invoked<T>> {
  try {
    return { done: true, value: await fn(makeClient(bearer.token, baseUrl, io.stderr)) };
  } catch (e) {
    if (!(e instanceof AnonaError)) throw e;
    if (e.statusCode !== 401 || bearer.renew === undefined || isTransport(e)) {
      return { done: false, exit: fail(io, e, baseUrl) };
    }
  }
  const renewed = await bearer.renew(bearer.token);
  if (typeof renewed === "number") return { done: false, exit: renewed };
  bearer.token = renewed;
  try {
    return { done: true, value: await fn(makeClient(renewed, baseUrl, io.stderr)) };
  } catch (e) {
    if (!(e instanceof AnonaError)) throw e;
    const rc = fail(io, e, baseUrl);
    if (e.statusCode === 401) io.stderr.write("anona: run `anona login --cli` to sign the command line in again.\n");
    return { done: false, exit: rc };
  }
}

/** A presigned download link that answered a non-2xx (see LINK_REFUSED). */
const linkRefused = (e: AnonaError) => isTransport(e) && e.code === LINK_REFUSED;

/** No server answered at all: the SDK wraps the fetch failure as its detail. */
export function isTransport(e: AnonaError): boolean {
  return e.detail instanceof Error;
}

function hostOf(url: string): string {
  const m = /^[A-Za-z][A-Za-z0-9+.-]*:\/\/([^/?#]*)/.exec(url);
  return (m && m[1]) || url;
}

function transportReason(e: AnonaError): string {
  const cause = e.detail as Error & { cause?: { code?: string; message?: string } };
  if (cause.name === "TimeoutError" || cause.name === "AbortError") return "the request timed out";
  const inner = cause.cause;
  if (inner?.code === "ECONNREFUSED") return "Connection refused";
  if (inner?.code === "ENOTFOUND") return "Name or service not known";
  return inner?.message || cause.message || String(cause);
}

function requestIdOf(e: AnonaError): string | undefined {
  const error = isObject(e.detail) ? e.detail.error : undefined;
  const v = isObject(error) ? error.request_id : undefined;
  return typeof v === "string" && v.trim() ? v : undefined;
}

function withFieldErrors(message: string, error: Record<string, unknown>): string {
  const errors = error.errors;
  if (!Array.isArray(errors)) return message;
  const parts: string[] = [];
  for (const item of errors.slice(0, 3)) {
    if (!isObject(item)) continue;
    const loc = item.loc;
    const field = Array.isArray(loc) && loc.length > 1 ? loc.slice(1).map(pyStr).join(".") : null;
    const msg = item.msg;
    if (typeof msg === "string") parts.push(field ? `${field}: ${msg}` : msg);
  }
  return parts.length ? `${message} -- ${parts.join("; ")}` : message;
}

function messageOf(e: AnonaError): string {
  const detail = e.detail;
  if (isObject(detail)) {
    const error = detail.error;
    if (isObject(error)) {
      const message = error.message;
      if (typeof message === "string" && message.trim()) return withFieldErrors(message, error);
    }
    const message = detail.message;
    if (typeof message === "string" && message.trim()) return message;
    // FastAPI's own shape, from a route that raised before the envelope
    // handler, or from a proxy in front of it.
    const inner = detail.detail;
    if (typeof inner === "string" && inner.trim()) return inner;
    // Never a language-specific repr: JSON is what the docs show.
    return pyJson(detail, { sortKeys: true });
  }
  if (Array.isArray(detail)) return pyJson(detail);
  return pyStr(detail);
}

/**
 * Report a failed call in the terms of what actually went wrong: no server
 * answered, an API error with its stable `code`, or an answer with no envelope
 * (an edge page, a proxy), where the status is all there is. The exit code is
 * the one exits.ts gives that outcome.
 */
export function fail(io: DataIO, e: AnonaError, baseUrl: string): number {
  if (linkRefused(e)) {
    // The link, not the API, refused: it expired or was never good. A fresh
    // `download` asks the API for a new one.
    io.stderr.write(`anona: download link expired or refused (HTTP ${e.statusCode}); run download again.\n`);
    return exits.UNAVAILABLE;
  }
  if (isTransport(e)) {
    io.stderr.write(`anona: could not reach ${printable(hostOf(baseUrl))}: ${printable(transportReason(e))}\n`);
    return exits.UNAVAILABLE;
  }
  const rc = exits.forStatus(e.statusCode);
  if (truthy(e.code)) {
    // `code` is server-supplied text on its way to a terminal like the rest.
    let line = `anona: ${printable(e.code)}: ${printable(messageOf(e))}`;
    const requestId = e.requestId || requestIdOf(e);
    if (requestId) line += ` (request_id ${printable(requestId)})`;
    io.stderr.write(line + "\n");
    return rc;
  }
  io.stderr.write(`anona: the server answered HTTP ${e.statusCode}: ${printable(messageOf(e))}\n`);
  return rc;
}

// --- input ---------------------------------------------------------------------

/**
 * An input the server is certain to reject is rejected here instead: the round
 * trip is metered, so sending a write that cannot succeed bills for a 422.
 */
function rejectEmpty(io: DataIO, what: string, value: string): number | null {
  if (value.trim()) return null;
  io.stderr.write(
    `anona: the ${what} is empty. Nothing was sent -- the server rejects an empty ${what}, ` +
      "and the request would still be billed.\n",
  );
  return USAGE_EXIT;
}

export async function readAll(stream: Readable): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of stream) chunks.push(typeof chunk === "string" ? Buffer.from(chunk) : (chunk as Buffer));
  return Buffer.concat(chunks).toString("utf8");
}

/**
 * The text `record` stores: the argument, stdin for `-`, or `--file`. Exactly
 * one source; naming two or none is a usage error that sends nothing. An empty
 * result is left to runRecord's own check, so all three share one message.
 */
export async function readContent(io: DataIO, positional: string | undefined, path: string | undefined): Promise<string | number> {
  if (positional !== undefined && path !== undefined) {
    io.stderr.write("anona: give the content or --file, not both. Nothing was sent.\n");
    return USAGE_EXIT;
  }
  if (path !== undefined) {
    try {
      return readUtf8(path);
    } catch (e) {
      // Redacted as well as sanitized: a key pasted into the wrong flag would
      // otherwise be echoed straight back.
      io.stderr.write(`anona: could not read ${printable(redact(path))}: ${readErrorName(e)}. Nothing was sent.\n`);
      return USAGE_EXIT;
    }
  }
  if (positional === undefined) {
    io.stderr.write("anona: nothing to record. Pass the content, `-` to read it from stdin, or --file PATH. Nothing was sent.\n");
    return USAGE_EXIT;
  }
  if (positional === "-") return readAll(io.stdin);
  return positional;
}

/** One JSON document on stdout, and nothing else there; only after success. */
export function emitJson(io: DataIO, doc: unknown): void {
  io.stdout.write(pyJson(doc) + "\n");
}

export { given };

// --- what a record response says happened -------------------------------------
//
// A 201 means the write was accepted, not that anything was stored: extraction
// runs over the content and short, context-free statements often produce
// nothing. The verdict is read off the ids, not off `status`, which has been
// wrong about this before. The server's `message` is believed about the
// wording, and only restated here when absent.

const NOTHING_EXTRACTED_FALLBACK =
  "Nothing was stored in space '{space_id}': no facts could be extracted from " +
  "that content. Short, context-free statements often extract to nothing — " +
  "re-record it with a sentence of context (what the thing is, or why it " +
  "matters), which usually fixes it.";

const QUEUED_STATUSES = ["processing", "pending", "queued"];

export function storedIds(result: unknown): string[] {
  if (!isObject(result)) return [];
  const many = result.memory_ids;
  if (Array.isArray(many)) {
    const ids = many.filter(truthy).map(pyStr);
    if (ids.length) return ids;
  }
  const one = result.memory_id;
  return truthy(one) ? [pyStr(one)] : [];
}

export function isQueued(result: unknown): boolean {
  if (!isObject(result)) return false;
  return truthy(result.job_id) || QUEUED_STATUSES.includes(result.status as string);
}

/** A non-object is treated as stored: inventing a failure the API did not report would be worse. */
export function storedSomething(result: unknown): boolean {
  if (!isObject(result)) return true;
  return storedIds(result).length > 0 || isQueued(result);
}

export function nothingExtractedMessage(result: unknown, spaceId: string): string {
  if (isObject(result)) {
    const message = result.message;
    if (typeof message === "string" && message.trim()) return message.trim();
  }
  return NOTHING_EXTRACTED_FALLBACK.replace("{space_id}", spaceId);
}

// --- record ------------------------------------------------------------------

export interface Scope {
  user_id?: string;
  agent_id?: string;
  session_id?: string;
}

export async function runRecord(
  io: DataIO,
  baseUrl: string,
  content: string,
  space: string | undefined,
  o: Scope & {
    apiKey?: string;
    asJson?: boolean;
    tags?: string[];
    timestamp?: string;
    metadata?: Record<string, unknown>;
    background?: boolean;
    wait?: number;
  },
): Promise<number> {
  const empty = rejectEmpty(io, "memory", content);
  if (empty !== null) return empty;
  const got = await credential(io, space, o.apiKey);
  if (typeof got === "number") return got;
  const bearer = got;
  const sp = bearer.space as string;
  // Exactly the pip package's body: metadata is always sent, `{}` when unset.
  const body: Record<string, unknown> = { space_id: sp, content, metadata: o.metadata ?? {} };
  Object.assign(
    body,
    given({
      tags: o.tags && o.tags.length ? o.tags : undefined,
      user_id: o.user_id,
      agent_id: o.agent_id,
      session_id: o.session_id,
      timestamp: o.timestamp,
    }),
  );
  if (o.background) body.async = true;
  const r = await invoke(io, baseUrl, bearer, (c) =>
    c.raw<unknown>({ method: "POST", path: "/v1/record", body, idempotent: false }),
  );
  if (!r.done) return r.exit;
  const result = r.value;
  if (o.background) {
    return afterQueued(io, baseUrl, bearer, sp, result, jobIds(result), { asJson: Boolean(o.asJson), wait: o.wait });
  }
  if (o.asJson) {
    // The exit code still says what happened: a write that stored nothing
    // exits 3 with its document on stdout and the server's advice on stderr.
    emitJson(io, result);
    if (!storedSomething(result)) {
      io.stderr.write(printable(nothingExtractedMessage(result, sp)) + "\n");
      return NOTHING_EXTRACTED_EXIT;
    }
    return 0;
  }
  if (!storedSomething(result)) {
    io.stderr.write(printable(nothingExtractedMessage(result, sp)) + "\n");
    return NOTHING_EXTRACTED_EXIT;
  }
  const ids = storedIds(result);
  if (!ids.length) {
    // Accepted for background extraction: no outcome yet, and no id to name.
    io.stdout.write(`Queued in space '${printable(sp)}'. Extraction runs in the background.\n`);
    return 0;
  }
  const noun = ids.length === 1 ? "memory" : "memories";
  io.stdout.write(`Stored ${ids.length} ${noun} in space '${printable(sp)}': ${ids.map(printable).join(", ")}\n`);
  return 0;
}

/** Every job id a queued write returned, `job_id` first, without repeats. */
export function jobIds(result: unknown): string[] {
  if (!isObject(result)) return [];
  const ids: string[] = [];
  const one = result.job_id;
  const many = result.job_ids;
  for (const jid of [...(truthy(one) ? [one] : []), ...(Array.isArray(many) ? many : [])]) {
    if (truthy(jid) && !ids.includes(pyStr(jid))) ids.push(pyStr(jid));
  }
  return ids;
}

/**
 * Report a write the server queued; with `wait`, poll it to the end. Without
 * --wait: the job ids, exit 0. With it: each job's final state, and the exit
 * code they add up to -- 3 when every job finished and none stored anything.
 */
export async function afterQueued(
  io: DataIO,
  baseUrl: string,
  bearer: Bearer,
  space: string | undefined,
  result: unknown,
  ids: string[],
  o: { asJson: boolean; wait?: number; nothingStoredIsAnOutcome?: boolean },
): Promise<number> {
  const jobs = await import("./jobs.js");
  const sp = printable(space);
  if (o.wait === undefined || !ids.length) {
    if (o.asJson) emitJson(io, result);
    else if (ids.length) {
      io.stdout.write(`Queued in space '${sp}'. Job: ${ids.map(printable).join(", ")}\n`);
      io.stdout.write(`Check it with \`anona jobs get ${printable(ids[0])} --space ${sp}\`.\n`);
    } else {
      io.stdout.write(`Queued in space '${sp}'.\n`);
    }
    return 0;
  }
  const got = await jobs.wait(io, baseUrl, bearer, space, ids, o.wait);
  if (!got.done) return got.exit;
  const docs = got.value;
  let rc = jobs.outcome(docs);
  if (rc === 0 && o.nothingStoredIsAnOutcome !== false && jobs.storedNothing(docs)) rc = NOTHING_EXTRACTED_EXIT;
  if (o.asJson) emitJson(io, { submitted: result, jobs: docs });
  else docs.forEach((doc, n) => jobs.render(io, doc, ids[n]));
  if (rc === exits.STILL_RUNNING) {
    io.stderr.write(
      "anona: still running when --wait-timeout ran out; " +
        `check later with \`anona jobs get ${printable(ids[0])} --space ${sp}\`.\n`,
    );
  } else if (rc === exits.NOT_FOUND) {
    io.stderr.write(jobs.NOT_FOUND_LINE + "\n");
  } else if (rc === NOTHING_EXTRACTED_EXIT) {
    io.stderr.write(`anona: nothing was stored in space '${sp}': no facts could be extracted.\n`);
  }
  return rc;
}

/** One batch item, as the API takes it: these keys and no others. */
export const BATCH_ITEM_KEYS = ["agent_id", "content", "context", "metadata", "session_id", "tags", "timestamp", "user_id"];
export const BATCH_MAX_ITEMS = 100;

// Python's str.splitlines() boundaries.
const LINE_BREAKS = /\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]/;

/**
 * JSON Lines from `path` (`-` for stdin): an object with `content` and
 * optionally the other BATCH_ITEM_KEYS, or a bare JSON string. Blank lines are
 * skipped. Everything is checked here, so a bad file exits 2 and sends nothing.
 */
export async function readBatch(io: DataIO, path: string): Promise<Record<string, unknown>[] | number> {
  let text: string;
  try {
    text = path === "-" ? await readAll(io.stdin) : readUtf8(path);
  } catch (e) {
    io.stderr.write(`anona: could not read ${printable(redact(path))}: ${readErrorName(e)}. Nothing was sent.\n`);
    return USAGE_EXIT;
  }
  const items: Record<string, unknown>[] = [];
  const lines = text.split(LINE_BREAKS);
  if (lines.length && lines[lines.length - 1] === "") lines.pop();
  for (let n = 0; n < lines.length; n++) {
    const line = lines[n] as string;
    if (!line.trim()) continue;
    let why: string | null = null;
    let item: unknown;
    try {
      item = JSON.parse(line);
    } catch {
      item = null;
      why = "not valid JSON";
    }
    if (typeof item === "string") item = { content: item };
    if (why === null && !isObject(item)) why = "must be a JSON object or a JSON string";
    if (why === null && isObject(item)) {
      const unknown = Object.keys(item)
        .filter((k) => !BATCH_ITEM_KEYS.includes(k))
        .sort();
      const content = item.content;
      if (unknown.length) {
        why = `unknown field(s) ${unknown.map(printable).join(", ")}; allowed: ${BATCH_ITEM_KEYS.join(", ")}`;
      } else if (typeof content !== "string" || !content.trim()) {
        why = "`content` must be a non-empty string";
      }
    }
    if (why !== null) {
      io.stderr.write(`anona: --batch line ${n + 1}: ${why}. Nothing was sent.\n`);
      return USAGE_EXIT;
    }
    items.push(item as Record<string, unknown>);
  }
  if (!items.length) {
    io.stderr.write("anona: --batch holds no items. Nothing was sent.\n");
    return USAGE_EXIT;
  }
  if (items.length > BATCH_MAX_ITEMS) {
    io.stderr.write(
      `anona: --batch holds ${items.length} items; the API takes at most ${BATCH_MAX_ITEMS} per call. Split the file. Nothing was sent.\n`,
    );
    return USAGE_EXIT;
  }
  return items;
}

/** `record --batch`: up to 100 items in one queued call. */
export async function runRecordBatch(
  io: DataIO,
  baseUrl: string,
  items: Record<string, unknown>[],
  space: string | undefined,
  o: Scope & { apiKey?: string; asJson?: boolean; wait?: number },
): Promise<number> {
  const got = await credential(io, space, o.apiKey);
  if (typeof got === "number") return got;
  const bearer = got;
  const sp = bearer.space as string;
  const body = { space_id: sp, items, ...given({ user_id: o.user_id, agent_id: o.agent_id, session_id: o.session_id }) };
  const r = await invoke(io, baseUrl, bearer, (c) =>
    c.raw<unknown>({ method: "POST", path: "/v1/record/batch", body, idempotent: false }),
  );
  if (!r.done) return r.exit;
  return afterQueued(io, baseUrl, bearer, sp, r.value, jobIds(r.value), { asJson: Boolean(o.asJson), wait: o.wait });
}

// --- retrieve ------------------------------------------------------------------

// Three significant figures, not a fixed number of decimals: relevance is
// close to two-valued (~1.1 for a near-verbatim match, ~1e-4 for a correct
// paraphrase), and two decimals would print every paraphrase hit as 0.00.
// A JSON `true` is not a score.
function score(m: Record<string, unknown>): string {
  const s = m.relevance_score;
  return typeof s === "number" ? g3(s) : "-";
}

export async function runRetrieve(
  io: DataIO,
  baseUrl: string,
  query: string,
  space: string | undefined,
  o: Scope & {
    apiKey?: string;
    asJson?: boolean;
    limit?: number;
    minScore?: number;
    tags?: string[];
    tagsMatch?: string;
    asOf?: string;
    occurredAfter?: string;
    occurredBefore?: string;
    tagGroups?: unknown[];
    memberId?: string;
    preferObservations?: boolean;
    block?: boolean;
    maxTokens?: number;
  },
): Promise<number> {
  const empty = rejectEmpty(io, "query", query);
  if (empty !== null) return empty;
  const got = await credential(io, space, o.apiKey);
  if (typeof got === "number") return got;
  const bearer = got;
  const sp = bearer.space as string;
  // The pip package's body: limit and mode always, then the named extras in
  // its order.
  const body: Record<string, unknown> = { space_id: sp, query, limit: o.limit ?? 10, mode: "accurate" };
  Object.assign(
    body,
    given({
      tags: o.tags,
      tags_match: o.tagsMatch,
      tag_groups: o.tagGroups,
      prefer_observations: o.preferObservations,
      min_score: o.minScore,
      member_id: o.memberId,
      user_id: o.user_id,
      agent_id: o.agent_id,
      session_id: o.session_id,
      as_of: o.asOf,
      occurred_after: o.occurredAfter,
      occurred_before: o.occurredBefore,
    }),
  );
  if (o.block) {
    // The same search body, rendered as the prompt-ready block.
    body.format = "block";
    if (o.maxTokens !== undefined) body.context_max_tokens = o.maxTokens;
  }
  const r = await invoke(io, baseUrl, bearer, (c) => c.raw<unknown>({ method: "POST", path: "/v1/retrieve", body }));
  if (!r.done) return r.exit;
  if (o.block) {
    if (o.asJson) {
      emitJson(io, r.value);
      return 0;
    }
    const context = isObject(r.value) ? r.value.context : undefined;
    if (!truthy(context)) {
      io.stdout.write("No memories matched.\n");
      return 0;
    }
    // Plain output is the block itself, multi-line sanitized, so it can be
    // piped straight into a prompt.
    io.stdout.write(printableBlock(context) + "\n");
    return 0;
  }
  const data = isObject(r.value) ? r.value : {};
  const rows = Array.isArray(data.results) ? data.results : [];
  if (o.asJson) {
    // The rows exactly as the API returned them; no match is {"results": []}.
    const doc: Record<string, unknown> = { results: rows };
    if (data.searched !== undefined && data.searched !== null) doc.searched = data.searched;
    emitJson(io, doc);
    return 0;
  }
  if (!rows.length) {
    io.stdout.write("No memories matched.\n");
    return 0;
  }
  rows.forEach((row, n) => {
    const m = isObject(row) ? row : {};
    const id = truthy(m.memory_id) ? m.memory_id : truthy(m.id) ? m.id : "-";
    const short = Array.from(pyStr(id)).slice(0, 8).join("");
    const content = truthy(m.content) ? m.content : truthy(m.text) ? m.text : "";
    io.stdout.write(`${n + 1}. ${score(m)}  ${printable(short)}  ${printable(content)}\n`);
  });
  return 0;
}

// --- reason --------------------------------------------------------------------

export async function runReason(
  io: DataIO,
  baseUrl: string,
  query: string,
  space: string | undefined,
  o: Scope & { apiKey?: string; asJson?: boolean; depth?: string },
): Promise<number> {
  const empty = rejectEmpty(io, "question", query);
  if (empty !== null) return empty;
  const got = await credential(io, space, o.apiKey);
  if (typeof got === "number") return got;
  const bearer = got;
  const sp = bearer.space as string;
  // A synthesis is an agent loop over the space, not a lookup: it runs tens of
  // seconds and sometimes past a minute. stderr, so `anona reason ... >
  // answer.md` still gets only the answer.
  notice(io, `Reasoning over space '${printable(sp)}'. This usually takes under a minute.`);
  // The pip package drops every falsy field from this body.
  const body: Record<string, unknown> = { space_id: sp, query };
  for (const [k, v] of [
    ["user_id", o.user_id],
    ["agent_id", o.agent_id],
    ["session_id", o.session_id],
    ["depth", o.depth],
  ] as const) {
    if (v) body[k] = v;
  }
  const r = await invoke(io, baseUrl, bearer, (c) =>
    c.raw<unknown>({ method: "POST", path: "/v1/reason", body, idempotent: false }),
  );
  if (!r.done) return r.exit;
  const data = isObject(r.value) ? r.value : {};
  // The receipt form: same answer, plus what it was built from.
  const receipt = {
    insights: data.insights ?? null,
    sources: truthy(data.sources) ? data.sources : {},
    rules_applied: truthy(data.rules_applied) ? data.rules_applied : [],
    model: data.model ?? null,
    usage: data.usage ?? null,
  };
  if (o.asJson) {
    emitJson(io, receipt);
    return 0;
  }
  // The gateway types `insights` as Any: a string in practice, but a number,
  // object or list must still print -- objects and lists as JSON.
  let answer: unknown = receipt.insights;
  if (answer !== null && typeof answer !== "string") {
    answer = Array.isArray(answer) || isObject(answer) ? pyJson(answer) : pyStr(answer);
  }
  if (!truthy(answer) || !(answer as string).trim()) {
    // The call succeeded and the space had nothing to build an answer from.
    io.stdout.write("No answer. The space may hold nothing on that topic yet.\n");
    return 0;
  }
  io.stdout.write(printableBlock(answer) + "\n");
  return 0;
}
