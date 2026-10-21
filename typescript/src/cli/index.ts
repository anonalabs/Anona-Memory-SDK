/** The `anona` command: start, login, logout, status, mcp. */
import type { Readable } from "node:stream";
import { runLogin, type LoginOptions } from "./login.js";
import { runProxy } from "./proxy.js";
import { withCredentialLock } from "./tokens.js";
import { clearCredentials, DEFAULT_BASE_URL, isSignedIn, liveTempToken, loadCredentials } from "./store.js";
import { checkTransport, resolveTarget, TargetError } from "./target.js";
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

  --help, -h            Show this message
  --version             Print the installed version

The pip package additionally has record, retrieve and reason.
`;

// Replaced at build time by tsup (see tsup.config.ts). Declared so that the
// unbundled source still type-checks and still runs under tsx/vitest, where
// the define does not apply.
declare const __ANONA_VERSION__: string | undefined;

function packageVersion(): string {
  return typeof __ANONA_VERSION__ === "string" ? __ANONA_VERSION__ : "unknown";
}

function defaultBaseUrl(): string {
  return process.env.ANONA_BASE_URL || DEFAULT_BASE_URL;
}

/** Runs `fn`; a TargetError becomes `anona: <message>` on stderr and exit 1. */
function guarded(io: IO, fn: () => Promise<number> | number): Promise<number> | number {
  try {
    return fn();
  } catch (exc) {
    if (!(exc instanceof TargetError)) throw exc;
    io.stderr.write(`anona: ${exc.message}\n`);
    return 1;
  }
}

function status(io: IO, requested?: string): number {
  const c = loadCredentials();
  if (c === null) {
    io.stdout.write("Not logged in. Run `anona login`.\n");
    return 1;
  }
  // `--base-url` was read and then ignored here, so `anona status --base-url
  // https://other` described the stored deployment while naming another --
  // the one shape of wrong answer a status command must never give. Every
  // other credential-holding command already refuses a mismatch.
  if (requested !== undefined) {
    const refused = guarded(io, () => (resolveTarget(requested, c.baseUrl), 0));
    if (refused !== 0) return refused as number;
  }
  if (c.tempToken && c.tempExpiresAt !== undefined) {
    // The only warning an unclaimed profile gets: it has no email address.
    io.stdout.write(`Temporary profile for ${c.baseUrl}: ${describeDeadline(c.tempExpiresAt)}.\n`);
    if (c.tempExpiresAt > Date.now() / 1000) {
      io.stdout.write("Run `anona login` to claim it before then.\n");
    } else if (!isSignedIn(c)) {
      // Expired and nothing to fall back on: it read as success, which is how
      // a scripted `anona status && ...` walked into a command with no
      // credential behind it.
      return 1;
    }
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
  let helpWanted = false;
  let versionWanted = false;
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
    } else if (a === "--help" || a === "-h") {
      helpWanted = true;
    } else if (a === "--version" || a === "-V") {
      versionWanted = true;
    } else {
      rest.push(a);
    }
  }
  // Asking for help is not a usage error. These went to the "unknown command"
  // branch, so `anona --help` printed usage on stderr and exited 2 -- the npm
  // binary could not print its own help successfully, and `--version` did not
  // exist at all while the docs told people to check they were on 0.17.0+.
  if (helpWanted) {
    io.stdout.write(USAGE);
    return 0;
  }
  if (versionWanted) {
    io.stdout.write(`anona ${packageVersion()}\n`);
    return 0;
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
  // What the user named, if anything. Only `start` and `login` may follow it to
  // a new deployment; `mcp` holds a credential and is pinned to the deployment
  // that issued it (resolveTarget).
  const requested = baseUrl || process.env.ANONA_BASE_URL || undefined;
  if (command === "start") {
    const base = baseUrl || defaultBaseUrl();
    return guarded(io, () => (checkTransport(base), runStart(base, io)));
  }
  if (command === "login") {
    const base = baseUrl || defaultBaseUrl();
    const refused = guarded(io, () => (checkTransport(base), 0));
    if (refused !== 0) return refused;
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
  if (command === "status") return status(io, requested);
  // mcp: a credential belongs to the deployment that issued it. Naming another
  // is refused on stderr; stdout is the client's JSON-RPC stream.
  const stored = loadCredentials();
  return guarded(io, () => runProxy(resolveTarget(requested, stored?.baseUrl), io.stdin, io.stdout, io.stderr));
}
