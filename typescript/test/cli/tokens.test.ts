import { existsSync, readFileSync, utimesSync, writeFileSync, statSync } from "node:fs";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { credentialsPath, loadCredentials } from "../../src/cli/store.js";
import { accessToken, lockTuning, NotLoggedIn } from "../../src/cli/tokens.js";
import { OAuthError } from "../../src/cli/oauth.js";
import { json, seed, startAs, tokenOk, useTempHome, type FakeAs } from "./helpers.js";

describe("accessToken", () => {
  useTempHome();
  let as: FakeAs;
  beforeEach(async () => void (as = await startAs()));
  afterEach(() => as.close());
  const lock = () => credentialsPath() + ".nodelock";

  it("raises NotLoggedIn with no credential", async () => {
    await expect(accessToken()).rejects.toThrow(NotLoggedIn);
  });
  it("returns a fresh token without any network", async () => {
    seed(as.base, { offset: 3600 });
    expect(await accessToken()).toBe("tok-1");
    expect(as.hits).toHaveLength(0);
  });
  it("refreshes inside the skew window and not outside it", async () => {
    as.on("/oauth/token", tokenOk("tok-2", "rt-2"));
    seed(as.base, { offset: 30 });
    expect(await accessToken()).toBe("tok-2");
    seed(as.base, { offset: 90 });
    expect(await accessToken()).toBe("tok-1");
    expect(as.count("/oauth/token")).toBe(1);
  });
  it("persists the rotated pair before returning, and sends the old refresh token", async () => {
    as.on("/oauth/token", tokenOk("tok-2", "rt-2"));
    seed(as.base, { offset: -10 });
    await accessToken();
    expect(loadCredentials()).toMatchObject({ accessToken: "tok-2", refreshToken: "rt-2", clientId: "cid-1", baseUrl: as.base });
    const sent = new URLSearchParams(as.hits.find((h) => h.path === "/oauth/token")!.body);
    expect(sent.get("grant_type")).toBe("refresh_token");
    expect(sent.get("refresh_token")).toBe("rt-1");
    expect(sent.get("client_id")).toBe("cid-1");
  });
  it("releases the lock after a success", async () => {
    as.on("/oauth/token", tokenOk("tok-2", "rt-2"));
    seed(as.base, { offset: -10 });
    await accessToken();
    expect(existsSync(lock())).toBe(false);
  });
  it("releases the lock after a failure too", async () => {
    as.on("/oauth/token", (_q, r) => json(r, 400, {}));
    seed(as.base, { offset: -10 });
    await expect(accessToken()).rejects.toThrow(NotLoggedIn);
    expect(existsSync(lock())).toBe(false);
  });
  it("a rejected refresh token reads as logged out and leaves the file alone", async () => {
    for (const status of [400, 401]) {
      as.on("/oauth/token", (_q, r) => json(r, status, {}));
      seed(as.base, { offset: -10 });
      const before = readFileSync(credentialsPath(), "utf8");
      await expect(accessToken()).rejects.toThrow(NotLoggedIn);
      expect(readFileSync(credentialsPath(), "utf8")).toBe(before);
    }
  });
  it("a 5xx is transient: not a logout, file untouched", async () => {
    as.on("/oauth/token", (_q, r) => json(r, 503, {}));
    seed(as.base, { offset: -10 });
    const before = readFileSync(credentialsPath(), "utf8");
    const err = await accessToken().catch((e) => e);
    expect(err).toBeInstanceOf(OAuthError);
    expect(err).not.toBeInstanceOf(NotLoggedIn);
    expect(readFileSync(credentialsPath(), "utf8")).toBe(before);
  });
  it.each([
    ["no access_token", { refresh_token: "r", expires_in: 3600 }],
    ["no refresh_token", { access_token: "a", expires_in: 3600 }],
    ["no expires_in", { access_token: "a", refresh_token: "r" }],
    ["zero expires_in", { access_token: "a", refresh_token: "r", expires_in: 0 }],
    ["string expires_in", { access_token: "a", refresh_token: "r", expires_in: "3600" }],
  ])("refuses a rotated response with %s and keeps the old file", async (_n, body) => {
    as.on("/oauth/token", (_q, r) => json(r, 200, body));
    seed(as.base, { offset: -10 });
    const before = readFileSync(credentialsPath(), "utf8");
    await expect(accessToken()).rejects.toThrow(OAuthError);
    expect(readFileSync(credentialsPath(), "utf8")).toBe(before);
  });
  it("a refresh response that is JSON null is an OAuthError, not a TypeError", async () => {
    as.on("/oauth/token", (_q, r) => json(r, 200, null));
    seed(as.base, { offset: -10 });
    await expect(accessToken()).rejects.toThrow(/not an object/);
  });
  it("refreshes only if what is stored is still the rejected token", async () => {
    as.on("/oauth/token", tokenOk("tok-3", "rt-3"));
    seed(as.base, { offset: 3600, access: "new" });
    // Someone else already rotated: ours was "old", the file holds "new".
    expect(await accessToken({ forceRefresh: true, rejected: "old" })).toBe("new");
    expect(as.count("/oauth/token")).toBe(0);
    // Same token is still the stored one: do refresh.
    expect(await accessToken({ forceRefresh: true, rejected: "new" })).toBe("tok-3");
    expect(as.count("/oauth/token")).toBe(1);
  });
  it("`rejected` alone does not force a refresh", async () => {
    seed(as.base, { offset: 3600 });
    expect(await accessToken({ rejected: "tok-1" })).toBe("tok-1");
    expect(as.hits).toHaveLength(0);
  });

  it("many concurrent callers on an expired token cause ONE rotation", async () => {
    as.on("/oauth/token", async (_q, r) => {
      await new Promise((s) => setTimeout(s, 80)); // a slow refresh keeps the window open
      tokenOk("tok-2", "rt-2")(_q, r, null as never);
    });
    seed(as.base, { offset: -10 });
    const got = await Promise.all(Array.from({ length: 6 }, () => accessToken()));
    expect(got).toEqual(Array(6).fill("tok-2"));
    expect(as.count("/oauth/token")).toBe(1);
  });

  it("recovers a stale lock left by a dead process", async () => {
    as.on("/oauth/token", tokenOk("tok-2", "rt-2"));
    seed(as.base, { offset: -10 });
    writeFileSync(lock(), "99999");
    const old = new Date(Date.now() - lockTuning.staleMs - 5_000);
    utimesSync(lock(), old, old);
    expect(await accessToken()).toBe("tok-2");
  });
  it("waits for a live lock, then gives up with a message rather than hanging", async () => {
    const saved = { ...lockTuning };
    Object.assign(lockTuning, { acquireTimeoutMs: 300, staleMs: 60_000 });
    try {
      seed(as.base, { offset: -10 });
      writeFileSync(lock(), "1"); // fresh mtime: someone is working
      const t = Date.now();
      await expect(accessToken()).rejects.toThrow(/Timed out waiting/);
      expect(Date.now() - t).toBeGreaterThanOrEqual(250);
      expect(as.hits).toHaveLength(0);
      expect(statSync(lock()).isFile()).toBe(true); // not ours to delete
    } finally {
      Object.assign(lockTuning, saved);
    }
  });
});
