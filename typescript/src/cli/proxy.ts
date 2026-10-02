/**
 * `anona mcp`: a stdio MCP server that forwards to the remote one.
 *
 * Any MCP client can launch a stdio server, including the ones with no OAuth
 * support, so this is what makes login work everywhere. The proxy holds the
 * tokens and refreshes them; nothing secret ever sits in a client's config file.
 *
 * The remote is Streamable HTTP in stateless JSON mode: every POST returns one
 * complete JSON response and there is no session. So this is a forwarder, not a
 * transport implementation, and the remote's tool list appears here with no
 * schema maintained on this side.
 *
 * Three rules, each pinned by a test:
 *   * stdout is the transport and carries nothing but JSON-RPC. Diagnostics go
 *     to stderr.
 *   * A 401 refreshes once and retries once, per message. Never a loop.
 *   * A failure becomes a JSON-RPC error carrying the request's id, not a stack
 *     trace: a crashed proxy takes the client's whole memory capability with it,
 *     a failed call does not.
 */
import { createInterface } from "node:readline";
import type { Readable } from "node:stream";
import { accessToken, NotLoggedIn } from "./tokens.js";

// `reason` is an agent loop and can legitimately run for a minute and a half.
const TIMEOUT_MS = 120_000;

const PARSE_ERROR = -32700;
const INVALID_REQUEST = -32600;
const SERVER_ERROR = -32000;

type Json = unknown;

function rpcError(id: Json, code: number, message: string) {
  return { jsonrpc: "2.0", id, error: { code, message } };
}

/** A failure whose message is safe to show the client and the user. */
class Failure extends Error {
  constructor(
    message: string,
    readonly code: number = SERVER_ERROR,
  ) {
    super(message);
  }
}

function post(url: string, token: string, msg: Json): Promise<Response> {
  return fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      Accept: "application/json",
    },
    body: JSON.stringify(msg),
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
}

/** The response line to write, or null when there is nothing to say. */
async function forward(url: string, msg: Json): Promise<string | null> {
  let token = await accessToken();
  let resp = await post(url, token, msg);
  if (resp.status === 401) {
    token = await accessToken({ forceRefresh: true, rejected: token });
    resp = await post(url, token, msg);
    if (resp.status === 401) throw new Failure("The server rejected the stored login. Run `anona login` again.");
  }
  if (resp.status >= 400) throw new Failure(`The Anona server answered HTTP ${resp.status}.`);
  const text = await resp.text();
  // 202 for a notification is normal. For a request it would leave the client
  // waiting forever, so the caller turns it into an error.
  if (!text.trim()) return null;
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    throw new Failure("The Anona server returned something that is not JSON.");
  }
  // Re-serialise: framing is one message per line, and a pretty-printed body
  // would arrive as several malformed ones. Also guarantees JSON on stdout.
  return JSON.stringify(body);
}

function isObject(m: Json): m is Record<string, unknown> {
  return m !== null && typeof m === "object" && !Array.isArray(m);
}

function isRequest(m: Json): boolean {
  return !(isObject(m) && !("id" in m));
}

async function handle(url: string, line: string, stderr: { write(s: string): unknown }): Promise<string | null> {
  let msg: Json;
  try {
    msg = JSON.parse(line);
  } catch {
    return JSON.stringify(rpcError(null, PARSE_ERROR, "Parse error: not valid JSON."));
  }
  // JSON-RPC batches are forwarded: the gateway answers them, so rejecting one
  // here would block traffic the server accepts. Anything else that is not an
  // object is not a message at all.
  if (!isObject(msg) && !Array.isArray(msg)) {
    return JSON.stringify(rpcError(null, INVALID_REQUEST, "Invalid request: expected a JSON object."));
  }
  const batch = Array.isArray(msg);
  const items: Json[] = Array.isArray(msg) ? msg : [msg];
  // Ids that are owed a reply. A message with no "id" is a notification.
  const owed = items.filter(isRequest).map((m) => (isObject(m) ? m.id : null));
  try {
    const out = await forward(url, msg);
    if (out === null && owed.length > 0) throw new Failure("The Anona server sent no response to a request.");
    return out;
  } catch (e) {
    const known = e instanceof NotLoggedIn || e instanceof Failure;
    const text = known ? (e as Error).message : `Request failed: ${(e as Error).name}: ${(e as Error).message}`;
    stderr.write(`anona mcp: ${text}\n`);
    // An error with a null id would be an unsolicited response.
    if (owed.length === 0) return null;
    const code = e instanceof Failure ? e.code : SERVER_ERROR;
    const errs = owed.map((id) => rpcError(id, code, text));
    return JSON.stringify(batch ? errs : errs[0]);
  }
}

export async function runProxy(
  baseUrl: string,
  stdin: Readable,
  stdout: { write(s: string): unknown },
  stderr: { write(s: string): unknown },
): Promise<number> {
  const url = baseUrl.replace(/\/+$/, "") + "/mcp";
  const rl = createInterface({ input: stdin, crlfDelay: Infinity });
  // `for await` serialises: one message at a time, replies in order.
  for await (const raw of rl) {
    const line = raw.trim();
    if (!line) continue;
    const out = await handle(url, line, stderr);
    if (out !== null) stdout.write(out + "\n");
  }
  return 0;
}
