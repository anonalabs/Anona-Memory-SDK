/**
 * The OAuth 2.1 native-app flow, minus the browser and the listener.
 *
 * Split out so the parts with no I/O and one HTTP call each can be tested
 * without opening anything.
 */
import { createHash, randomBytes } from "node:crypto";

export const WELL_KNOWN = "/.well-known/oauth-authorization-server";

/** Anything that should stop the login with a message the human can act on. */
export class OAuthError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "OAuthError";
  }
}

/**
 * The authorization server refused this registration.
 *
 * Carries the server's own status and RFC 6749 `error` code so the caller can
 * decide whether the refusal is one it can recover from.
 */
export class RegistrationRejected extends OAuthError {
  constructor(
    message: string,
    readonly status: number,
    readonly error: string | null,
  ) {
    super(message);
    this.name = "RegistrationRejected";
  }
}

/**
 * Server text, made safe to print to a terminal. The body is remote input:
 * collapsing whitespace drops embedded newlines that would forge extra lines of
 * CLI output, and the cap stops a long body burying the rest of the message.
 */
function printable(text: string, limit = 300): string | null {
  const cleaned = text.split(/\s+/).filter(Boolean).join(" ");
  if (!cleaned) return null;
  return cleaned.length <= limit ? cleaned : cleaned.slice(0, limit - 1) + "…";
}

/** The server's own `(error, error_description)`, through either envelope. */
async function serverError(resp: Response, secret?: string): Promise<[string | null, string | null]> {
  let body: unknown;
  try {
    body = await resp.json();
  } catch {
    return [null, null];
  }
  if (body === null || typeof body !== "object" || Array.isArray(body)) return [null, null];
  let o = body as Record<string, unknown>;
  const inner = o.error;
  if (inner !== null && typeof inner === "object" && !Array.isArray(inner)) o = inner as Record<string, unknown>;
  // Redact BEFORE the cap: truncating first can slice the token in half, leaving
  // a prefix with no complete match for the replace to find. Best effort: this
  // matches the exact string, so a server that case-folds or URL-encodes the
  // token before echoing it would slip through.
  const scrub = (t: string) => (secret ? t.split(secret).join("[redacted]") : t);
  const error = typeof o.error === "string" ? scrub(o.error) : null;
  const d = o.error_description || o.message;
  return [error, typeof d === "string" ? printable(scrub(d)) : null];
}

export interface ServerMetadata {
  issuer: string;
  authorization_endpoint: string;
  token_endpoint: string;
  registration_endpoint: string;
  code_challenge_methods_supported?: unknown;
  [k: string]: unknown;
}

function b64url(buf: Buffer): string {
  return buf.toString("base64url");
}

/** The S256 challenge for a verifier. Exported so a frozen vector can pin it. */
export function challengeFor(verifier: string): string {
  return b64url(createHash("sha256").update(verifier).digest());
}

/**
 * A fresh { verifier, challenge } pair, S256.
 *
 * 64 random bytes base64url-encoded lands inside RFC 7636's 43-128 range and
 * leaves no padding to strip later.
 */
export function newPkce(): { verifier: string; challenge: string } {
  const verifier = b64url(randomBytes(64));
  return { verifier, challenge: challengeFor(verifier) };
}

interface Origin {
  scheme: string;
  host: string;
  userinfo: boolean;
}

function origin(u: string): Origin | null {
  try {
    const p = new URL(u);
    return { scheme: p.protocol, host: p.host, userinfo: p.username !== "" || p.password !== "" };
  } catch {
    return null;
  }
}

function same(a: Origin | null, b: Origin | null): boolean {
  return a !== null && b !== null && a.scheme === b.scheme && a.host === b.host && !a.userinfo && !b.userinfo;
}

export async function discover(baseUrl: string): Promise<ServerMetadata> {
  const url = baseUrl.replace(/\/+$/, "") + WELL_KNOWN;
  let resp: Response;
  try {
    resp = await fetch(url, { signal: AbortSignal.timeout(15_000) });
  } catch (err) {
    throw new OAuthError(`Could not reach ${url}: ${(err as Error).message}`);
  }
  if (resp.status !== 200) throw new OAuthError(`${url} returned ${resp.status}, expected 200.`);
  let meta: unknown;
  try {
    meta = await resp.json();
  } catch {
    throw new OAuthError(`${url} returned something that is not JSON.`);
  }
  if (meta === null || typeof meta !== "object" || Array.isArray(meta)) {
    throw new OAuthError(`${url} returned JSON that is not an object.`);
  }
  const m = meta as ServerMetadata;

  // The issuer must be the host we chose to talk to. If a compromised or
  // misconfigured deployment can name a different issuer here, it chooses where
  // the browser goes and who mints the token, and the user sees a plausible
  // consent screen on someone else's domain. Scheme counts as well as host:
  // http://host must not satisfy https://host.
  const want = origin(baseUrl);
  const got = origin(typeof m.issuer === "string" ? m.issuer : "");
  if (!same(want, got)) {
    throw new OAuthError(
      `Authorization server metadata names issuer '${String(m.issuer)}', but we asked ` +
        `'${baseUrl}'. Refusing to continue.`,
    );
  }
  // Every endpoint we will send the browser or a credential to must live on the
  // issuer's origin, so a mismatched document cannot redirect us.
  for (const key of ["authorization_endpoint", "token_endpoint", "registration_endpoint"] as const) {
    const value = m[key];
    if (typeof value !== "string" || !value) {
      throw new OAuthError(`Authorization server metadata is missing ${key}.`);
    }
    if (!same(origin(value), got)) {
      throw new OAuthError(
        `Authorization server metadata ${key} '${value}' is not on the issuer's origin. Refusing to continue.`,
      );
    }
  }
  return m;
}

/**
 * Register this machine as an OAuth client (RFC 7591) and return its id.
 *
 * Registration is open and unauthenticated by design, so there is nothing to
 * present here. A new id per login is fine and is what keeps the loopback port,
 * which changes every run, matching the registered redirect_uri.
 */
export async function registerClient(meta: ServerMetadata, redirectUri: string, tempToken?: string): Promise<string> {
  let resp: Response;
  try {
    resp = await fetch(meta.registration_endpoint, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        client_name: "Anona CLI",
        redirect_uris: [redirectUri],
        grant_types: ["authorization_code", "refresh_token"],
        response_types: ["code"],
        token_endpoint_auth_method: "none",
        // In the request body, never the authorize URL: that URL is printed to
        // the terminal and lands in browser history.
        ...(tempToken ? { temp_token: tempToken } : {}),
      }),
      signal: AbortSignal.timeout(15_000),
    });
  } catch (err) {
    throw new OAuthError(`Could not register with the authorization server: ${(err as Error).message}`);
  }
  if (resp.status !== 200 && resp.status !== 201) {
    const [error, description] = await serverError(resp, tempToken);
    // The server's sentence, not just its number: this is the one call site
    // where the refusal is actionable.
    throw new RegistrationRejected(
      `Client registration failed with ${resp.status}${description ? `: ${description}` : "."}`,
      resp.status,
      error,
    );
  }
  let body: unknown;
  try {
    body = await resp.json();
  } catch {
    throw new OAuthError("Client registration returned something that is not JSON.");
  }
  if (body === null || typeof body !== "object" || Array.isArray(body)) {
    throw new OAuthError("Client registration returned JSON that is not an object.");
  }
  const id = (body as { client_id?: unknown }).client_id;
  if (typeof id !== "string" || !id) throw new OAuthError("Client registration returned no client_id.");
  return id;
}

/**
 * The two grants `anona login` asks for. `mcp` mints `mcp_access`, which only
 * /mcp accepts; exactly `cli` mints `cli_access`, which only the REST data
 * plane accepts, and is granted only to a client whose every redirect URI is
 * loopback -- which is what login registers.
 */
export const MCP_SCOPE = "mcp";
export const CLI_SCOPE = "cli";
export const CLI_ACCESS_TYP = "cli_access";

export function authorizeUrl(
  meta: ServerMetadata,
  clientId: string,
  redirectUri: string,
  challenge: string,
  state: string,
  scope: string = MCP_SCOPE,
): string {
  const methods = meta.code_challenge_methods_supported;
  // A string would turn the membership test into a substring test.
  if (!Array.isArray(methods) || !methods.includes("S256")) {
    // Never fall back to "plain": the verifier would add nothing, and the code
    // could be redeemed by anyone who intercepted it.
    throw new OAuthError("The authorization server does not advertise S256 PKCE. Refusing to continue.");
  }
  const query = new URLSearchParams({
    response_type: "code",
    client_id: clientId,
    redirect_uri: redirectUri,
    code_challenge: challenge,
    code_challenge_method: "S256",
    state,
    scope,
  });
  return `${meta.authorization_endpoint}?${query.toString()}`;
}

/**
 * The server's sentence when it refuses this authorize request's scope, else
 * null.
 *
 * `/oauth/authorize` answers a scope it will not grant with `400
 * invalid_scope` -- to the BROWSER, which shows a JSON error page and never
 * comes back to the loopback listener, so the CLI would sit out its whole wait
 * for nothing. Asking once first, without following the redirect, turns that
 * into an immediate message. Anything else, a network failure included, is
 * left for the browser to show.
 */
export async function scopeRefusal(url: string): Promise<string | null> {
  let resp: Response;
  try {
    resp = await fetch(url, { redirect: "manual", signal: AbortSignal.timeout(15_000) });
  } catch {
    return null;
  }
  if (resp.status !== 400) return null;
  const [error, description] = await serverError(resp);
  if (error !== "invalid_scope") return null;
  return description ?? "the server does not grant that scope.";
}

/**
 * The `typ` claim of a JWT access token, read WITHOUT verifying it. Not a
 * security check (the server verifies every token): the only way to tell which
 * grant a deployment actually minted. One that predates command-line tokens
 * takes `scope=cli` and mints an MCP token anyway.
 */
export function tokenTyp(accessToken: string): string | null {
  const parts = accessToken.split(".");
  if (parts.length !== 3) return null;
  try {
    const claims: unknown = JSON.parse(Buffer.from(parts[1] as string, "base64url").toString("utf8"));
    if (claims === null || typeof claims !== "object" || Array.isArray(claims)) return null;
    const typ = (claims as { typ?: unknown }).typ;
    return typeof typ === "string" ? typ : null;
  } catch {
    return null;
  }
}

/**
 * Revoke a refresh token (RFC 7009). True when the server answered 200. Best
 * effort: callers delete the local file whatever this returns. The endpoint
 * must live on the issuer's origin, like every other one `discover` checks.
 */
export async function revoke(meta: ServerMetadata, refreshToken: string): Promise<boolean> {
  const endpoint = meta.revocation_endpoint;
  if (typeof endpoint !== "string" || !endpoint) return false;
  let ep: URL;
  let issuer: URL;
  try {
    ep = new URL(endpoint);
    issuer = new URL(meta.issuer);
  } catch {
    return false;
  }
  if (ep.origin !== issuer.origin) return false;
  try {
    const resp = await fetch(endpoint, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ token: refreshToken }).toString(),
      signal: AbortSignal.timeout(15_000),
    });
    return resp.status === 200;
  } catch {
    return false;
  }
}
