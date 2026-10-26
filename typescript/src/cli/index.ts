/**
 * The `anona` command: start, login, logout, status, mcp, doctor, guide,
 * help, completion, and the data commands (record, retrieve, reason and the
 * resource verbs), which data.ts runs. Every command line is parsed by the
 * one grammar in args.ts.
 *
 * `login` holds two credentials, each in its own file (store.ts): the command
 * line's, which the data commands use, and MCP's. The pip package's `anona`
 * reads and writes the same files and has the same commands, so the two CLIs
 * interoperate.
 */
import { existsSync } from "node:fs";
import type { Readable } from "node:stream";
import { commandHelp, findCommand, parseArgs, RESOURCE_COMMAND_NAMES, topHelp } from "./args.js";
import { script as completionScript } from "./completion.js";
import { loadConfigEnv } from "./config-env.js";
import { displayKey, findApiKey, findBaseUrl, findSpace, redact } from "./credential.js";
import { runData } from "./data.js";
import { runDoctor } from "./doctor.js";
import { ORIENTATION, runGuide } from "./guide.js";
import { OPTIONS } from "./memory.js";
import { printable } from "./py.js";
import { runCliLogin, runLogin, runLogins, type LoginOptions } from "./login.js";
import { discover, revoke } from "./oauth.js";
import { runProxy } from "./proxy.js";
import { withCredentialLock } from "./tokens.js";
import {
  clearCredentials,
  cliCredentialsPath,
  credentialsPath,
  BadProfile,
  DEFAULT_BASE_URL,
  isSignedIn,
  liveTempToken,
  loadCredentials,
  profile,
  PROFILE_ENV,
  type Credentials,
} from "./store.js";
import { checkTransport, forApiKey, resolveTarget, SOURCE_NAMES, TargetError } from "./target.js";
import { describeDeadline, runStart } from "./temp.js";

export interface IO {
  stdin: Readable;
  stdout: { write(s: string): unknown };
  stderr: { write(s: string): unknown };
}

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
function cliStatus(io: IO, requested?: string, shadowed = false): number | null {
  const c = loadCredentials(cliCredentialsPath());
  if (c === null || !isSignedIn(c)) return null;
  if (requested !== undefined) {
    const refused = guarded(io, () => (resolveTarget(requested, c.baseUrl), 0));
    if (refused !== 0) return refused as number;
  }
  // Only what the file says: never a token or the client id, and no network
  // call, which would rotate the refresh token just to look at it.
  const use = shadowed
    ? "not used while an API key is set"
    : "used by record, retrieve, reason and the other data commands";
  io.stdout.write(`Command-line login for ${c.baseUrl} (${use}).\n`);
  const remaining = c.expiresAt - Date.now() / 1000;
  if (remaining > 0) {
    io.stdout.write(`Its access token expires in ${Math.floor(remaining / 60)} minutes and is renewed automatically.\n`);
  } else {
    io.stdout.write("Its access token has expired; a refresh is attempted on the next command.\n");
  }
  io.stdout.write("If a command reports an authorization failure, run `anona login --cli`.\n");
  return 0;
}

function status(io: IO, requested?: string, baseUrlFlag?: string, apiKeyFlag?: string): number {
  const config = loadConfigEnv(io.stderr);
  const key = findApiKey(apiKeyFlag, config);
  if (key !== null) return keyStatus(io, key, config, baseUrlFlag);
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

/**
 * The data commands' API key: its source and a prefix only, since this output
 * ends up in pastes and logs. The stored logins are still described -- they
 * are what `anona mcp` uses -- but the key works either way, so their exit
 * codes do not decide this one.
 */
function keyStatus(
  io: IO,
  key: ReturnType<typeof findApiKey> & object,
  config: ReturnType<typeof loadConfigEnv>,
  baseUrlFlag: string | undefined,
): number {
  io.stdout.write(`API key for record/retrieve/reason: ${displayKey(key)} (from ${SOURCE_NAMES[key.source]}).\n`);
  const [url, urlSource] = findBaseUrl(baseUrlFlag, config);
  let base: string;
  try {
    base = forApiKey(url, urlSource, key.source);
  } catch (e) {
    if (!(e instanceof TargetError)) throw e;
    io.stderr.write(`anona: ${printable(redact(e.message))}\n`);
    return 4;
  }
  const safe = (s: string) => printable(redact(s));
  io.stdout.write(`It is sent to ${safe(base)}.\n`);
  const space = findSpace(undefined, config);
  io.stdout.write(space ? `Space: ${safe(space)}.\n` : "Space: none set; pass --space or set ANONA_SPACE_ID.\n");
  cliStatus(io, undefined, true);
  if (loadCredentials() === null) {
    io.stdout.write("No `anona login` credential is stored for MCP (that one is for `anona mcp`).\n");
    return 0;
  }
  // Introduced as such, or its "Temporary profile ..." line reads as the
  // credential this command just said the key had replaced.
  io.stdout.write("Also stored, for `anona mcp` (the data commands use the API key above):\n");
  mcpStatus(io);
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
  // Whether a file was there to remove: "logged out" when nothing was stored
  // reads as though a credential existed and is now gone.
  let removed = false;
  const take = (file?: string) => {
    const held = loadCredentials(file);
    if (held !== null && isSignedIn(held)) toRevoke.push(held);
    if (existsSync(file ?? credentialsPath())) removed = true;
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
  if (!removed) {
    io.stdout.write(
      which.cli && which.mcp
        ? "No login was stored; nothing to remove.\n"
        : `No ${which.cli ? "command-line" : "MCP"} login was stored; nothing to remove.\n`,
    );
    return 0;
  }
  io.stdout.write(
    which.cli && which.mcp ? "Logged out.\n" : which.cli ? "Logged out of the command line.\n" : "Logged out of MCP.\n",
  );
  return 0;
}

/**
 * --profile, --timeout, --retries and --quiet, before anything reads a file.
 * The profile is put in the environment, as the pip package does, so every
 * path in store.ts and config-env.ts -- and so every lock -- moves with it.
 */
function applyGlobals(io: IO, args: Record<string, unknown>): number | null {
  if (typeof args.profile === "string") process.env[PROFILE_ENV] = args.profile;
  try {
    profile();
  } catch (e) {
    if (!(e instanceof BadProfile)) throw e;
    io.stderr.write(`anona: ANONA_PROFILE: ${printable(redact(e.message))}\n`);
    return 2;
  }
  OPTIONS.timeout = (args.timeout as number | undefined) ?? null;
  OPTIONS.maxRetries = (args.retries as number | undefined) ?? null;
  OPTIONS.quiet = Boolean(args.quiet);
  return null;
}

/** `anona help spaces delete`: that command's own --help, on stdout, exit 0. */
function helpTopic(io: IO, topic: string[], args: Record<string, unknown>): number {
  if (args.llm || args.json) {
    io.stderr.write("anona: help COMMAND takes no --llm or --json. Nothing was printed.\n");
    return 2;
  }
  const found = findCommand(topic);
  if ("unknown" in found) {
    const known = found.unknown.length ? found.unknown.join(", ") : "none";
    io.stderr.write(`anona: no command \`${printable(redact(topic.join(" ")))}\`. Commands here: ${known}.\n`);
    return 2;
  }
  io.stdout.write(commandHelp(found.prog, found.spec));
  return 0;
}

export async function main(argv: string[], io: IO, login: Partial<LoginOptions> = {}): Promise<number> {
  // Bare `anona` is how a person or an agent first meets the command: orient
  // them rather than fail with "arguments are required". Only the truly empty
  // line -- `anona --quiet` with no command is still a usage error.
  if (argv.length === 0) {
    io.stdout.write(ORIENTATION);
    return 0;
  }
  const outcome = parseArgs(argv, io, packageVersion());
  if (!outcome.ok) return outcome.exit;
  const { verb, values: args } = outcome.parsed;
  const bad = applyGlobals(io, args);
  if (bad !== null) return bad;
  const [command] = verb;
  const topic = (args.topic as string[] | undefined) ?? [];
  if (command === "help" && topic.length) return helpTopic(io, topic, args);
  if (command === "guide" || (command === "help" && args.llm)) return runGuide(io, Boolean(args.json));
  if (command === "help") {
    if (args.json) {
      io.stderr.write("anona: --json needs --llm. Nothing was printed.\n");
      return 2;
    }
    io.stdout.write(topHelp());
    return 0;
  }
  if (command === "completion") {
    io.stdout.write(completionScript(args.shell as string) + "\n");
    return 0;
  }
  const baseUrl = (args.base_url as string | undefined) || undefined;
  // What the user named, if anything. Only `start` and `login` may follow it to
  // a new deployment; everything else holds a credential and is pinned to the
  // deployment that issued it (resolveTarget).
  const requested = baseUrl || process.env.ANONA_BASE_URL || undefined;
  if (command === "start") {
    const base = baseUrl || defaultBaseUrl();
    return guarded(io, () => (checkTransport(base), runStart(base, io)));
  }
  if (command === "record" || command === "retrieve" || command === "reason" || RESOURCE_COMMAND_NAMES.has(command)) {
    return runData(io, verb, args, baseUrl, requested);
  }
  if (command === "login") {
    const base = baseUrl || defaultBaseUrl();
    const refused = guarded(io, () => (checkTransport(base), 0));
    if (refused !== 0) return refused;
    io.stdout.write(`Signing in to ${base}\n`);
    // Neither flag means both approvals; the parser keeps the two exclusive.
    const wantCli = !args.mcp;
    const wantMcp = !args.cli;
    const same = (a: string, b: string) => a.replace(/\/+$/, "") === b.replace(/\/+$/, "");
    const heldCli = wantCli ? loadCredentials(cliCredentialsPath()) : null;
    if (heldCli && isSignedIn(heldCli) && !same(heldCli.baseUrl, base)) {
      io.stdout.write(`Note: the stored command-line credential is for ${heldCli.baseUrl}; this replaces it.\n`);
    }
    const stored = loadCredentials();
    if (wantMcp && stored && !same(stored.baseUrl, base)) {
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
    if (stored && liveTempToken(stored) !== null && same(stored.baseUrl, base)) {
      io.stdout.write(
        `Signing in will try to claim your temporary profile, which ${describeDeadline(stored.tempExpiresAt as number)}.\n`,
      );
    }
    const opts: LoginOptions = { ...login, openBrowser: !args.no_browser, stdout: io.stdout, stderr: io.stderr };
    if (wantCli && wantMcp) return runLogins(base, opts);
    return wantCli ? runCliLogin(base, opts) : runLogin(base, opts);
  }
  if (command === "logout") return logout(io, { cli: !args.mcp, mcp: !args.cli, force: Boolean(args.force) });
  if (command === "status") return status(io, requested, baseUrl, args.api_key as string | undefined);
  if (command === "doctor") return runDoctor(io, args, baseUrl, requested, packageVersion());
  // mcp: a credential belongs to the deployment that issued it. Naming another
  // is refused on stderr; stdout is the client's JSON-RPC stream.
  const stored = loadCredentials();
  return guarded(io, () => runProxy(resolveTarget(requested, stored?.baseUrl), io.stdin, io.stdout, io.stderr));
}
