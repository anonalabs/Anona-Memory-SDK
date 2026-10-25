/**
 * `anona doctor`: what this install would do, and whether it would work.
 * Mirrors anona/cli/doctor.py; the report reports Node where pip's reports
 * Python, and is otherwise the same document.
 *
 * Answers, in order: which runtime and which `anona` are running, which
 * profile and directory, which credential a data command would use and why the
 * others lost, which server it talks to, whether that server's /health
 * answers, whether the space a data command would use is reachable, and how
 * many credits that credential's organization has left. Reads spend credits
 * as well as writes, so a balance of zero is a problem worth naming.
 *
 * Never prints a secret. A key is shown only as its prefix and four
 * characters; a login token is never shown at all. The /health probe carries
 * no credential: a doctor that sent a key to check reachability would send it
 * to whatever host a mistyped --base-url names.
 */
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { AnonaError } from "../errors.js";
import { loadConfigEnv, type ConfigEnv } from "./config-env.js";
import { displayKey, findApiKey, findBaseUrl, findSpace, redact } from "./credential.js";
import * as exits from "./exits.js";
import { emitJson, isTransport, makeClient, OPTIONS, tokenAndSpace, type DataIO } from "./memory.js";
import { printable, pyStr } from "./py.js";
import { anonaDir, cliCredentialsPath, credentialsPath, isSignedIn, liveTempToken, loadCredentials, profile } from "./store.js";
import { checkTransport, forApiKey, resolveTarget, SOURCE_NAMES, TargetError } from "./target.js";

type Info = Record<string, unknown>;

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** Which credential wins, mirroring dataCredential, with no side effects. */
function credentialInfo(apiKeyFlag: string | undefined, baseUrlFlag: string | undefined, requested: string | undefined, config: ConfigEnv): Info {
  const key = findApiKey(apiKeyFlag, config);
  const cli = loadCredentials(cliCredentialsPath());
  const held = loadCredentials();
  const others: string[] = [];
  if (cli !== null && isSignedIn(cli)) others.push("command-line login (`anona login --cli`)");
  if (held !== null && liveTempToken(held) !== null) others.push("temporary profile (`anona start`)");
  if (held !== null && isSignedIn(held)) others.push("MCP login (used only by `anona mcp`)");
  const pinned = (info: Info, resolve: () => string): Info => {
    try {
      info.base_url = resolve();
    } catch (e) {
      if (!(e instanceof TargetError)) throw e;
      info.base_url = null;
      info.error = e.message;
    }
    return info;
  };
  if (key !== null) {
    const [url, urlSource] = findBaseUrl(baseUrlFlag, config);
    const sources: Record<string, string> = { flag: "--base-url", env: "ANONA_BASE_URL", "config.env": "config.env" };
    return pinned(
      {
        kind: "api_key",
        display: displayKey(key),
        source: SOURCE_NAMES[key.source],
        why: "an API key outranks every stored login (--api-key, then ANONA_API_KEY, then config.env)",
        base_url_source: sources[urlSource ?? ""] ?? "default for an API key",
        shadowed: others,
      },
      () => forApiKey(url, urlSource, key.source),
    );
  }
  if (cli !== null && isSignedIn(cli)) {
    return pinned(
      {
        kind: "cli_login",
        display: "command-line login",
        source: cliCredentialsPath(),
        why: "no API key is set, and the command-line login comes next",
        expires_in_s: Math.trunc(cli.expiresAt - Date.now() / 1000),
        base_url_source: "the deployment that issued the login",
        shadowed: others.filter((o) => !o.startsWith("command-line")),
      },
      () => resolveTarget(requested, cli.baseUrl),
    );
  }
  if (held !== null && liveTempToken(held) !== null) {
    return pinned(
      {
        kind: "temp_profile",
        display: "temporary profile",
        source: credentialsPath(),
        why: "no API key and no command-line login; the temporary profile is last",
        base_url_source: "the deployment that created the profile",
        shadowed: others.filter((o) => o.startsWith("MCP")),
      },
      () => resolveTarget(requested, held.baseUrl),
    );
  }
  let why = "nothing usable is stored";
  if (held !== null && isSignedIn(held)) why = "only the MCP login is stored, and the REST API refuses it";
  else if (held !== null && held.tempToken) why = "the temporary profile has expired";
  return {
    kind: null,
    display: "none",
    source: null,
    why,
    base_url: null,
    error: "no credential: run `anona login --cli`, set ANONA_API_KEY, or `anona start`",
    shadowed: others,
  };
}

/** GET /health with no credential. Reachability only. */
async function health(baseUrl: string): Promise<Info> {
  try {
    checkTransport(baseUrl);
  } catch (e) {
    return { ok: false, status: null, error: message(e) };
  }
  const timeoutMs = (OPTIONS.timeout ?? 10) * 1000;
  const started = performance.now();
  let resp: Response;
  try {
    resp = await fetch(baseUrl.replace(/\/+$/, "") + "/health", { signal: AbortSignal.timeout(timeoutMs) });
  } catch (e) {
    const name = e instanceof Error ? e.name : "Error";
    return { ok: false, status: null, error: name === "TimeoutError" ? "TimeoutException" : name };
  }
  const elapsed = Math.round(performance.now() - started) / 1000;
  let bodyOk = false;
  try {
    const body = (await resp.json()) as unknown;
    bodyOk = body !== null && typeof body === "object" && (body as { status?: unknown }).status === "ok";
  } catch {
    /* not JSON */
  }
  return { ok: resp.status === 200 && bodyOk, status: resp.status, seconds: elapsed, error: null };
}

/**
 * Which space a data command would use, whether it answers, and the bearer
 * token the check used (null when there was none to use).
 */
async function spaceInfo(
  io: DataIO,
  cred: Info,
  spaceFlag: string | undefined,
  config: ConfigEnv,
  apiKey: string | undefined,
): Promise<[Info, string | null]> {
  let space: string | undefined;
  let source: string | null;
  if (cred.kind === "temp_profile") {
    [space, source] = spaceFlag ? [spaceFlag, "--space"] : ["default", "a temporary profile's only space"];
  } else {
    space = findSpace(spaceFlag, config);
    source = spaceFlag ? "--space" : space ? "ANONA_SPACE_ID" : null;
  }
  const info: Info = { id: space ?? null, source, accessible: null, error: null };
  if (space === undefined) {
    info.error = "no space named: pass --space or set ANONA_SPACE_ID";
    const usable = cred.base_url !== null && cred.base_url !== undefined && cred.kind !== null;
    return [info, usable && apiKey !== undefined ? apiKey : null];
  }
  if (cred.base_url === null || cred.base_url === undefined || cred.kind === null) return [info, null];
  let token: string;
  if (apiKey !== undefined) {
    token = apiKey;
  } else {
    const got = await tokenAndSpace(io, space);
    if (typeof got === "number") {
      info.error = "the stored credential could not be used (see above)";
      info.code = got;
      return [info, null];
    }
    token = got.token;
  }
  try {
    await makeClient(token, cred.base_url as string, io.stderr).getSpace(space);
    info.accessible = true;
  } catch (e) {
    if (!(e instanceof AnonaError)) throw e;
    const transport = isTransport(e);
    info.accessible = false;
    info.status = transport ? null : e.statusCode;
    info.error = e.code || (transport ? "unreachable" : `HTTP ${e.statusCode}`);
    info.code = exits.forStatus(e.statusCode, transport);
  }
  return [info, token];
}

const isNumber = (x: unknown): x is number => typeof x === "number" && Number.isFinite(x);

/**
 * The credit balance `anona usage` reports, read the same way. Never fails
 * doctor: a usage call that does not answer is an unknown balance.
 */
async function creditsInfo(io: DataIO, baseUrl: string, token: string): Promise<Info> {
  const info: Info = { remaining: null, limit: null, used: null, exhausted: null, error: null };
  let doc: unknown;
  try {
    doc = await makeClient(token, baseUrl, io.stderr).getUsage();
  } catch (e) {
    if (!(e instanceof AnonaError)) throw e;
    info.error = e.code || (isTransport(e) ? "unreachable" : `HTTP ${e.statusCode}`);
    return info;
  }
  const d = (doc !== null && typeof doc === "object" ? doc : {}) as Record<string, unknown>;
  for (const [field, key] of [
    ["remaining", "credits_remaining"],
    ["limit", "credits_limit"],
    ["used", "credits_used"],
  ] as const) {
    if (isNumber(d[key])) info[field] = d[key];
  }
  if (info.remaining === null) {
    info.error = "the usage answer carried no balance";
    return info;
  }
  info.exhausted = (info.remaining as number) <= 0;
  return info;
}

function packagePath(): string {
  try {
    return dirname(fileURLToPath(import.meta.url));
  } catch {
    return "";
  }
}

export async function runDoctor(
  io: DataIO,
  args: Record<string, unknown>,
  baseUrlFlag: string | undefined,
  requested: string | undefined,
  version: string,
): Promise<number> {
  const config = loadConfigEnv(io.stderr);
  const apiKeyFlag = args.api_key as string | undefined;
  const cred = credentialInfo(apiKeyFlag, baseUrlFlag, requested, config);
  const key = cred.kind === "api_key" ? findApiKey(apiKeyFlag, config) : null;
  const report: Info = {
    anona_version: version,
    node: process.versions.node,
    executable: process.execPath,
    package_path: packagePath(),
    profile: profile() ?? "default",
    profile_dir: anonaDir(),
    credential: cred,
  };
  const h = cred.base_url ? await health(cred.base_url as string) : null;
  report.health = h;
  const [space, token] = await spaceInfo(io, cred, args.space as string | undefined, config, key?.key);
  report.space = space;
  // Only against a server that answered /health, with a credential the space
  // check could use: anything else is already a reported problem.
  const credits = token !== null && h !== null && h.ok ? await creditsInfo(io, cred.base_url as string, token) : null;
  report.credits = credits;

  let rc: number = exits.OK;
  if (cred.kind === null || cred.error) rc = exits.AUTH;
  else if (h !== null && !h.ok) rc = exits.UNAVAILABLE;
  else if (space.code) rc = space.code as number;
  else if (space.id === null) rc = exits.USAGE;
  else if (credits !== null && credits.exhausted) rc = exits.RATE_LIMITED;
  report.ok = rc === exits.OK;

  if (args.json) {
    emitJson(io, report);
    return rc;
  }
  const p = printable;
  const out = (line: string) => void io.stdout.write(line + "\n");
  out(`anona ${p(version)} on Node ${p(report.node)} (${p(report.executable)})`);
  out(`Profile: ${p(report.profile)} (${p(report.profile_dir)})`);
  if (cred.kind === null) {
    out(`Credential: none -- ${p(cred.why)}.`);
  } else {
    out(`Credential: ${p(cred.display)} from ${p(cred.source)}.`);
    out(`  Why: ${p(cred.why)}.`);
  }
  for (const other of (cred.shadowed as string[] | undefined) ?? []) out(`  Also present, not used here: ${p(other)}.`);
  if (cred.error) out(`  Problem: ${p(redact(pyStr(cred.error)))}`);
  if (cred.base_url) out(`Server: ${p(cred.base_url)} (${p(cred.base_url_source ?? "")}).`);
  if (h !== null) {
    if (h.ok) out(`Health: ok (HTTP 200 in ${pyStr(h.seconds)} s).`);
    else if (h.status !== null) out(`Health: HTTP ${pyStr(h.status)} -- the server answered but is not healthy.`);
    else out(`Health: unreachable (${p(redact(pyStr(h.error)))}).`);
  }
  if (space.id === null) {
    out(`Space: none -- ${p(space.error)}.`);
  } else {
    let line = `Space: ${p(space.id)} (from ${p(space.source)})`;
    if (space.accessible === true) line += ": accessible.";
    else if (space.accessible === false) line += `: NOT accessible (${p(space.error)}).`;
    else line += ".";
    out(line);
  }
  if (credits !== null) {
    const limit = credits.limit === null ? "-" : p(credits.limit);
    if (credits.remaining === null) out(`Credits: unknown (${p(credits.error)}).`);
    else if (credits.exhausted) {
      out(`Credits: ${p(credits.remaining)} remaining of ${limit}.`);
      out("  Problem: credits exhausted; writes and reads will be refused with exit 6.");
    } else out(`Credits: ${p(credits.remaining)} remaining of ${limit}.`);
  }
  out(rc === exits.OK ? "All good." : `Problems found (exit ${rc}; see \`anona guide\` for exit codes).`);
  return rc;
}
