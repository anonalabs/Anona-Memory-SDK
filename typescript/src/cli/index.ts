/** The `anona` command: login, logout, status, mcp. */
import type { Readable } from "node:stream";
import { runLogin, type LoginOptions } from "./login.js";
import { runProxy } from "./proxy.js";
import { withCredentialLock } from "./tokens.js";
import { clearCredentials, DEFAULT_BASE_URL, loadCredentials } from "./store.js";

export interface IO {
  stdin: Readable;
  stdout: { write(s: string): unknown };
  stderr: { write(s: string): unknown };
}

const USAGE = `usage: anona [--base-url URL] <command>

  login [--no-browser]  Approve access in a browser and store a credential
  logout                Delete the stored credential
  status                Show whether you are signed in
  mcp                   Run the stdio MCP proxy for a client to launch
`;

function defaultBaseUrl(): string {
  return process.env.ANONA_BASE_URL || DEFAULT_BASE_URL;
}

function status(io: IO): number {
  const c = loadCredentials();
  if (c === null) {
    io.stdout.write("Not logged in. Run `anona login`.\n");
    return 1;
  }
  // Never the tokens or the client id: this output ends up in pastes and logs.
  // And only what the file can tell us: it cannot say whether the refresh token
  // is still good, and checking would mean a network call that rotates it.
  io.stdout.write(`Credential found for ${c.baseUrl}.\n`);
  const remaining = c.expiresAt - Date.now() / 1000;
  if (remaining > 0) {
    io.stdout.write(`Stored access token expires in ${Math.floor(remaining / 60)} minutes.\n`);
  } else {
    io.stdout.write("Stored access token has expired; a refresh is attempted on the next MCP call.\n");
  }
  io.stdout.write("The server may still reject it (revoked, or unused for 30 days).\n");
  io.stdout.write("If `anona mcp` reports an authorization failure, run `anona login`.\n");
  return 0;
}

export async function main(argv: string[], io: IO, login: Partial<LoginOptions> = {}): Promise<number> {
  // None, not the resolved default: `mcp` must be able to tell "the user said
  // so" from "nobody said anything" to prefer the stored deployment.
  let baseUrl: string | undefined;
  let noBrowser = false;
  const rest: string[] = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i] as string;
    if (a === "--base-url") {
      baseUrl = argv[++i];
      if (!baseUrl) {
        io.stderr.write("anona: --base-url needs a value\n");
        return 2;
      }
    } else if (a.startsWith("--base-url=")) {
      baseUrl = a.slice("--base-url=".length);
    } else if (a === "--no-browser") {
      noBrowser = true;
    } else {
      rest.push(a);
    }
  }
  const command = rest[0];
  if (rest.length !== 1 || !["login", "logout", "status", "mcp"].includes(command as string)) {
    io.stderr.write(USAGE);
    return 2;
  }
  if (command === "login") {
    const base = baseUrl || defaultBaseUrl();
    io.stdout.write(`Signing in to ${base}\n`);
    const stored = loadCredentials();
    if (stored && stored.baseUrl.replace(/\/+$/, "") !== base.replace(/\/+$/, "")) {
      io.stdout.write(`Note: the stored credential is for ${stored.baseUrl}; this replaces it.\n`);
    }
    return runLogin(base, {
      ...login,
      openBrowser: !noBrowser,
      stdout: io.stdout,
      stderr: io.stderr,
    });
  }
  if (command === "logout") {
    // Under the refresh lock, so a refresh finishing just after cannot write
    // the credential back.
    await withCredentialLock(() => clearCredentials());
    io.stdout.write("Logged out.\n");
    return 0;
  }
  if (command === "status") return status(io);
  // mcp: a credential belongs to the deployment that issued it, so talk to that
  // one unless the user explicitly named another.
  const stored = loadCredentials();
  const base = baseUrl || stored?.baseUrl || defaultBaseUrl();
  return runProxy(base, io.stdin, io.stdout, io.stderr);
}
