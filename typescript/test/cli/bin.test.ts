/**
 * The real thing: the bundled bin, run as a child process with a real stdout.
 * In-process tests spy on process.stdout; this one cannot be fooled by a spy.
 */
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { buildSync } from "esbuild";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { json, seed, startAs, useTempHome, type FakeAs } from "./helpers.js";

const here = dirname(fileURLToPath(import.meta.url));
let dir = "";
let bin = "";

beforeAll(() => {
  dir = mkdtempSync(join(tmpdir(), "anona-bin-"));
  bin = join(dir, "cli.mjs");
  buildSync({ entryPoints: [resolve(here, "../../src/cli/bin.ts")], bundle: true, platform: "node", format: "esm", outfile: bin, logLevel: "silent" });
});
afterAll(() => rmSync(dir, { recursive: true, force: true }));

function exec(args: string[], home: string, stdin = ""): Promise<{ code: number | null; out: string; err: string }> {
  return new Promise((done) => {
    const p = spawn(process.execPath, [bin, ...args], { env: { ...process.env, HOME: home, ANONA_BASE_URL: "" }, stdio: ["pipe", "pipe", "pipe"] });
    let out = "", err = "";
    p.stdout.on("data", (d) => (out += d));
    p.stderr.on("data", (d) => (err += d));
    p.on("close", (code) => done({ code, out, err }));
    p.stdin.end(stdin);
  });
}

describe("bundled anona bin", () => {
  const home = useTempHome();
  let as: FakeAs;
  beforeEach(async () => void (as = await startAs()));
  afterEach(() => as.close());

  it("mcp: stdout is JSON-RPC and nothing else, stderr carries the diagnostics", async () => {
    seed(as.base, { access: "SECRET-TOK" });
    as.on("/mcp", (_q, r, hit) => {
      const id = JSON.parse(hit.body).id;
      return id === 2 ? json(r, 500, {}) : json(r, 200, { jsonrpc: "2.0", id, result: {} });
    });
    const input = [1, 2, 3].map((id) => JSON.stringify({ jsonrpc: "2.0", id, method: "ping" })).join("\n") + "\nnot json\n";
    const { code, out, err } = await exec(["mcp"], home.dir(), input);
    expect(code).toBe(0);
    const lines = out.split("\n").filter(Boolean).map((l) => JSON.parse(l));
    expect(lines.map((l) => l.id)).toEqual([1, 2, 3, null]);
    expect(lines.every((l) => l.jsonrpc === "2.0")).toBe(true);
    expect(err).toContain("HTTP 500");
    expect(out + err).not.toContain("SECRET-TOK");
  });

  it("status runs and exits non-zero when logged out", async () => {
    const { code, out } = await exec(["status"], home.dir());
    expect(code).toBe(1);
    expect(out).toMatch(/Not logged in/);
  });

  it("an unknown command exits 2 with usage on stderr only", async () => {
    const { code, out, err } = await exec(["wat"], home.dir());
    expect([code, out]).toEqual([2, ""]);
    expect(err).toMatch(/usage: anona/);
  });
});
