import { existsSync, readdirSync, readFileSync, statSync, writeFileSync, mkdirSync, chmodSync } from "node:fs";
import { dirname } from "node:path";
import { describe, expect, it } from "vitest";
import { clearCredentials, credentialsPath, loadCredentials, saveCredentials, type Credentials } from "../../src/cli/store.js";
import { useTempHome } from "./helpers.js";

const C: Credentials = { accessToken: "a", refreshToken: "r", expiresAt: 1234.5, clientId: "cid", baseUrl: "https://x.test" };

describe("store", () => {
  const home = useTempHome();

  it("lives at ~/.anona/credentials.json, the Python CLI's path", () => {
    expect(credentialsPath()).toBe(`${home.dir()}/.anona/credentials.json`);
  });
  it("round-trips, with the Python file's key names", () => {
    saveCredentials(C);
    expect(loadCredentials()).toEqual(C);
    expect(Object.keys(JSON.parse(readFileSync(credentialsPath(), "utf8")))).toEqual([
      "access_token", "refresh_token", "expires_at", "client_id", "base_url",
    ]);
  });
  it("writes the file 0600 and the directory 0700, even under a lax umask", () => {
    const prev = process.umask(0);
    try {
      saveCredentials(C);
    } finally {
      process.umask(prev);
    }
    expect(statSync(credentialsPath()).mode & 0o777).toBe(0o600);
    expect(statSync(dirname(credentialsPath())).mode & 0o777).toBe(0o700);
  });
  it("tightens a pre-existing directory that was too open", () => {
    mkdirSync(dirname(credentialsPath()), { recursive: true });
    chmodSync(dirname(credentialsPath()), 0o755);
    saveCredentials(C);
    expect(statSync(dirname(credentialsPath())).mode & 0o777).toBe(0o700);
  });
  it("leaves no temp file behind, and no partial file when the rename fails", () => {
    saveCredentials(C);
    expect(readdirSync(dirname(credentialsPath()))).toEqual(["credentials.json"]);
    // Make the destination a directory so the rename fails.
    clearCredentials();
    mkdirSync(credentialsPath());
    expect(() => saveCredentials(C)).toThrow();
    expect(readdirSync(dirname(credentialsPath()))).toEqual(["credentials.json"]);
  });
  it("reads a missing, corrupt or incomplete file as logged out", () => {
    expect(loadCredentials()).toBeNull();
    mkdirSync(dirname(credentialsPath()), { recursive: true });
    for (const bad of ["{not json", "null", "[]", JSON.stringify({ access_token: "a" }),
      JSON.stringify({ access_token: "a", refresh_token: "r", client_id: "c", expires_at: "soon" })]) {
      writeFileSync(credentialsPath(), bad);
      expect(loadCredentials(), bad).toBeNull();
    }
  });
  it("defaults base_url when absent, and accepts a numeric-string expiry as Python's float() does", () => {
    mkdirSync(dirname(credentialsPath()), { recursive: true });
    writeFileSync(credentialsPath(), JSON.stringify({ access_token: "a", refresh_token: "r", client_id: "c", expires_at: "12.5" }));
    expect(loadCredentials()).toMatchObject({ baseUrl: "https://memory.anonalabs.com", expiresAt: 12.5 });
  });
  it("rejects a null or boolean expiry", () => {
    mkdirSync(dirname(credentialsPath()), { recursive: true });
    for (const v of [null, true]) {
      writeFileSync(credentialsPath(), JSON.stringify({ access_token: "a", refresh_token: "r", client_id: "c", expires_at: v }));
      expect(loadCredentials(), String(v)).toBeNull();
    }
  });
  it("clear deletes, and is quiet when there is nothing", () => {
    saveCredentials(C);
    clearCredentials();
    expect(existsSync(credentialsPath())).toBe(false);
    expect(() => clearCredentials()).not.toThrow();
  });
});
