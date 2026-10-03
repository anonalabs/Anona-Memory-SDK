/** The `anona` command: start, login, logout, status, mcp. */
import type { Readable } from "node:stream";
import { runLogin, type LoginOptions } from "./login.js";
import { runProxy } from "./proxy.js";
import { withCredentialLock } from "./tokens.js";
import { clearCredentials, DEFAULT_BASE_URL, isSignedIn, liveTempToken, loadCredentials } from "./store.js";
import { describeDeadline, runStart } from "./temp.js";

export interface IO {
  stdin: Readable;
  stdout: { write(s: string): unknown };
  stderr: { write(s: string): unknown };
}

const USAGE = `usage: anona [--base-url URL] <command>

  start                 Create a temporary profile with no account
  login [--no-browser]  Approve access in a browser and store a credential
  logout [--force]      Delete the stored credential (--force: also abandon an
                        unclaimed temporary profile, which cannot be recovered)
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
  if (c.tempToken && c.tempExpiresAt !== undefined) {
    // The only warning an unclaimed profile gets: it has no email address.
    io.stdout.write(`Temporary profile for ${c.baseUrl}: ${describeDeadline(c.tempExpiresAt)}.\n`);
    if (c.tempExpiresAt > Date.now() / 1000) io.stdout.write("Run `anona login` to claim it before then.\n");
    if (!isSignedIn(c)) return 0;
  } else if (!isSignedIn(c)) {
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
  let force = false;
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
    } else if (a === "--force") {
      force = true;
    } else {
      rest.push(a);
    }
  }
  const command = rest[0];
  if (rest.length !== 1 || !["start", "login", "logout", "status", "mcp"].includes(command as string)) {
    io.stderr.write(USAGE);
    return 2;
  }
  if (force && command !== "logout") {
    io.stderr.write("anona: --force only applies to logout\n");
    return 2;
  }
  if (command === "start") return runStart(baseUrl || defaultBaseUrl(), io);
  if (command === "login") {
    const base = baseUrl || defaultBaseUrl();
    io.stdout.write(`Signing in to ${base}\n`);
    const stored = loadCredentials();
    if (stored && stored.baseUrl.replace(/\/+$/, "") !== base.replace(/\/+$/, "")) {
      io.stdout.write(`Note: the stored credential is for ${stored.baseUrl}; this replaces it.\n`);
      // A profile belongs to the deployment that minted it, so its token cannot
      // be carried onto this one. But the token is never printed, so this file
      // is its only copy and replacing it is the same loss `logout` refuses to
      // do silently. Say so while they can still cancel.
      if (liveTempToken(stored) !== null) {
        io.stdout.write(
          `Note: that includes an unclaimed temporary profile (it ${describeDeadline(stored.tempExpiresAt as number)}), ` +
            `which will be abandoned. Claim it first with \`anona login --base-url ${stored.baseUrl}\`.\n`,
        );
      }
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
    return withCredentialLock(() => {
      const held = loadCredentials();
      if (held !== null && liveTempToken(held) !== null && !force) {
        // The token is never printed, so this file is its only copy: deleting
        // it strands the profile and everything in it until the server deletes
        // it too.
        io.stderr.write(
          `anona logout: this would abandon an unclaimed temporary profile (it ${describeDeadline(held.tempExpiresAt as number)}). ` +
            "Run `anona login` to claim it, or `anona logout --force` to discard it.\n",
        );
        return 1;
      }
      clearCredentials();
      io.stdout.write("Logged out.\n");
      return 0;
    });
  }
  if (command === "status") return status(io);
  // mcp: a credential belongs to the deployment that issued it, so talk to that
  // one unless the user explicitly named another.
  const stored = loadCredentials();
  const base = baseUrl || stored?.baseUrl || defaultBaseUrl();
  return runProxy(base, io.stdin, io.stdout, io.stderr);
}
