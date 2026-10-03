/**
 * The two implementations share ~/.anona/credentials.json. This is the test
 * that says so, by actually running both: Python writes and Node reads, Node
 * writes and Python reads, and each rotates a credential the other wrote.
 * Nothing else would notice the two drifting apart, because no other test holds
 * both in one process tree.
 */
import { execFile } from "node:child_process";
import { existsSync, readFileSync, rmSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { credentialsPath, liveTempToken, loadCredentials, saveCredentials } from "../../src/cli/store.js";
import { accessToken } from "../../src/cli/tokens.js";
import { startAs, tokenOk, useTempHome, type FakeAs } from "./helpers.js";

const run = promisify(execFile);
const here = dirname(fileURLToPath(import.meta.url));
// Monorepo: anona-sdk/python/anona. Public mirror: the repo root is the package.
const pyRoot = [resolve(here, "../../../python"), resolve(here, "../../..")].find((p) => existsSync(resolve(p, "anona/cli/store.py")));

// HOME is a temp dir in these tests, which would hide Python's user site-packages.
const realUserBase = process.env.PYTHONUSERBASE ?? `${process.env.HOME}/.local`;

async function py(code: string, home: string): Promise<string> {
  const { stdout } = await run("python3", ["-c", code], {
    env: { ...process.env, HOME: home, PYTHONUSERBASE: realUserBase, PYTHONPATH: pyRoot ?? "" },
  });
  return stdout;
}

let pythonOk = false;
try {
  if (pyRoot) {
    await run("python3", ["-c", "import anona.cli.tokens, httpx"], { env: { ...process.env, PYTHONPATH: pyRoot } });
    pythonOk = true;
  }
} catch {
  /* reported below */
}
// On a developer machine without the Python deps, skip. In CI a skip is a lie.
if (!pythonOk && process.env.CI) throw new Error("CI must be able to run the cross-implementation credential test (python3 + anona + httpx).");

describe.skipIf(!pythonOk)("credential file: Python <-> Node", () => {
  const home = useTempHome();
  let as: FakeAs;
  beforeEach(async () => void (as = await startAs()));
  afterEach(() => as.close());

  const PY_SAVE = (base: string, exp = "time.time() + 3600.25") => `
import time
from anona.cli import store
store.save(store.Credentials("py-access", "py-refresh", ${exp}, "py-cid", "${base}"))`;
  const PY_LOAD = `
import json
from anona.cli import store
c = store.load()
print(json.dumps(None if c is None else [c.access_token, c.refresh_token, c.expires_at, c.client_id, c.base_url]))`;

  it("Python writes, Node reads", async () => {
    await py(PY_SAVE("https://py.test"), home.dir());
    const c = loadCredentials()!;
    expect(c).toMatchObject({ accessToken: "py-access", refreshToken: "py-refresh", clientId: "py-cid", baseUrl: "https://py.test" });
    expect(c.expiresAt).toBeGreaterThan(Date.now() / 1000);
    expect(c.expiresAt % 1).not.toBe(0); // the fractional float survives
    expect(statSync(credentialsPath()).mode & 0o777).toBe(0o600);
  });

  it("Node writes, Python reads", async () => {
    saveCredentials({ accessToken: "n-access", refreshToken: "n-refresh", expiresAt: 1900000000.5, clientId: "n-cid", baseUrl: "https://n.test" });
    const got = JSON.parse(await py(PY_LOAD, home.dir()));
    expect(got).toEqual(["n-access", "n-refresh", 1900000000.5, "n-cid", "https://n.test"]);
    expect(statSync(credentialsPath()).mode & 0o777).toBe(0o600);
  });

  it("Python reads what Node wrote even when Node wrote an integer expiry", async () => {
    saveCredentials({ accessToken: "a", refreshToken: "r", expiresAt: 1900000000, clientId: "c", baseUrl: "https://n.test" });
    expect(JSON.parse(await py(PY_LOAD, home.dir()))[2]).toBe(1900000000);
  });

  it("Node rotates a credential Python wrote, and Python then reads the rotated pair", async () => {
    as.on("/oauth/token", tokenOk("node-rotated", "node-rt"));
    await py(PY_SAVE(as.base, "time.time() - 10"), home.dir());
    expect(await accessToken()).toBe("node-rotated");
    expect(as.count("/oauth/token")).toBe(1);
    const got = JSON.parse(await py(PY_LOAD, home.dir()));
    expect(got.slice(0, 2)).toEqual(["node-rotated", "node-rt"]);
    expect(got[3]).toBe("py-cid");
  });

  it("Python rotates a credential Node wrote, and Node then reads the rotated pair", async () => {
    as.on("/oauth/token", tokenOk("py-rotated", "py-rt"));
    saveCredentials({ accessToken: "a", refreshToken: "r", expiresAt: Date.now() / 1000 - 10, clientId: "n-cid", baseUrl: as.base });
    const out = await py(
      `
import httpx
from anona.cli import tokens
with httpx.Client() as c:
    print(tokens.access_token(c))`,
      home.dir(),
    );
    expect(out.trim()).toBe("py-rotated");
    expect(loadCredentials()).toMatchObject({ accessToken: "py-rotated", refreshToken: "py-rt", clientId: "n-cid" });
    // And the two sides sent the same refresh request.
    const form = new URLSearchParams(as.hits.find((h) => h.path === "/oauth/token")!.body);
    expect(Object.fromEntries(form)).toEqual({ grant_type: "refresh_token", refresh_token: "r", client_id: "n-cid" });
  });

  it("Node's refresh request is byte-for-byte what Python's is", async () => {
    as.on("/oauth/token", tokenOk("x", "y"));
    saveCredentials({ accessToken: "a", refreshToken: "r", expiresAt: 1, clientId: "c", baseUrl: as.base });
    await py(`
import httpx
from anona.cli import tokens
with httpx.Client() as c:
    tokens.access_token(c)`, home.dir());
    saveCredentials({ accessToken: "a", refreshToken: "r", expiresAt: 1, clientId: "c", baseUrl: as.base });
    await accessToken();
    const [pyHit, nodeHit] = as.hits.filter((h) => h.path === "/oauth/token");
    expect(nodeHit!.body).toBe(pyHit!.body);
    expect(nodeHit!.headers["content-type"]).toBe(pyHit!.headers["content-type"]);
  });

  // The gateway treats a replayed refresh token as theft and revokes every
  // refresh token for the client_id, which both implementations share, so two
  // rotations in one instant log BOTH sides out. One must refresh, the other
  // must wait and use its result. The token endpoint stalls so the two are
  // provably in flight together.
  const PY_REFRESH = `
import httpx
from anona.cli import tokens
with httpx.Client() as c:
    print(tokens.access_token(c))`;
  const stall = (ms: number) => (...a: Parameters<ReturnType<typeof tokenOk>>) => void setTimeout(() => tokenOk("rotated", "rt-new")(...a), ms);

  it("a Python and a Node refresh at the same instant rotate ONCE (Node first)", async () => {
    as.on("/oauth/token", stall(1500));
    saveCredentials({ accessToken: "a", refreshToken: "r", expiresAt: Date.now() / 1000 - 10, clientId: "cid", baseUrl: as.base });
    const node = accessToken();
    await new Promise((r) => setTimeout(r, 100));
    const [n, p] = await Promise.all([node, py(PY_REFRESH, home.dir())]);
    expect(n).toBe("rotated");
    expect(p.trim()).toBe("rotated");
    expect(as.count("/oauth/token")).toBe(1);
  });

  it("a Python and a Node refresh at the same instant rotate ONCE (Python first)", async () => {
    as.on("/oauth/token", stall(1500));
    saveCredentials({ accessToken: "a", refreshToken: "r", expiresAt: Date.now() / 1000 - 10, clientId: "cid", baseUrl: as.base });
    const python = py(PY_REFRESH, home.dir());
    // Wait until Python's request is in flight, i.e. it holds the lock.
    for (let i = 0; i < 200 && as.count("/oauth/token") === 0; i++) await new Promise((r) => setTimeout(r, 25));
    expect(as.count("/oauth/token")).toBe(1);
    const [n, p] = await Promise.all([accessToken(), python]);
    expect(n).toBe("rotated");
    expect(p.trim()).toBe("rotated");
    expect(as.count("/oauth/token")).toBe(1);
  });
});

// The temp-profile fields travel in the same file. Same shape as above: each
// direction, and each side surviving the other's refresh.
describe.skipIf(!pythonOk)("credential file, temp profile fields: Python <-> Node", () => {
  const home = useTempHome();
  let as: FakeAs;
  beforeEach(async () => void (as = await startAs()));
  afterEach(() => as.close());

  const TOK = "anona_tmp_CROSSIMPL";
  const PY_REFRESH = `
import httpx
from anona.cli import tokens
with httpx.Client() as c:
    print(tokens.access_token(c))`;
  const LOAD = `
import json
from anona.cli import store
c = store.load()
print(json.dumps(None if c is None else [c.access_token, c.temp_token, c.temp_expires_at, c.live_temp_token()]))`;

  it("Python writes a temp-only file, Node reads the token and the fractional deadline", async () => {
    await py(
      `
import time
from anona.cli import store
store.save(store.Credentials("", "", 0.0, "", "https://py.test", temp_token="${TOK}", temp_expires_at=time.time() + 7200.5))`,
      home.dir(),
    );
    const c = loadCredentials()!;
    expect(c).toMatchObject({ accessToken: "", tempToken: TOK, baseUrl: "https://py.test" });
    expect(c.tempExpiresAt! % 1).not.toBe(0);
    expect(liveTempToken(c)).toBe(TOK);
  });

  it("Node writes a temp-only file, Python reads the token and the deadline and calls it live", async () => {
    const at = Date.now() / 1000 + 7200.5;
    saveCredentials({ accessToken: "", refreshToken: "", expiresAt: 0, clientId: "", baseUrl: "https://n.test", tempToken: TOK, tempExpiresAt: at });
    expect(JSON.parse(await py(LOAD, home.dir()))).toEqual(["", TOK, at, TOK]);
  });

  it("Node writes an expired profile, Python agrees it is not live", async () => {
    saveCredentials({ accessToken: "", refreshToken: "", expiresAt: 0, clientId: "", baseUrl: "https://n.test", tempToken: TOK, tempExpiresAt: 1000 });
    expect(JSON.parse(await py(LOAD, home.dir()))).toEqual(["", TOK, 1000, null]);
  });

  it("Node rotates a credential Python wrote and Python's temp profile survives", async () => {
    as.on("/oauth/token", tokenOk("node-rotated", "node-rt"));
    await py(
      `
import time
from anona.cli import store
store.save(store.Credentials("a", "r", time.time() - 10, "cid", "${as.base}", temp_token="${TOK}", temp_expires_at=4102444800.0))`,
      home.dir(),
    );
    expect(await accessToken()).toBe("node-rotated");
    expect(JSON.parse(await py(LOAD, home.dir()))).toEqual(["node-rotated", TOK, 4102444800, TOK]);
  });

  it("Python rotates a credential Node wrote and Node's temp profile survives", async () => {
    as.on("/oauth/token", tokenOk("py-rotated", "py-rt"));
    saveCredentials({ accessToken: "a", refreshToken: "r", expiresAt: 1, clientId: "cid", baseUrl: as.base, tempToken: TOK, tempExpiresAt: 4102444800 });
    await py(PY_REFRESH, home.dir());
    expect(loadCredentials()).toMatchObject({ accessToken: "py-rotated", tempToken: TOK, tempExpiresAt: 4102444800 });
  });

  it("the file Node writes for an ordinary login has no temp keys, and Python reads it as no profile", async () => {
    saveCredentials({ accessToken: "a", refreshToken: "r", expiresAt: 1, clientId: "c", baseUrl: "https://n.test" });
    expect(JSON.parse(await py(LOAD, home.dir()))).toEqual(["a", null, null, null]);
  });

  // The same file must read the same to a human, in either CLI.
  const PY_STATUS = `
import sys
from anona.cli import main
sys.exit(main.main(["status"]))`;
  it("`status` prints the same words in both CLIs for a temp-only file", async () => {
    saveCredentials({ accessToken: "", refreshToken: "", expiresAt: 0, clientId: "", baseUrl: "https://n.test", tempToken: TOK, tempExpiresAt: Date.now() / 1000 + 71.5 * 3600 });
    const pyOut = await py(PY_STATUS, home.dir());
    const t: string[] = [];
    const { main } = await import("../../src/cli/index.js");
    await main(["status"], { stdin: process.stdin, stdout: { write: (s) => void t.push(s) }, stderr: { write: () => {} } });
    expect(t.join("")).toBe(pyOut);
    expect(pyOut).toContain("Temporary profile for https://n.test");
    expect(pyOut).not.toContain(TOK);
  });

  it("`start` prints the same four sentences in both CLIs, and writes the same file shape", async () => {
    const expires = new Date(Date.now() + 71.5 * 3600_000).toISOString();
    as.on("/v1/temp/profiles", (_q, r) => {
      r.writeHead(201, { "content-type": "application/json" });
      r.end(JSON.stringify({ temp_token: TOK, expires_at: expires }));
    });
    const pyOut = await py(
      `
import sys
from anona.cli import main
sys.exit(main.main(["--base-url", "${as.base}", "start"]))`,
      home.dir(),
    );
    const pyFile = JSON.parse(readFileSync(credentialsPath(), "utf8"));
    rmSync(credentialsPath());
    const t: string[] = [];
    const { main } = await import("../../src/cli/index.js");
    expect(await main(["--base-url", as.base, "start"], { stdin: process.stdin, stdout: { write: (s) => void t.push(s) }, stderr: { write: () => {} } })).toBe(0);
    expect(t.join("")).toBe(pyOut);
    expect(Object.keys(JSON.parse(readFileSync(credentialsPath(), "utf8")))).toEqual(Object.keys(pyFile));
    expect(JSON.parse(readFileSync(credentialsPath(), "utf8")).temp_expires_at).toBeCloseTo(pyFile.temp_expires_at, 3);
  });
});
