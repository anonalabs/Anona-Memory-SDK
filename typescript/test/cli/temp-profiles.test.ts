/**
 * Temporary profiles (`anona start`), parity with the Python CLI.
 *
 * The file is shared, so the Node CLI has to read what `anona start` wrote.
 * Every guard here has one test that only it fails; the test names say which
 * guard. The token is a sentinel so a leak anywhere is a string match.
 */
import { PassThrough } from "node:stream";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { main } from "../../src/cli/index.js";
import { describeDeadline } from "../../src/cli/temp.js";
import { runLogin } from "../../src/cli/login.js";
import { registerClient, RegistrationRejected } from "../../src/cli/oauth.js";
import { credentialsPath, liveTempToken, loadCredentials, saveCredentials, type Credentials } from "../../src/cli/store.js";
import { accessToken } from "../../src/cli/tokens.js";
import { drive, json, startAs, tokenOk, useTempHome, type FakeAs } from "./helpers.js";

const TEMP = "anona_tmp_SECRETSENTINEL";
const HOUR = 3600;
const now = () => Date.now() / 1000;

function io(input = "") {
  const o: string[] = [], e: string[] = [];
  const stdin = new PassThrough();
  stdin.end(input);
  return { stdin, stdout: { write: (s: string) => void o.push(s) }, stderr: { write: (s: string) => void e.push(s) }, o: () => o.join(""), e: () => e.join("") };
}
const all = (t: ReturnType<typeof io>) => t.o() + t.e();

function seedTemp(base: string, o: Partial<Credentials> = {}) {
  saveCredentials({ accessToken: "", refreshToken: "", expiresAt: 0, clientId: "", baseUrl: base, tempToken: TEMP, tempExpiresAt: now() + 71.5 * HOUR, ...o });
}
function seedBoth(base: string, o: Partial<Credentials> = {}) {
  saveCredentials({ accessToken: "OAUTH-A", refreshToken: "OAUTH-R", expiresAt: now() + HOUR, clientId: "cid-1", baseUrl: base, tempToken: TEMP, tempExpiresAt: now() + 71.5 * HOUR, ...o });
}
const rawFile = () => JSON.parse(readFileSync(credentialsPath(), "utf8"));
const iso = (secs: number) => new Date(secs * 1000).toISOString();

describe("store: temp fields", () => {
  useTempHome();

  it("writes the Python key names, and only when a temp token is set", () => {
    seedTemp("https://x.test", { tempExpiresAt: 1900000000.5 });
    expect(rawFile()).toMatchObject({ temp_token: TEMP, temp_expires_at: 1900000000.5 });
    expect(loadCredentials()).toMatchObject({ tempToken: TEMP, tempExpiresAt: 1900000000.5 });
    // ...and an ordinary login's file is what it always was.
    saveCredentials(base());
    expect(Object.keys(rawFile())).toEqual(["access_token", "refresh_token", "expires_at", "client_id", "base_url"]);
  });

  it("wrong-typed temp fields read as absent without making the credential unreadable", () => {
    seedBoth("https://x.test");
    writeFileSync(credentialsPath(), JSON.stringify({ ...rawFile(), temp_token: 5, temp_expires_at: "soon" }));
    const c = loadCredentials();
    expect(c).not.toBeNull();
    expect(c!.accessToken).toBe("OAUTH-A");
    expect(c!.tempToken).toBeUndefined();
    expect(c!.tempExpiresAt).toBeUndefined();
  });

  it("liveTempToken: a future deadline is live", () => {
    expect(liveTempToken({ ...base(), tempToken: TEMP, tempExpiresAt: now() + 60 })).toBe(TEMP);
  });
  it("liveTempToken: a past deadline is not", () => {
    expect(liveTempToken({ ...base(), tempToken: TEMP, tempExpiresAt: now() - 1 })).toBeNull();
  });
  it("liveTempToken: a token with no deadline is not", () => {
    expect(liveTempToken({ ...base(), tempToken: TEMP })).toBeNull();
  });
  it("liveTempToken: an empty token is not, even with a future deadline", () => {
    expect(liveTempToken({ ...base(), tempToken: "", tempExpiresAt: now() + 60 })).toBeNull();
  });
});
function base(): Credentials {
  return { accessToken: "", refreshToken: "", expiresAt: 0, clientId: "", baseUrl: "https://x.test" };
}

describe("describeDeadline", () => {
  const at = Date.UTC(2026, 9, 6, 15, 22) / 1000;
  it("hours, in Python's exact words", () => {
    expect(describeDeadline(at, at - 71 * HOUR - 600)).toBe("expires 2026-10-06 15:22 UTC (in 71 hours), after which its memories are deleted");
  });
  it("minutes under an hour", () => {
    expect(describeDeadline(at, at - 45 * 60 - 10)).toBe("expires 2026-10-06 15:22 UTC (in 45 minutes), after which its memories are deleted");
  });
  it("past tense once the deadline has gone", () => {
    expect(describeDeadline(at, at + 1)).toBe("expired 2026-10-06 15:22 UTC; its memories have been or will shortly be deleted");
  });
});

describe("anona start", () => {
  useTempHome();
  let as: FakeAs;
  const created = (expiresAt: string | unknown = iso(now() + 71.5 * HOUR), extra: object = {}) =>
    as.on("/v1/temp/profiles", (_q, r) => json(r, 201, { temp_token: TEMP, expires_at: expiresAt, ...extra }));
  beforeEach(async () => (as = await startAs()));
  afterEach(() => as.close());

  it("creates a profile, stores it, and prints Python's text", async () => {
    created();
    const t = io();
    expect(await main(["--base-url", as.base, "start"], t)).toBe(0);
    expect(as.hits.find((h) => h.path === "/v1/temp/profiles")!.method).toBe("POST");
    expect(t.o()).toMatch(
      /^Temporary profile created\. It expires \d{4}-\d\d-\d\d \d\d:\d\d UTC \(in 71 hours\), after which its memories are deleted\.\n/,
    );
    expect(t.o()).toContain(
      "Nothing warns you before then: run `anona status` to check, and\n`anona login` to claim it and keep what it holds.\nIts space is created by the first memory written to it.\n",
    );
    expect(rawFile()).toMatchObject({ access_token: "", refresh_token: "", client_id: "", base_url: as.base, temp_token: TEMP });
    expect(all(t)).not.toContain(TEMP);
  });

  it("guard already-signed-in: refuses, and sends nothing", async () => {
    seedBoth(as.base, { tempToken: undefined, tempExpiresAt: undefined });
    created();
    const t = io();
    expect(await main(["--base-url", as.base, "start"], t)).toBe(1);
    expect(t.e()).toContain("already signed in");
    expect(as.count("/v1/temp/profiles")).toBe(0);
    expect(loadCredentials()!.accessToken).toBe("OAUTH-A");
  });

  it("guard live-profile-exists: a second is refused, exit 0, nothing sent, nothing overwritten", async () => {
    seedTemp(as.base);
    created(iso(now() + 5 * HOUR), {});
    const before = readFileSync(credentialsPath(), "utf8");
    const t = io();
    expect(await main(["--base-url", as.base, "start"], t)).toBe(0);
    expect(t.o()).toContain("A temporary profile already exists; not creating another.");
    expect(t.o()).toContain("(in 71 hours)");
    expect(as.count("/v1/temp/profiles")).toBe(0);
    expect(readFileSync(credentialsPath(), "utf8")).toBe(before);
    expect(all(t)).not.toContain(TEMP);
  });

  it("guard live-profile-exists is about LIVE: an expired one does not block a new one", async () => {
    seedTemp(as.base, { tempExpiresAt: now() - HOUR });
    created();
    expect(await main(["--base-url", as.base, "start"], io())).toBe(0);
    expect(as.count("/v1/temp/profiles")).toBe(1);
    expect(loadCredentials()!.tempExpiresAt).toBeGreaterThan(now());
  });

  it("guard status-201: a 200 is refused even with a perfect body", async () => {
    as.on("/v1/temp/profiles", (_q, r) => json(r, 200, { temp_token: TEMP, expires_at: iso(now() + HOUR) }));
    const t = io();
    expect(await main(["--base-url", as.base, "start"], t)).toBe(1);
    expect(t.e()).toContain("(200)");
    expect(existsSync(credentialsPath())).toBe(false);
  });

  it("429 says to try tomorrow and saves nothing", async () => {
    as.on("/v1/temp/profiles", (_q, r) => json(r, 429, {}));
    const t = io();
    expect(await main(["--base-url", as.base, "start"], t)).toBe(1);
    expect(t.e()).toContain("Try again tomorrow");
    expect(existsSync(credentialsPath())).toBe(false);
  });

  it("guard token-prefix: a token without anona_tmp_ is refused, and not echoed", async () => {
    as.on("/v1/temp/profiles", (_q, r) => json(r, 201, { temp_token: "sk_WRONGSHAPE", expires_at: iso(now() + HOUR) }));
    const t = io();
    expect(await main(["--base-url", as.base, "start"], t)).toBe(1);
    expect(t.e()).toContain("no usable token");
    expect(all(t)).not.toContain("WRONGSHAPE");
    expect(existsSync(credentialsPath())).toBe(false);
  });

  it("guard body-is-object: an array body is refused", async () => {
    as.on("/v1/temp/profiles", (_q, r) => json(r, 201, [TEMP]));
    const t = io();
    expect(await main(["--base-url", as.base, "start"], t)).toBe(1);
    expect(t.e()).toContain("not a JSON object");
    expect(all(t)).not.toContain(TEMP);
  });

  it("guard deadline-present: a missing expiry saves nothing", async () => {
    created(null);
    const t = io();
    expect(await main(["--base-url", as.base, "start"], t)).toBe(1);
    expect(t.e()).toContain("no expiry time");
    expect(existsSync(credentialsPath())).toBe(false);
  });

  it("guard deadline-parses: an expiry that is not a date saves nothing and never prints the valid token it arrived with", async () => {
    created("tomorrow-ish");
    const t = io();
    expect(await main(["--base-url", as.base, "start"], t)).toBe(1);
    expect(t.e()).toContain("not a date");
    expect(all(t)).not.toContain(TEMP);
    expect(existsSync(credentialsPath())).toBe(false);
  });

  it("guard iso-shape: a bare year is not a date, though JS would parse it", async () => {
    created("2099");
    const t = io();
    expect(await main(["--base-url", as.base, "start"], t)).toBe(1);
    expect(t.e()).toContain("not a date");
    expect(existsSync(credentialsPath())).toBe(false);
  });

  it("an expiry with no zone is UTC, as in Python, whatever the local zone", async () => {
    const prev = process.env.TZ;
    process.env.TZ = "America/New_York";
    try {
      created("2099-10-06T15:22:00");
      expect(await main(["--base-url", as.base, "start"], io())).toBe(0);
      expect(loadCredentials()!.tempExpiresAt).toBe(Date.UTC(2099, 9, 6, 15, 22) / 1000);
    } finally {
      if (prev === undefined) delete process.env.TZ;
      else process.env.TZ = prev;
    }
  });

  it("a trailing Z and an offset both parse", async () => {
    created("2099-10-06T15:22:00Z");
    await main(["--base-url", as.base, "start"], io());
    expect(loadCredentials()!.tempExpiresAt).toBe(Date.UTC(2099, 9, 6, 15, 22) / 1000);
    rmFile();
    created("2099-10-06T17:22:00+02:00");
    await main(["--base-url", as.base, "start"], io());
    expect(loadCredentials()!.tempExpiresAt).toBe(Date.UTC(2099, 9, 6, 15, 22) / 1000);
  });

  it("an unreachable server is a message, not a stack", async () => {
    const t = io();
    expect(await main(["--base-url", "http://127.0.0.1:1", "start"], t)).toBe(1);
    expect(t.e()).toMatch(/Could not reach http:\/\/127\.0\.0\.1:1/);
  });
});
function rmFile() {
  writeFileSync(credentialsPath(), "{}");
}

describe("anona status with a temp profile", () => {
  useTempHome();
  it("a temp-only file is described as a temp profile, with its deadline, and claims no OAuth credential", async () => {
    seedTemp("https://t.test");
    const t = io();
    expect(await main(["status"], t)).toBe(0);
    expect(t.o()).toContain("Temporary profile for https://t.test: expires ");
    expect(t.o()).toContain("(in 71 hours)");
    expect(t.o()).toContain("Run `anona login` to claim it before then.");
    expect(t.o()).not.toMatch(/Credential found|access token|refresh/);
    expect(all(t)).not.toContain(TEMP);
  });

  it("guard still-live: an expired profile says expired and does not invite a claim", async () => {
    seedTemp("https://t.test", { tempExpiresAt: now() - 2 * HOUR });
    const t = io();
    expect(await main(["status"], t)).toBe(0);
    expect(t.o()).toContain("expired ");
    expect(t.o()).not.toContain("claim it before then");
  });

  it("guard temp-only-stops: a profile plus an OAuth login reports both", async () => {
    seedBoth("https://t.test");
    const t = io();
    expect(await main(["status"], t)).toBe(0);
    expect(t.o()).toContain("Temporary profile for https://t.test");
    expect(t.o()).toContain("Credential found for https://t.test.");
    expect(all(t)).not.toMatch(/OAUTH-|SECRETSENTINEL/);
  });

  it("guard not-signed-in: a file with empty OAuth fields and no temp token is logged out, exit 1", async () => {
    saveCredentials(base());
    const t = io();
    expect(await main(["status"], t)).toBe(1);
    expect(t.o()).toContain("Not logged in");
    expect(t.o()).not.toContain("Credential found");
  });

  it("guard needs-deadline: a temp token with no deadline is not a profile; status says logged out", async () => {
    seedTemp("https://t.test", { tempExpiresAt: undefined });
    const t = io();
    expect(await main(["status"], t)).toBe(1);
    expect(t.o()).toContain("Not logged in");
  });
});

describe("anona logout with a temp profile", () => {
  useTempHome();
  it("refuses a live profile without --force, names the deadline, keeps the file, prints no token", async () => {
    seedTemp("https://t.test");
    const t = io();
    expect(await main(["logout"], t)).toBe(1);
    expect(t.e()).toContain("abandon an unclaimed temporary profile");
    expect(t.e()).toContain("(in 71 hours)");
    expect(t.o()).not.toContain("Logged out");
    expect(existsSync(credentialsPath())).toBe(true);
    expect(loadCredentials()!.tempToken).toBe(TEMP);
    expect(all(t)).not.toContain(TEMP);
  });

  it("--force discards it", async () => {
    seedTemp("https://t.test");
    expect(await main(["logout", "--force"], io())).toBe(0);
    expect(existsSync(credentialsPath())).toBe(false);
  });

  it("guard live-only: an expired profile no longer blocks logout", async () => {
    seedTemp("https://t.test", { tempExpiresAt: now() - HOUR });
    expect(await main(["logout"], io())).toBe(0);
    expect(existsSync(credentialsPath())).toBe(false);
  });

  it("guard independent-of-login: a signed-in credential that still holds a live profile is refused too", async () => {
    seedBoth("https://t.test");
    expect(await main(["logout"], io())).toBe(1);
    expect(existsSync(credentialsPath())).toBe(true);
  });

  it("an ordinary logout is untouched", async () => {
    seedBoth("https://t.test", { tempToken: undefined, tempExpiresAt: undefined });
    expect(await main(["logout"], io())).toBe(0);
    expect(existsSync(credentialsPath())).toBe(false);
  });

  it("guard force-scope: --force on another command is a usage error", async () => {
    const t = io();
    expect(await main(["status", "--force"], t)).toBe(2);
    expect(t.e()).toContain("--force only applies to logout");
  });
});

describe("proxy bearer selection", () => {
  useTempHome();
  let as: FakeAs;
  const call = (id: number) => JSON.stringify({ jsonrpc: "2.0", id, method: "tools/list" });
  beforeEach(async () => {
    as = await startAs();
    as.on("/mcp", (_q, r, hit) => json(r, 200, { jsonrpc: "2.0", id: JSON.parse(hit.body).id, result: {} }));
    as.on("/oauth/token", tokenOk("tok-2", "rt-2"));
  });
  afterEach(() => as.close());
  const auths = () => as.hits.filter((h) => h.path === "/mcp").map((h) => h.headers.authorization);
  const oauthHits = () => as.hits.filter((h) => h.path.startsWith("/oauth") || h.path.startsWith("/.well-known")).length;

  it("a temp-only file sends the temp token as the bearer, and never touches OAuth", async () => {
    seedTemp(as.base);
    const { out, err } = await drive(as.base, [call(1)]);
    expect(out).toEqual([{ jsonrpc: "2.0", id: 1, result: {} }]);
    expect(auths()).toEqual([`Bearer ${TEMP}`]);
    expect(oauthHits()).toBe(0);
    expect(err).not.toContain("anona login");
  });

  it("an OAuth access token wins over a stored temp token", async () => {
    seedBoth(as.base);
    await drive(as.base, [call(1)]);
    expect(auths()).toEqual(["Bearer OAUTH-A"]);
  });

  it("a 401 on OAuth refreshes and retries with OAuth, never swapping in the temp token", async () => {
    seedBoth(as.base);
    let first = true;
    as.on("/mcp", (_q, r, hit) => (first ? ((first = false), json(r, 401, {})) : json(r, 200, { jsonrpc: "2.0", id: JSON.parse(hit.body).id, result: {} })));
    await drive(as.base, [call(1)]);
    expect(auths()).toEqual(["Bearer OAUTH-A", "Bearer tok-2"]);
    expect(rawFile().temp_token).toBe(TEMP);
  });

  it("a 401 on a temp token is ONE post, no discovery, no refresh, and a message that names neither", async () => {
    seedTemp(as.base);
    as.on("/mcp", (_q, r) => json(r, 401, {}));
    const { out, err } = await drive(as.base, [call(1), call(2)]);
    expect(auths()).toEqual([`Bearer ${TEMP}`, `Bearer ${TEMP}`]); // one per message, never two
    expect(oauthHits()).toBe(0);
    expect(out.map((o) => o.error.message)).toEqual(Array(2).fill(expect.stringContaining("expired, or already claimed")));
    expect(JSON.stringify(out) + err).not.toContain(TEMP);
    expect(loadCredentials()!.tempToken).toBe(TEMP); // file untouched
  });

  it("an expired temp-only profile sends nothing and points at `anona start`, not at a login", async () => {
    seedTemp(as.base, { tempExpiresAt: now() - HOUR });
    const { out, err } = await drive(as.base, [call(1)]);
    expect(as.count("/mcp")).toBe(0);
    expect(out[0].error.message).toContain("anona start");
    expect(out[0].error.message).not.toContain("login again");
    expect(JSON.stringify(out) + err).not.toContain(TEMP);
  });

  it("guard needs-deadline: a temp token with no deadline is not sent", async () => {
    seedTemp(as.base, { tempExpiresAt: undefined });
    const { out } = await drive(as.base, [call(1)]);
    expect(as.count("/mcp")).toBe(0);
    expect(out[0].error).toBeDefined();
  });

  it("refresh preserves a temp profile the file also holds", async () => {
    seedBoth(as.base, { expiresAt: now() - 10 });
    expect(await accessToken()).toBe("tok-2");
    expect(rawFile()).toMatchObject({ access_token: "tok-2", temp_token: TEMP });
    expect(rawFile().temp_expires_at).toBeGreaterThan(now());
  });
});

describe("login and the temp token", () => {
  useTempHome();
  let as: FakeAs;
  const pending: Promise<void>[] = [];
  afterEach(async () => void (await Promise.all(pending.splice(0))));
  const ok = (url: string) => {
    const u = new URL(url);
    const cb = new URL(u.searchParams.get("redirect_uri")!);
    cb.searchParams.set("state", u.searchParams.get("state")!);
    cb.searchParams.set("code", "CODE-SECRET");
    pending.push(fetch(cb).then(() => undefined));
  };
  const sink = () => {
    const c: string[] = [];
    return { write: (s: string) => void c.push(s), text: () => c.join("") };
  };
  const regBodies = () => as.hits.filter((h) => h.path === "/oauth/register").map((h) => JSON.parse(h.body));
  const tokenBody = (extra: object = {}) => (_q: unknown, r: any) =>
    json(r, 200, { access_token: "NEW-A", refresh_token: "NEW-R", expires_in: 3600, token_type: "Bearer", ...extra });
  async function login() {
    const out = sink(), err = sink();
    let url = "";
    const rc = await runLogin(as.base, { opener: (u) => ((url = u), ok(u)), stdout: out, stderr: err });
    return { rc, out, err, url };
  }
  beforeEach(async () => {
    as = await startAs();
    as.on("/oauth/register", (_q, r) => json(r, 201, { client_id: "cid-new" }));
    as.on("/oauth/token", tokenBody());
  });
  afterEach(() => as.close());

  it("sends the stored token in the registration body, and nowhere else", async () => {
    seedTemp(as.base);
    const { rc, out, err, url } = await login();
    expect(rc).toBe(0);
    expect(regBodies()[0].temp_token).toBe(TEMP);
    expect(url).not.toContain(TEMP);
    expect(as.hits.filter((h) => h.path !== "/oauth/register").map((h) => h.body + JSON.stringify(h.headers) + h.path).join("\n")).not.toContain(TEMP);
    expect(out.text() + err.text()).not.toContain(TEMP);
  });

  it("sends none when there is no stored profile", async () => {
    await login();
    expect(regBodies()[0]).not.toHaveProperty("temp_token");
  });

  it("guard same-deployment: a profile minted by another host is not sent here", async () => {
    seedTemp("http://other.test");
    await login();
    expect(regBodies()[0]).not.toHaveProperty("temp_token");
  });

  it("guard live: an expired profile is not sent", async () => {
    seedTemp(as.base, { tempExpiresAt: now() - HOUR });
    await login();
    expect(regBodies()[0]).not.toHaveProperty("temp_token");
  });

  it("a confirmed claim (temp_claimed: true) clears the stored token", async () => {
    seedTemp(as.base);
    as.on("/oauth/token", tokenBody({ temp_claimed: true }));
    await login();
    const raw = rawFile();
    expect(raw.access_token).toBe("NEW-A");
    expect(raw).not.toHaveProperty("temp_token");
    expect(raw).not.toHaveProperty("temp_expires_at");
  });

  it("no claim confirmed: the token survives the login, deadline and all", async () => {
    seedTemp(as.base, { tempExpiresAt: 4102444800 });
    await login();
    expect(rawFile()).toMatchObject({ access_token: "NEW-A", temp_token: TEMP, temp_expires_at: 4102444800 });
  });

  it("guard literal-true: temp_claimed as a string, a number or 'false' is not a claim", async () => {
    for (const v of ["true", 1, "yes"]) {
      seedTemp(as.base);
      as.on("/oauth/token", tokenBody({ temp_claimed: v }));
      await login();
      expect(rawFile().temp_token, `temp_claimed=${JSON.stringify(v)}`).toBe(TEMP);
    }
  });

  it("guard carry-same-deployment: an unclaimed profile from another host is NOT retagged onto this one", async () => {
    seedTemp("http://other.test");
    await login();
    expect(rawFile()).not.toHaveProperty("temp_token");
    expect(rawFile().base_url).toBe(as.base);
  });

  it("guard retry-scope(a): a stale token is dropped on 400 invalid_request, once, with a notice, and not written back", async () => {
    seedTemp(as.base);
    let n = 0;
    as.on("/oauth/register", (_q, r) =>
      ++n === 1 ? json(r, 400, { error: "invalid_request", error_description: "That profile is already claimed." }) : json(r, 201, { client_id: "cid-2" }),
    );
    const { rc, err } = await login();
    expect(rc).toBe(0);
    expect(regBodies().map((b) => "temp_token" in b)).toEqual([true, false]);
    expect(err.text()).toContain("can no longer be claimed. Client registration failed with 400: That profile is already claimed. Signing in without it.");
    expect(rawFile()).not.toHaveProperty("temp_token");
    expect(err.text()).not.toContain(TEMP);
  });

  it("guard retry-scope(b): a 400 with a different error code is NOT retried", async () => {
    seedTemp(as.base);
    as.on("/oauth/register", (_q, r) => json(r, 400, { error: "invalid_redirect_uri" }));
    const { rc } = await login();
    expect(rc).toBe(1);
    expect(as.count("/oauth/register")).toBe(1);
    expect(loadCredentials()!.tempToken).toBe(TEMP);
  });

  it("guard retry-scope(c): a 500 is NOT retried", async () => {
    seedTemp(as.base);
    as.on("/oauth/register", (_q, r) => json(r, 500, { error: "invalid_request" }));
    const { rc } = await login();
    expect(rc).toBe(1);
    expect(as.count("/oauth/register")).toBe(1);
  });

  it("guard retry-scope(d): with no token sent, invalid_request is NOT retried", async () => {
    as.on("/oauth/register", (_q, r) => json(r, 400, { error: "invalid_request" }));
    const { rc } = await login();
    expect(rc).toBe(1);
    expect(as.count("/oauth/register")).toBe(1);
  });

  it("the gateway's enveloped error ({error: {error, message}}) is read too", async () => {
    seedTemp(as.base);
    let n = 0;
    as.on("/oauth/register", (_q, r) =>
      ++n === 1 ? json(r, 400, { error: { error: "invalid_request", message: "Claimed." } }) : json(r, 201, { client_id: "c" }),
    );
    expect((await login()).rc).toBe(0);
    expect(as.count("/oauth/register")).toBe(2);
  });

  it("a server that echoes the token in its refusal does not get it onto the terminal", async () => {
    seedTemp(as.base);
    as.on("/oauth/register", (_q, r) => json(r, 403, { error: "forbidden", error_description: `bad ${TEMP} here` }));
    const { rc, err, out } = await login();
    expect(rc).toBe(1);
    expect(err.text()).toContain("bad [redacted] here");
    expect(err.text() + out.text()).not.toContain(TEMP);
  });

  it("a token straddling the 300-character cap leaves no fragment behind (redact before truncating)", async () => {
    seedTemp(as.base);
    // 280 chars of filler, then the token: a cap applied first cuts it after 19 of its 24 characters.
    const filler = "x".repeat(279) + " ";
    as.on("/oauth/register", (_q, r) => json(r, 403, { error: `e_${TEMP}`, error_description: filler + TEMP + " tail" }));
    const { rc, err, out } = await login();
    expect(rc).toBe(1);
    const seen = err.text() + out.text();
    expect(seen).toContain("[redacted]");
    // No fragment of the token, down to its 6-character prefix.
    for (let n = 6; n <= TEMP.length; n++) expect(seen).not.toContain(TEMP.slice(0, n));
    for (let n = 6; n <= TEMP.length; n++) expect(seen).not.toContain(TEMP.slice(-n));
  });

  it("the refusal's error code is redacted too, because it rides on the exception", async () => {
    as.on("/oauth/register", (_q, r) => json(r, 400, { error: `e_${TEMP}` }));
    const meta = { ...as.meta } as never;
    const e = await registerClient(meta, "http://127.0.0.1:1/cb", TEMP).catch((x) => x);
    expect(e).toBeInstanceOf(RegistrationRejected);
    expect(e.error).toBe("e_[redacted]");
    expect(JSON.stringify([e.message, e.error])).not.toContain("SECRETSENTINEL");
  });

  it("server text is made printable: newlines cannot forge extra lines", async () => {
    as.on("/oauth/register", (_q, r) => json(r, 403, { error_description: "line one\nanona login: forged" }));
    const { err } = await login();
    expect(err.text()).toBe("anona login: Client registration failed with 403: line one anona login: forged\n");
  });

  it("login names the abandoned profile when the stored one is for another deployment", async () => {
    seedTemp("https://other.test");
    const t = io();
    await main(["login", "--no-browser", "--base-url", "http://127.0.0.1:1"], t);
    expect(t.o()).toContain("that includes an unclaimed temporary profile");
    expect(t.o()).toContain("anona login --base-url https://other.test");
    expect(all(t)).not.toContain(TEMP);
  });

  it("login says nothing about a profile when the stored one is expired", async () => {
    seedTemp("https://other.test", { tempExpiresAt: now() - HOUR });
    const t = io();
    await main(["login", "--no-browser", "--base-url", "http://127.0.0.1:1"], t);
    expect(t.o()).toContain("this replaces it.");
    expect(t.o()).not.toContain("unclaimed temporary profile");
  });
});

