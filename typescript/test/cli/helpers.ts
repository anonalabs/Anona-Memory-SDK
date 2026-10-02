import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PassThrough, Writable } from "node:stream";
import { afterEach, beforeEach, expect, vi } from "vitest";
import { saveCredentials } from "../../src/cli/store.js";
import { runProxy } from "../../src/cli/proxy.js";

/** A throwaway $HOME per test, so nothing here can touch a real credential. */
export function useTempHome(): { dir: () => string } {
  let dir = "";
  let prev: string | undefined;
  beforeEach(() => {
    prev = process.env.HOME;
    dir = mkdtempSync(join(tmpdir(), "anona-cli-"));
    process.env.HOME = dir;
  });
  afterEach(() => {
    process.env.HOME = prev;
    rmSync(dir, { recursive: true, force: true });
  });
  return { dir: () => dir };
}

export interface Hit {
  method: string;
  path: string;
  headers: IncomingMessage["headers"];
  body: string;
}

type Handler = (req: IncomingMessage, res: ServerResponse, hit: Hit) => void;

export interface FakeAs {
  base: string;
  hits: Hit[];
  count(path: string): number;
  /** Replace the behaviour of one path. */
  on(path: string, h: Handler): void;
  meta: Record<string, unknown>;
  close(): Promise<void>;
}

export function json(res: ServerResponse, status: number, body: unknown) {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
}

/** A real HTTP server playing authorization server + MCP endpoint. */
export async function startAs(): Promise<FakeAs> {
  const hits: Hit[] = [];
  const handlers = new Map<string, Handler>();
  const server: Server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const path = new URL(req.url ?? "/", "http://x").pathname;
      const hit: Hit = { method: req.method ?? "", path, headers: req.headers, body: Buffer.concat(chunks).toString() };
      hits.push(hit);
      const h = handlers.get(path);
      if (h) return h(req, res, hit);
      json(res, 404, {});
    });
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  const meta: Record<string, unknown> = {
    issuer: base,
    authorization_endpoint: base + "/oauth/authorize",
    token_endpoint: base + "/oauth/token",
    registration_endpoint: base + "/oauth/register",
    code_challenge_methods_supported: ["S256"],
  };
  handlers.set("/.well-known/oauth-authorization-server", (_q, res) => json(res, 200, meta));
  return {
    base,
    hits,
    meta,
    count: (p) => hits.filter((h) => h.path === p).length,
    on: (p, h) => void handlers.set(p, h),
    close: () =>
      new Promise<void>((r) => {
        server.close(() => r());
        server.closeAllConnections();
      }),
  };
}

export function tokenOk(access: string, refresh: string, expiresIn = 3600): Handler {
  return (_q, res) => json(res, 200, { access_token: access, refresh_token: refresh, expires_in: expiresIn, token_type: "Bearer" });
}

export function seed(base: string, o: { offset?: number; access?: string; refresh?: string } = {}) {
  saveCredentials({
    accessToken: o.access ?? "tok-1",
    refreshToken: o.refresh ?? "rt-1",
    expiresAt: Date.now() / 1000 + (o.offset ?? 3600),
    clientId: "cid-1",
    baseUrl: base,
  });
}

/**
 * Run the real proxy against real streams and enforce the stdout rule HERE, so
 * no individual test can forget it: stdout carries only JSON-RPC, and nothing
 * reaches the process's real stdout (a stray console.log or process.stdout.write
 * anywhere in the call tree) while it runs. Returns what the proxy wrote.
 */
export async function drive(base: string, lines: string[]): Promise<{ rc: number; out: any[]; err: string }> {
  const stdin = new PassThrough();
  const outChunks: string[] = [];
  const errChunks: string[] = [];
  const mk = (sink: string[]) =>
    new Writable({
      write(c, _e, cb) {
        sink.push(c.toString());
        cb();
      },
    });
  const stray: string[] = [];
  const spies = [
    vi.spyOn(process.stdout, "write").mockImplementation(((c: unknown) => (stray.push(String(c)), true)) as never),
    ...(["log", "info", "debug"] as const).map((m) =>
      vi.spyOn(console, m).mockImplementation(((...a: unknown[]) => void stray.push(a.join(" "))) as never),
    ),
  ];
  let rc: number;
  try {
    stdin.end(lines.map((l) => l + "\n").join(""));
    rc = await runProxy(base, stdin, mk(outChunks), mk(errChunks));
  } finally {
    spies.forEach((s) => s.mockRestore());
  }
  expect(stray, "something wrote to the real stdout while the proxy ran").toEqual([]);
  const text = outChunks.join("");
  const out = text
    .split("\n")
    .filter((l) => l !== "")
    .map((l) => {
      const m = JSON.parse(l); // throws if any non-JSON reached stdout
      const items = Array.isArray(m) ? m : [m];
      for (const i of items) expect(i.jsonrpc).toBe("2.0");
      return m;
    });
  expect(text === "" || text.endsWith("\n")).toBe(true);
  return { rc, out, err: errChunks.join("") };
}
