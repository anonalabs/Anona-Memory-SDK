import { PassThrough } from "node:stream";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { main } from "../../src/cli/index.js";
import { credentialsPath, loadCredentials } from "../../src/cli/store.js";
import { existsSync, rmSync, writeFileSync } from "node:fs";
import { json, seed, startAs, useTempHome, type FakeAs } from "./helpers.js";

function io(input = "") {
  const o: string[] = [], e: string[] = [];
  const stdin = new PassThrough();
  stdin.end(input);
  return { stdin, stdout: { write: (s: string) => void o.push(s) }, stderr: { write: (s: string) => void e.push(s) }, o: () => o.join(""), e: () => e.join("") };
}

describe("anona command", () => {
  useTempHome();
  let as: FakeAs;
  beforeEach(async () => {
    as = await startAs();
    as.on("/mcp", (_q, r, hit) => json(r, 200, { jsonrpc: "2.0", id: JSON.parse(hit.body).id, result: {} }));
    delete process.env.ANONA_BASE_URL;
  });
  afterEach(() => {
    delete process.env.ANONA_BASE_URL;
    return as.close();
  });
  const line = JSON.stringify({ jsonrpc: "2.0", id: 1, method: "ping" }) + "\n";

  it("status says logged out with exit 1", async () => {
    const t = io();
    expect(await main(["status"], t)).toBe(1);
    expect(t.o()).toMatch(/Not logged in/);
  });
  it("status shows the deployment and never a token or client id", async () => {
    seed(as.base, { access: "SECRET-A", refresh: "SECRET-R" });
    const t = io();
    expect(await main(["status"], t)).toBe(0);
    expect(t.o()).toContain(as.base);
    expect(t.o()).toMatch(/Credential found for .*\n.*Stored access token expires in \d+ minutes/);
    expect(t.o()).toMatch(/may still reject it/);
    expect(t.o() + t.e()).not.toMatch(/SECRET|cid-1/);
  });
  it("status explains an expired access token as normal", async () => {
    seed(as.base, { offset: -100 });
    const t = io();
    expect(await main(["status"], t)).toBe(0);
    expect(t.o()).toMatch(/Stored access token has expired; a refresh is attempted/);
    expect(t.o()).not.toMatch(/Logged in/);
  });
  it("logout deletes the credential", async () => {
    seed(as.base);
    expect(await main(["logout"], io())).toBe(0);
    expect(existsSync(credentialsPath())).toBe(false);
  });
  it("logout waits for the refresh lock, so a refresh cannot write the credential back", async () => {
    seed(as.base);
    const lock = credentialsPath() + ".nodelock";
    writeFileSync(lock, "99999");
    let done = false;
    const p = main(["logout"], io()).then((rc) => ((done = true), rc));
    await new Promise((r) => setTimeout(r, 200));
    expect(done).toBe(false);
    expect(existsSync(credentialsPath())).toBe(true);
    rmSync(lock);
    expect(await p).toBe(0);
    expect(existsSync(credentialsPath())).toBe(false);
    expect(existsSync(lock)).toBe(false);
  });
  it("accepts --base-url and --no-browser before or after the command (parity with the Python CLI)", async () => {
    // Pointing login at a dead address makes it fail at discovery, after it has
    // printed the target, which is all this needs to observe.
    for (const argv of [["--base-url", "http://127.0.0.1:1", "login"], ["login", "--base-url", "http://127.0.0.1:1"], ["login", "--base-url=http://127.0.0.1:1", "--no-browser"], ["--no-browser", "login", "--base-url", "http://127.0.0.1:1"]]) {
      const t = io();
      await main(argv, t, { timeoutMs: 1000 });
      expect(t.o(), argv.join(" ")).toContain("Signing in to http://127.0.0.1:1");
      expect(t.e()).not.toMatch(/usage:/);
    }
  });
  it("prints usage and exits 2 for nothing, an unknown command, or extras", async () => {
    for (const argv of [[], ["bogus"], ["status", "extra"], ["--base-url"]]) {
      const t = io();
      expect(await main(argv, t), argv.join(" ")).toBe(2);
      expect(t.o()).toBe("");
    }
  });
  it("mcp talks to the stored deployment when no flag is given", async () => {
    seed(as.base);
    const t = io(line);
    expect(await main(["mcp"], t)).toBe(0);
    expect(JSON.parse(t.o()).id).toBe(1);
  });
  it("--base-url beats the stored deployment, in both spellings", async () => {
    seed("http://127.0.0.1:1"); // stored deployment is dead
    for (const argv of [["--base-url", as.base, "mcp"], [`--base-url=${as.base}`, "mcp"]]) {
      as.hits.length = 0;
      const t = io(line);
      await main(argv, t);
      expect(as.count("/mcp"), argv.join(" ")).toBe(1);
    }
  });
  it("login honours ANONA_BASE_URL and --no-browser, and reports a bad deployment", async () => {
    process.env.ANONA_BASE_URL = "http://127.0.0.1:1";
    const t = io();
    expect(await main(["login", "--no-browser"], t)).toBe(1);
    expect(t.e()).toMatch(/Could not reach http:\/\/127\.0\.0\.1:1/);
    expect(loadCredentials()).toBeNull();
  });
  it("login names its target, and says when it is replacing a credential for another deployment", async () => {
    seed("https://other.test");
    const t = io();
    await main(["--base-url", "http://127.0.0.1:1", "login", "--no-browser"], t);
    expect(t.o()).toContain("Signing in to http://127.0.0.1:1\n");
    expect(t.o()).toContain("Note: the stored credential is for https://other.test; this replaces it.");
    // Same deployment (trailing slash aside): no note.
    seed("http://127.0.0.1:1");
    const u = io();
    await main(["--base-url", "http://127.0.0.1:1/", "login", "--no-browser"], u);
    expect(u.o()).not.toContain("Note:");
  });
  it("login's --base-url beats ANONA_BASE_URL", async () => {
    process.env.ANONA_BASE_URL = "http://127.0.0.1:1";
    as.on("/oauth/register", (_q, r) => json(r, 201, { client_id: "c" }));
    const t = io();
    await main(["--base-url", as.base, "login", "--no-browser"], t, { timeoutMs: 100 });
    expect(t.e()).not.toMatch(/127\.0\.0\.1:1\//);
    expect(t.o()).toContain("Open this URL");
  });
});
