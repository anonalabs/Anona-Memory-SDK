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

  // --- Asking for help is not a usage error. `--help`, `-h` and
  // `--version` fell through to the "unknown command" branch, so the npm
  // binary printed usage on stderr and exited 2 for all three, and
  // `--version` did not exist while the docs told people to check it.
  it("--help and -h print usage on stdout and exit 0", async () => {
    for (const flag of ["--help", "-h"]) {
      const t = io();
      expect(await main([flag], t)).toBe(0);
      expect(t.o()).toContain("usage: anona");
      expect(t.e()).toBe("");
    }
  });

  it("--version prints a version on stdout and exits 0", async () => {
    const t = io();
    expect(await main(["--version"], t)).toBe(0);
    expect(t.o()).toMatch(/^anona \S+\n$/);
    expect(t.e()).toBe("");
  });

  it("an unknown command is still a usage error on stderr, exit 2", async () => {
    // The fix must not turn every mistake into a success.
    const t = io();
    expect(await main(["wat"], t)).toBe(2);
    expect(t.e()).toContain("usage: anona");
    expect(t.o()).toBe("");
  });

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
    expect(t.o()).toMatch(/Credential found for .*\n.*Its access token expires in \d+ minutes and is renewed automatically/);
    expect(t.o()).toMatch(/only if `anona mcp` reports an authorization failure/);
    expect(t.o() + t.e()).not.toMatch(/SECRET|cid-1/);
  });
  it("status explains an expired access token as normal", async () => {
    seed(as.base, { offset: -100 });
    const t = io();
    expect(await main(["status"], t)).toBe(0);
    expect(t.o()).toMatch(/Its access token has expired; `anona mcp` renews it/);
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
  it("orients a bare `anona` on stdout and exits 0, touching nothing", async () => {
    const t = io();
    expect(await main([], t)).toBe(0);
    expect(t.o()).toContain("anona guide");
    expect(t.o()).toContain("anona start");
    expect(t.e()).toBe("");
  });
  it("prints usage and exits 2 for a missing command, an unknown one, or extras", async () => {
    for (const argv of [["--quiet"], ["bogus"], ["status", "extra"], ["--base-url"]]) {
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
  it("mcp refuses a --base-url the credential was not issued for, in both spellings", async () => {
    seed("https://memory.anonalabs.com");
    for (const argv of [["--base-url", as.base, "mcp"], [`--base-url=${as.base}`, "mcp"]]) {
      const t = io(line);
      expect(await main(argv, t), argv.join(" ")).toBe(1);
      expect(t.e()).toContain(`the stored credential is for https://memory.anonalabs.com, not ${as.base}`);
      expect(t.e()).toContain(`anona login --base-url ${as.base}`);
      expect(t.o()).toBe("");
    }
    // The point: the token never reached the named host.
    expect(as.count("/mcp")).toBe(0);
  });
  it("mcp refuses ANONA_BASE_URL naming another deployment, like the flag", async () => {
    seed("https://memory.anonalabs.com");
    process.env.ANONA_BASE_URL = as.base;
    const t = io(line);
    expect(await main(["mcp"], t)).toBe(1);
    expect(t.e()).toContain("the stored credential is for https://memory.anonalabs.com");
    expect(as.count("/mcp")).toBe(0);
  });
  it("naming the credential's own deployment is not a switch", async () => {
    seed(as.base);
    process.env.ANONA_BASE_URL = as.base;
    const t = io(line);
    expect(await main(["--base-url", as.base + "/", "mcp"], t)).toBe(0);
    expect(as.count("/mcp")).toBe(1);
  });
  it("refuses plain http to a remote host for mcp, start and login", async () => {
    seed("http://memory.anonalabs.com");
    let t = io(line);
    expect(await main(["mcp"], t)).toBe(1);
    expect(t.e()).toContain("must be https://");
    rmSync(credentialsPath());
    for (const argv of [["start"], ["login", "--no-browser"]]) {
      t = io();
      expect(await main([...argv, "--base-url", "http://memory.anonalabs.com"], t), argv.join(" ")).toBe(1);
      expect(t.e()).toContain("must be https://");
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
    await main(["--base-url", "http://127.0.0.1:1", "login", "--mcp", "--no-browser"], t);
    expect(t.o()).toContain("Signing in to http://127.0.0.1:1\n");
    expect(t.o()).toContain("Note: the stored credential is for https://other.test; this replaces it.");
    // Same deployment (trailing slash aside): no note.
    seed("http://127.0.0.1:1");
    const u = io();
    await main(["--base-url", "http://127.0.0.1:1/", "login", "--mcp", "--no-browser"], u);
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
