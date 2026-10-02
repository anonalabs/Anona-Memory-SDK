/**
 * `anona login`: open a browser, catch the redirect on loopback, store a token.
 *
 * RFC 8252 native-app flow. Nothing in here prints or logs a token, an
 * authorization code or the PKCE verifier; the authorization URL is the one
 * thing that must be printed.
 */
import { spawn } from "node:child_process";
import { randomBytes, timingSafeEqual } from "node:crypto";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { authorizeUrl, discover, newPkce, OAuthError, registerClient, type ServerMetadata } from "./oauth.js";
import { credentialsPath, saveCredentials, type Credentials } from "./store.js";
import { withCredentialLock } from "./tokens.js";

export const TIMEOUT_SECONDS = 300;
export const CALLBACK_PATH = "/cb";
/** Per-connection idle limit; see makeServer. */
export const IDLE_SOCKET_MS = 5_000;

const PAGE = (title: string, body: string) =>
  "<!doctype html><meta charset=utf-8><title>Anona</title>" +
  `<body style='font-family:system-ui;max-width:32rem;margin:4rem auto'><h1>${title}</h1><p>${body}</p></body>`;
// Static text only. No query parameter is ever interpolated into a page.
const OK_PAGE = PAGE("You are signed in", "You can close this tab and return to your terminal.");
const DENIED_PAGE = PAGE("Sign-in was not completed", "Return to your terminal for details.");
const STATE_PAGE = PAGE("Request refused", "The state value did not match this login, so it was ignored.");
const BAD_PAGE = PAGE("Request refused", "This request carried no authorization result.");

/** A clean, message-only exit. */
export class Abort extends Error {
  constructor(message: string) {
    super(message);
    this.name = "Abort";
  }
}

export interface Callback {
  code?: string;
  error?: string;
}

export interface LoopbackServer {
  server: Server;
  port: number;
  /** Resolves with the first VERIFIED code or error. */
  result: Promise<Callback>;
  setState(state: string): void;
  close(): void;
}

function stateMatches(got: string, expected: string): boolean {
  const a = Buffer.from(got);
  const b = Buffer.from(expected);
  // timingSafeEqual THROWS on unequal lengths, which would let any local
  // process crash the handler with a short state. The length leak is harmless:
  // the expected length is fixed and public.
  return a.length === b.length && timingSafeEqual(a, b);
}

export async function makeServer(opts: { idleMs?: number } = {}): Promise<LoopbackServer> {
  let expected = "";
  let resolve!: (c: Callback) => void;
  const result = new Promise<Callback>((r) => (resolve = r));

  const reply = (res: ServerResponse, status: number, page: string) => {
    const data = Buffer.from(page);
    res.writeHead(status, {
      "Content-Type": "text/html; charset=utf-8",
      "Content-Length": data.length,
      "Cache-Control": "no-store",
      Connection: "close",
    });
    res.end(data);
  };

  const handler = (req: IncomingMessage, res: ServerResponse) => {
    if (req.method !== "GET") return reply(res, 405, BAD_PAGE);
    const url = new URL(req.url ?? "/", "http://127.0.0.1");
    if (url.pathname !== CALLBACK_PATH) return reply(res, 404, BAD_PAGE);
    const got = url.searchParams.get("state") ?? "";
    // Constant-time, and on the error path too: otherwise any local process can
    // abort the login with a forged denial.
    if (!expected || !stateMatches(got, expected)) return reply(res, 400, STATE_PAGE);
    const error = url.searchParams.get("error");
    const code = url.searchParams.get("code");
    if (error !== null) {
      reply(res, 200, DENIED_PAGE);
      resolve({ error });
    } else if (code) {
      reply(res, 200, OK_PAGE);
      resolve({ code });
    } else {
      reply(res, 400, BAD_PAGE);
    }
  };

  const server = createServer(handler);
  // A peer that connects (browsers preconnect) and sends nothing must not hold
  // a socket open for the life of the login.
  const idle = opts.idleMs ?? IDLE_SOCKET_MS;
  server.on("connection", (s) => {
    s.setTimeout(idle, () => s.destroy());
  });
  server.headersTimeout = idle;
  server.requestTimeout = idle;

  // 127.0.0.1, never 0.0.0.0: the code must not be reachable from the network.
  // Port 0 lets the OS pick, which is what the gateway's loopback rule expects.
  await new Promise<void>((ok, fail) => {
    server.once("error", fail);
    server.listen(0, "127.0.0.1", ok);
  });
  return {
    server,
    port: (server.address() as AddressInfo).port,
    result,
    setState: (s) => void (expected = s),
    close: () => {
      server.close();
      server.closeAllConnections();
    },
  };
}

async function awaitCallback(lb: LoopbackServer, timeoutMs: number): Promise<string> {
  let timer: NodeJS.Timeout | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(
      () => reject(new Abort(`Timed out after ${Math.round(timeoutMs / 1000)}s waiting for the browser. Run \`anona login\` again.`)),
      timeoutMs,
    );
  });
  let cb: Callback;
  try {
    cb = await Promise.race([lb.result, timeout]);
  } finally {
    clearTimeout(timer);
  }
  if (cb.error !== undefined) {
    if (cb.error === "access_denied") throw new Abort("Access was denied in the browser. Nothing was saved.");
    const safe = cb.error.replace(/[^A-Za-z0-9_.-]/g, "?").slice(0, 64);
    throw new Abort(`The authorization server returned an error: ${safe}. Nothing was saved.`);
  }
  return cb.code as string;
}

async function exchange(
  meta: ServerMetadata,
  clientId: string,
  redirectUri: string,
  code: string,
  verifier: string,
  baseUrl: string,
): Promise<Credentials> {
  let resp: Response;
  try {
    resp = await fetch(meta.token_endpoint, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "authorization_code",
        code,
        redirect_uri: redirectUri,
        client_id: clientId,
        code_verifier: verifier,
      }).toString(),
      signal: AbortSignal.timeout(30_000),
    });
  } catch (err) {
    throw new Abort(`Could not reach the token endpoint: ${(err as Error).name}.`);
  }
  if (resp.status !== 200) {
    throw new Abort(`The token endpoint refused the code (${resp.status}). Run \`anona login\` again.`);
  }
  let body: unknown;
  try {
    body = await resp.json();
  } catch {
    body = null;
  }
  if (body === null || typeof body !== "object" || Array.isArray(body)) {
    throw new Abort("The token endpoint returned something that is not a JSON object.");
  }
  const b = body as { access_token?: unknown; refresh_token?: unknown; expires_in?: unknown };
  if (
    typeof b.access_token !== "string" ||
    !b.access_token ||
    typeof b.refresh_token !== "string" ||
    !b.refresh_token ||
    typeof b.expires_in !== "number" ||
    !Number.isFinite(b.expires_in) ||
    b.expires_in <= 0
  ) {
    throw new Abort("The token response was incomplete, so nothing was saved.");
  }
  return {
    accessToken: b.access_token,
    refreshToken: b.refresh_token,
    expiresAt: Date.now() / 1000 + b.expires_in,
    clientId,
    baseUrl,
  };
}

/**
 * The one place a browser is launched, as a replaceable binding. The test suite
 * swaps `browser.open` for a fake in a global setup file, so no test can open a
 * window on the machine running it.
 */
export const browser: { open: (url: string) => void } = { open: openBrowser };

/** Best effort, detached, silent. The URL is printed regardless. */
export function openBrowser(url: string): void {
  let cmd: string;
  let args: string[];
  if (process.platform === "darwin") {
    cmd = "open";
    args = [url];
  } else if (process.platform === "win32") {
    // Not `cmd /c start`: cmd would read the & in our query string as a command
    // separator. rundll32 takes the URL as one argument.
    cmd = "rundll32";
    args = ["url.dll,FileProtocolHandler", url];
  } else {
    cmd = "xdg-open";
    args = [url];
  }
  try {
    const child = spawn(cmd, args, { detached: true, stdio: "ignore" });
    child.on("error", () => {}); // no xdg-open in a container is not an error
    child.unref();
  } catch {
    /* the printed URL is the fallback */
  }
}

export interface LoginOptions {
  /** Default true. */
  openBrowser?: boolean;
  /** Called instead of `browser.open`; for tests. */
  opener?: (url: string) => void;
  timeoutMs?: number;
  stdout?: { write(s: string): unknown };
  stderr?: { write(s: string): unknown };
}

export async function runLogin(baseUrl: string, opts: LoginOptions = {}): Promise<number> {
  const out = opts.stdout ?? process.stdout;
  const err = opts.stderr ?? process.stderr;
  let lb: LoopbackServer | undefined;
  try {
    const meta = await discover(baseUrl);
    lb = await makeServer();
    const redirectUri = `http://127.0.0.1:${lb.port}${CALLBACK_PATH}`;
    const clientId = await registerClient(meta, redirectUri);
    const { verifier, challenge } = newPkce();
    const state = randomBytes(32).toString("base64url");
    lb.setState(state);
    const url = authorizeUrl(meta, clientId, redirectUri, challenge, state);

    // Printed unconditionally: a spawn that fails silently (SSH, containers) is
    // indistinguishable from one that worked.
    out.write("Open this URL in a browser to sign in to Anona:\n");
    out.write(`\n  ${url}\n\n`);
    if (opts.openBrowser !== false) (opts.opener ?? browser.open)(url);
    out.write("Waiting for approval...\n");

    const code = await awaitCallback(lb, opts.timeoutMs ?? TIMEOUT_SECONDS * 1000);
    const creds = await exchange(meta, clientId, redirectUri, code, verifier, baseUrl);
    try {
      // Under the refresh lock: a proxy that finishes rotating just after this
      // must not overwrite the credential the user just approved.
      await withCredentialLock(() => saveCredentials(creds));
    } catch (e) {
      if (e instanceof OAuthError) throw e; // the lock timed out; not a disk fault
      throw new Abort(
        `Approval succeeded, but the credential could not be saved to ${credentialsPath()}: ` +
          `${(e as NodeJS.ErrnoException).code ?? "error"}. Fix that and run \`anona login\` again.`,
      );
    }
  } catch (e) {
    if (e instanceof Abort || e instanceof OAuthError) {
      err.write(`anona login: ${e.message}\n`);
      return 1;
    }
    throw e;
  } finally {
    lb?.close();
  }
  out.write("Signed in. MCP clients can now connect without an API key.\n");
  return 0;
}
