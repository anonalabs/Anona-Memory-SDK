/**
 * `anona login`: one approval for the command line and `anona mcp`.
 *
 * One browser approval, scope `cli`, stored in `cli-credentials.json`
 * (`typ=cli_access`) with the same JSON shape the Python CLI writes. The
 * gateway accepts that token on `/mcp` too, so `anona mcp` presents it. `anona
 * login --mcp` still mints a separate MCP-only token in `credentials.json`,
 * which `anona mcp` uses only when there is no command-line login for its
 * deployment. A deployment that will not grant `cli` gets the MCP approval.
 */
import { existsSync, readFileSync, statSync } from "node:fs";
import { PassThrough } from "node:stream";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { main } from "../../src/cli/index.js";
import { tokenTyp } from "../../src/cli/oauth.js";
import { runProxy } from "../../src/cli/proxy.js";
import { cliCredentialsPath, credentialsPath, saveCredentials } from "../../src/cli/store.js";
import { json, startAs, useTempHome, type FakeAs } from "./helpers.js";

function io() {
  const o: string[] = [], e: string[] = [];
  const stdin = new PassThrough();
  stdin.end("");
  return { stdin, stdout: { write: (s: string) => void o.push(s) }, stderr: { write: (s: string) => void e.push(s) }, o: () => o.join(""), e: () => e.join("") };
}

const b64 = (o: unknown) => Buffer.from(JSON.stringify(o)).toString("base64url");
const jwt = (typ: string, n: number) => `${b64({ alg: "HS256" })}.${b64({ typ, n })}.sig`;
const doc = (p: string) => JSON.parse(readFileSync(p, "utf8"));

describe("anona login: one approval, and what `anona mcp` presents", () => {
  useTempHome();
  let as: FakeAs;
  let mode: { cliScope: "grant" | "refuse" | "old" };
  let opened: string[];
  let registered: Array<Record<string, unknown>>;
  let revoked: string[];
  const pending: Promise<void>[] = [];
  let n = 0;

  beforeEach(async () => {
    mode = { cliScope: "grant" };
    opened = [];
    registered = [];
    revoked = [];
    as = await startAs();
    as.meta.revocation_endpoint = as.base + "/oauth/revoke";
    as.on("/oauth/register", (_q, r, hit) => {
      registered.push(JSON.parse(hit.body));
      json(r, 201, { client_id: `cid-${registered.length}` });
    });
    as.on("/oauth/authorize", (_q, r, hit) => {
      void hit;
      if (mode.cliScope === "refuse") {
        return json(r, 400, { error: { error: "invalid_scope", error_description: "Scope 'cli' is not granted here." } });
      }
      r.writeHead(302, { location: as.base + "/consent?req=x" });
      r.end();
    });
    as.on("/oauth/token", (_q, r, hit) => {
      const form = new URLSearchParams(hit.body);
      const scope = (form.get("code") ?? "").split(":")[1];
      // A refresh keeps the typ of the grant it renews.
      const rt = form.get("refresh_token");
      const typ = rt
        ? /CLI|cli_access/.test(rt) ? "cli_access" : "mcp_access"
        : scope === "cli" && mode.cliScope !== "old" ? "cli_access" : "mcp_access";
      n++;
      json(r, 200, { access_token: jwt(typ, n), refresh_token: `RT-${typ}-${n}`, expires_in: 3600, token_type: "Bearer" });
    });
    as.on("/oauth/revoke", (_q, r, hit) => {
      revoked.push(new URLSearchParams(hit.body).get("token") ?? "");
      json(r, 200, { ok: true });
    });
  });
  afterEach(async () => {
    await Promise.all(pending.splice(0));
    await as.close();
  });

  /** Plays the person: "Allow" in the browser; the code remembers the scope asked for. */
  const opener = (authUrl: string) => {
    const u = new URL(authUrl);
    const scope = u.searchParams.get("scope")!;
    opened.push(scope);
    const cb = new URL(u.searchParams.get("redirect_uri")!);
    cb.searchParams.set("state", u.searchParams.get("state")!);
    cb.searchParams.set("code", `code-${opened.length}:${scope}`);
    pending.push(fetch(cb).then(() => undefined));
  };

  it("login is one approval into the command-line file, 0600", async () => {
    const t = io();
    expect(await main(["login", "--base-url", as.base], t, { opener })).toBe(0);
    expect(opened).toEqual(["cli"]);
    expect(registered).toHaveLength(1);
    const c = doc(cliCredentialsPath());
    expect(c.client_id).toBe("cid-1");
    expect(tokenTyp(c.access_token)).toBe("cli_access");
    // Same shape as what Python writes.
    expect(Object.keys(c).sort()).toEqual(["access_token", "base_url", "client_id", "expires_at", "refresh_token"]);
    expect(statSync(cliCredentialsPath()).mode & 0o777).toBe(0o600);
    expect(existsSync(credentialsPath())).toBe(false);
    expect(t.o()).toContain("`anona mcp`");
    for (const secret of [c.access_token, c.refresh_token]) expect(t.o() + t.e()).not.toContain(secret);
  });

  it("login --cli does only the command-line approval", async () => {
    expect(await main(["login", "--cli", "--base-url", as.base], io(), { opener })).toBe(0);
    expect(opened).toEqual(["cli"]);
    expect(existsSync(cliCredentialsPath())).toBe(true);
    expect(existsSync(credentialsPath())).toBe(false);
  });

  it("login --mcp does only the MCP approval and sends no cli preflight", async () => {
    expect(await main(["login", "--mcp", "--base-url", as.base], io(), { opener })).toBe(0);
    expect(opened).toEqual(["mcp"]);
    expect(as.count("/oauth/authorize")).toBe(0);
    expect(existsSync(cliCredentialsPath())).toBe(false);
    expect(existsSync(credentialsPath())).toBe(true);
  });

  it("--cli with --mcp, or on another command, is a usage error", async () => {
    expect(await main(["login", "--cli", "--mcp"], io())).toBe(2);
    expect(await main(["status", "--cli"], io())).toBe(2);
    expect(registered).toEqual([]);
  });

  it("invalid_scope falls back to the MCP approval", async () => {
    mode.cliScope = "refuse";
    const t = io();
    expect(await main(["login", "--base-url", as.base], t, { opener })).toBe(0);
    expect(opened).toEqual(["mcp"]);
    expect(t.e()).toContain("Signing in for MCP only");
    expect(t.e()).toContain("does not issue command-line credentials");
    expect(t.e()).toContain("not granted here");
    expect(existsSync(cliCredentialsPath())).toBe(false);
    expect(existsSync(credentialsPath())).toBe(true);
    // Alone, the same refusal is a failure.
    expect(await main(["login", "--cli", "--base-url", as.base], io(), { opener })).toBe(1);
  });

  it("an older server minting an MCP token for scope cli saves nothing and revokes it", async () => {
    mode.cliScope = "old";
    const t = io();
    expect(await main(["login", "--base-url", as.base], t, { opener })).toBe(0);
    expect(existsSync(cliCredentialsPath())).toBe(false);
    expect(revoked).toHaveLength(1);
    expect(revoked[0]).not.toBe(doc(credentialsPath()).refresh_token);
    expect(t.e()).toContain("issued an MCP credential instead");
  });

  it("a temp profile is claimed by the one approval and forgotten", async () => {
    saveCredentials({ accessToken: "", refreshToken: "", expiresAt: 0, clientId: "", baseUrl: as.base, tempToken: "anona_tmp_SECRET", tempExpiresAt: Date.now() / 1000 + 3600 });
    as.on("/oauth/token", (_q, r, hit) => {
      const scope = (new URLSearchParams(hit.body).get("code") ?? "").split(":")[1];
      n++;
      json(r, 200, {
        access_token: jwt(scope === "cli" ? "cli_access" : "mcp_access", n),
        refresh_token: `RT-${n}`,
        expires_in: 3600,
        ...(scope === "cli" ? { temp_claimed: true } : {}),
      });
    });
    expect(await main(["login", "--base-url", as.base], io(), { opener })).toBe(0);
    expect(registered).toHaveLength(1);
    expect(registered[0]!.temp_token).toBe("anona_tmp_SECRET");
    // Spent by the claim: a file that held only the profile is gone.
    expect(existsSync(credentialsPath())).toBe(false);
    expect(doc(cliCredentialsPath()).temp_token).toBeUndefined();
  });

  function seedBoth(base = as.base) {
    saveCredentials({ accessToken: jwt("mcp_access", 90), refreshToken: "RT-MCP", expiresAt: Date.now() / 1000 + 3600, clientId: "m", baseUrl: base });
    saveCredentials({ accessToken: jwt("cli_access", 91), refreshToken: "RT-CLI", expiresAt: Date.now() / 1000 + 3600, clientId: "c", baseUrl: base }, cliCredentialsPath());
  }

  it("logout removes and revokes both", async () => {
    seedBoth();
    expect(await main(["logout"], io())).toBe(0);
    expect(existsSync(cliCredentialsPath())).toBe(false);
    expect(existsSync(credentialsPath())).toBe(false);
    expect(revoked.sort()).toEqual(["RT-CLI", "RT-MCP"]);
  });

  it("logout --cli and logout --mcp each remove only their own", async () => {
    seedBoth();
    const mcpBefore = readFileSync(credentialsPath());
    expect(await main(["logout", "--cli"], io())).toBe(0);
    expect(existsSync(cliCredentialsPath())).toBe(false);
    expect(readFileSync(credentialsPath())).toEqual(mcpBefore);
    expect(revoked).toEqual(["RT-CLI"]);
    seedBoth();
    const cliBefore = readFileSync(cliCredentialsPath());
    expect(await main(["logout", "--mcp"], io())).toBe(0);
    expect(existsSync(credentialsPath())).toBe(false);
    expect(readFileSync(cliCredentialsPath())).toEqual(cliBefore);
  });

  it("status reports both, never a token, and is success with only the command-line login", async () => {
    seedBoth();
    const t = io();
    expect(await main(["status"], t)).toBe(0);
    expect(t.o()).toContain(`Command-line login for ${as.base}`);
    expect(t.o()).toContain("`anona mcp`");
    expect(t.o()).toContain(`An MCP-only login for ${as.base} is also stored`);
    expect(t.o()).not.toMatch(/RT-|cli_access|eyJ/);
    await main(["logout", "--mcp"], io());
    const u = io();
    expect(await main(["status"], u)).toBe(0);
    // One login covers MCP: nothing tells the user to sign in again for it.
    expect(u.o()).not.toContain("anona login --mcp");
  });

  // --- which credential `anona mcp` presents ---------------------------------

  async function mcpCall(base = as.base, tokens: string[] = [], dead = new Set<string>()) {
    as.on("/mcp", (_q, r, hit) => {
      const tok = String(hit.headers.authorization ?? "").replace(/^Bearer /, "");
      tokens.push(tok);
      if (dead.has(tok)) return json(r, 401, { error: "invalid_token" });
      json(r, 200, { jsonrpc: "2.0", id: 1, result: { tools: [] } });
    });
    const stdin = new PassThrough();
    stdin.end(JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }) + "\n");
    const o: string[] = [], e: string[] = [];
    const rc = await runProxy(base, stdin, { write: (s: string) => void o.push(s) }, { write: (s: string) => void e.push(s) });
    return { rc, out: o.join(""), err: e.join(""), tokens };
  }

  it("anona mcp presents the command-line login", async () => {
    saveCredentials({ accessToken: jwt("cli_access", 91), refreshToken: "RT-CLI", expiresAt: Date.now() / 1000 + 3600, clientId: "c", baseUrl: as.base }, cliCredentialsPath());
    const r = await mcpCall();
    expect(JSON.parse(r.out).result).toEqual({ tools: [] });
    expect(r.tokens).toEqual([jwt("cli_access", 91)]);
  });

  it("the command-line login beats an MCP-only login", async () => {
    seedBoth();
    const r = await mcpCall();
    expect(r.tokens).toEqual([jwt("cli_access", 91)]);
  });

  it("a command-line login for another deployment is never sent", async () => {
    seedBoth();
    saveCredentials({ accessToken: jwt("cli_access", 91), refreshToken: "RT-CLI", expiresAt: Date.now() / 1000 + 3600, clientId: "c", baseUrl: "https://other.example" }, cliCredentialsPath());
    const r = await mcpCall();
    expect(r.tokens).toEqual([jwt("mcp_access", 90)]);
  });

  it("a refused command-line token is renewed in its own file", async () => {
    seedBoth();
    const r = await mcpCall(as.base, [], new Set([jwt("cli_access", 91)]));
    expect(JSON.parse(r.out).result).toEqual({ tools: [] });
    expect(r.tokens).toHaveLength(2);
    expect(r.tokens[0]).toBe(jwt("cli_access", 91));
    expect(tokenTyp(r.tokens[1]!)).toBe("cli_access");
    expect(doc(cliCredentialsPath()).access_token).toBe(r.tokens[1]);
    expect(doc(credentialsPath()).refresh_token).toBe("RT-MCP");
  });
});
