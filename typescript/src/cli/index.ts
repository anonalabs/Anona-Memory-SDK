/**
 * The `anona` command: start, login, logout, status, mcp.
 *
 * `login` holds two credentials, each in its own file (store.ts): the command
 * line's, which the pip package's data commands use, and MCP's. This package
 * has no data commands of its own; it signs both in, out and reports them, so
 * the two CLIs interoperate on the same files.
 */
import type { Readable } from "node:stream";
import { runCliLogin, runLogin, runLogins, type LoginOptions } from "./login.js";
import { discover, revoke } from "./oauth.js";
import { runProxy } from "./proxy.js";
import { withCredentialLock } from "./tokens.js";
import {
  clearCredentials,
  cliCredentialsPath,
  DEFAULT_BASE_URL,
  isSignedIn,
  liveTempToken,
  loadCredentials,
  type Credentials,
} from "./store.js";
import { checkTransport, resolveTarget, TargetError } from "./target.js";
import { describeDeadline, runStart } from "./temp.js";

export interface IO {
  stdin: Readable;
  stdout: { write(s: string): unknown };
  stderr: { write(s: string): unknown };
}

const USAGE = `usage: anona [--base-url URL] <command>

  start                 Create a temporary profile with no account
  login [--cli|--mcp] [--no-browser]
                        Approve access in a browser: the command line, then
                        MCP, each stored in its own file (--cli or --mcp: only
                        that one)
  logout [--cli|--mcp] [--force]
                        Delete the stored credentials and revoke them (--cli or
                        --mcp: only that one; --force: also abandon an
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

/** The command-line login, described; null when there is none. */
function cliStatus(io: IO, requested?: string): number | null {
  const c = loadCredentials(cliCredentialsPath());
  if (c === null || !isSignedIn(c)) return null;
  if (requested !== undefined) {
    const refused = guarded(io, () => (resolveTarget(requested, c.baseUrl), 0));
    if (refused !== 0) return refused as number;
  }
  // Only what the file says: never a token or the client id, and no network
  // call, which would rotate the refresh token just to look at it.
  io.stdout.write(`Command-line login for ${c.baseUrl} (used by record, retrieve, reason and the other data commands).\n`);
  const remaining = c.expiresAt - Date.now() / 1000;
  if (remaining > 0) {
    io.stdout.write(`Its access token expires in ${Math.floor(remaining / 60)} minutes and is renewed automatically.\n`);
  } else {
    io.stdout.write("Its access token has expired; a refresh is attempted on the next command.\n");
  }
  io.stdout.write("If a command reports an authorization failure, run `anona login --cli`.\n");
  return 0;
}

function status(io: IO, requested?: string): number {
  const cli = cliStatus(io, requested);
  if (cli === null) return mcpStatus(io, requested);
  if (cli !== 0) return cli;
  if (loadCredentials() === null) {
    io.stdout.write("No MCP credential is stored. Run `anona login --mcp` to connect MCP clients.\n");
    return 0;
  }
  // A working command-line login decides the exit code.
  mcpStatus(io, requested);
  return 0;
}

function mcpStatus(io: IO, requested?: string): number {
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

/**
 * Delete the stored credential(s), then revoke each at the server. Local first
 * and unconditionally, so a logout works offline; a refresh token that could
 * not be revoked dies on its own after 30 unused days. Each file is removed
 * under its own lock, so a refresh finishing just after cannot write it back.
 */
async function logout(io: IO, which: { cli: boolean; mcp: boolean; force: boolean }): Promise<number> {
  if (which.mcp) {
    const refused = await withCredentialLock(() => {
      const held = loadCredentials();
      if (held !== null && liveTempToken(held) !== null && !which.force) {
        // The token is never printed, so this file is its only copy: deleting
        // it strands the profile and everything in it until the server deletes
        // it too. Checked first, so a refused logout removes nothing at all.
        io.stderr.write(
          `anona logout: this would abandon an unclaimed temporary profile (it ${describeDeadline(held.tempExpiresAt as number)}). ` +
            "Run `anona login` to claim it, or `anona logout --force` to discard it. " +
            "`anona logout --cli` removes only the command-line credential.\n",
        );
        return true;
      }
      return false;
    });
    if (refused) return 1;
  }
  const toRevoke: Credentials[] = [];
  const take = (file?: string) => {
    const held = loadCredentials(file);
    if (held !== null && isSignedIn(held)) toRevoke.push(held);
    clearCredentials(file);
  };
  if (which.cli) await withCredentialLock(() => take(cliCredentialsPath()), cliCredentialsPath());
  if (which.mcp) await withCredentialLock(() => take());
  let unrevoked = 0;
  for (const c of toRevoke) {
    try {
      // Pinned like any stored credential: only to the deployment that issued it.
      checkTransport(c.baseUrl);
      if (!(await revoke(await discover(c.baseUrl), c.refreshToken))) unrevoked++;
    } catch {
      unrevoked++;
    }
  }
  if (unrevoked > 0) {
    io.stderr.write(
      "anona logout: removed locally, but the server could not be told to revoke it; it expires after " +
        "30 days unused, or revoke it now under Integrations -> MCP -> Connected apps in the dashboard.\n",
    );
  }
  io.stdout.write(
    which.cli && which.mcp ? "Logged out.\n" : which.cli ? "Logged out of the command line.\n" : "Logged out of MCP.\n",
  );
  return 0;
}

export async function main(argv: string[], io: IO, login: Partial<LoginOptions> = {}): Promise<number> {
  // None, not the resolved default: `mcp` must be able to tell "the user said
  // so" from "nobody said anything" to prefer the stored deployment.
  let baseUrl: string | undefined;
  let noBrowser = false;
  let force = false;
  let onlyCli = false;
  let onlyMcp = false;
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
    } else if (a === "--cli") {
      onlyCli = true;
    } else if (a === "--mcp") {
      onlyMcp = true;
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
  if ((onlyCli || onlyMcp) && command !== "login" && command !== "logout") {
    io.stderr.write("anona: --cli and --mcp only apply to login and logout\n");
    return 2;
  }
  if (onlyCli && onlyMcp) {
    io.stderr.write("anona: give --cli or --mcp, not both (neither means both)\n");
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
    const wantCli = !onlyMcp;
    const wantMcp = !onlyCli;
    const heldCli = wantCli ? loadCredentials(cliCredentialsPath()) : null;
    if (heldCli && isSignedIn(heldCli) && heldCli.baseUrl.replace(/\/+$/, "") !== base.replace(/\/+$/, "")) {
      io.stdout.write(`Note: the stored command-line credential is for ${heldCli.baseUrl}; this replaces it.\n`);
    }
    const stored = loadCredentials();
    if (wantMcp && stored && stored.baseUrl.replace(/\/+$/, "") !== base.replace(/\/+$/, "")) {
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
    const opts: LoginOptions = { ...login, openBrowser: !noBrowser, stdout: io.stdout, stderr: io.stderr };
    if (wantCli && wantMcp) return runLogins(base, opts);
    return wantCli ? runCliLogin(base, opts) : runLogin(base, opts);
  }
  if (command === "logout") return logout(io, { cli: !onlyMcp, mcp: !onlyCli, force });
  if (command === "status") return status(io, requested);
  // mcp: a credential belongs to the deployment that issued it. Naming another
  // is refused on stderr; stdout is the client's JSON-RPC stream.
  const stored = loadCredentials();
  return guarded(io, () => runProxy(resolveTarget(requested, stored?.baseUrl), io.stdin, io.stdout, io.stderr));
}
