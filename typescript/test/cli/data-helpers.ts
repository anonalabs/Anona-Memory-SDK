/**
 * A real local HTTP server for the data commands, and a way to run `anona`
 * against it. Every case asserts on the request that actually left the
 * process -- method, path, query, body, bearer -- or that nothing arrived.
 */
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { chmodSync, mkdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { PassThrough } from "node:stream";
import { afterEach, beforeEach, expect } from "vitest";
import { main } from "../../src/cli/index.js";
import { useTempHome } from "./helpers.js";

export interface Seen {
  method: string;
  path: string;
  query: Record<string, string>;
  body: any;
  auth: string | undefined;
  /** The raw request body, for multipart. */
  raw: Buffer;
  contentType: string;
}

export type Reply = { status: number; body?: unknown; headers?: Record<string, string>; raw?: string; hang?: boolean };
export type Responder = (seen: Seen) => Reply;

export interface DataServer {
  base: string;
  seen: Seen[];
  respond: Responder;
  close(): Promise<void>;
}

export async function startDataServer(respond: Responder): Promise<DataServer> {
  const state: DataServer = { base: "", seen: [], respond, close: async () => {} };
  const server: Server = createServer((req: IncomingMessage, res: ServerResponse) => {
    const chunks: Buffer[] = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const url = new URL(req.url ?? "/", "http://x");
      const rawBuf = Buffer.concat(chunks);
      const raw = rawBuf.toString();
      const ctype = req.headers["content-type"] ?? "";
      const seen: Seen = {
        method: req.method ?? "",
        path: url.pathname,
        query: Object.fromEntries(url.searchParams),
        // JSON for the API; the OAuth token endpoint takes a form.
        body: !raw ? null : ctype.includes("json") ? JSON.parse(raw) : ctype.includes("multipart") ? null : Object.fromEntries(new URLSearchParams(raw)),
        auth: req.headers.authorization,
        raw: rawBuf,
        contentType: ctype,
      };
      state.seen.push(seen);
      const reply = state.respond(seen);
      // Never answer: what a server that has gone quiet looks like.
      if (reply.hang) return;
      if (reply.body === undefined && reply.raw === undefined) {
        res.writeHead(reply.status, reply.headers ?? {});
        res.end();
        return;
      }
      const data = reply.raw ?? JSON.stringify(reply.body);
      res.writeHead(reply.status, { "content-type": reply.raw ? "text/plain" : "application/json", ...(reply.headers ?? {}) });
      res.end(data);
    });
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  state.base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  state.close = () =>
    new Promise<void>((r) => {
      server.close(() => r());
      server.closeAllConnections();
    });
  return state;
}

export const SECRET = "anona_live_SECRETSECRET";

/** Run `anona` in-process. No secret may appear on either stream, whatever happened. */
export async function run(argv: string[], stdin = ""): Promise<{ rc: number; out: string; err: string }> {
  const o: string[] = [];
  const e: string[] = [];
  const input = new PassThrough();
  input.end(stdin);
  // --profile lands in the environment, as in the pip package; one run must
  // not leave it set for the next.
  const profile = process.env.ANONA_PROFILE;
  const rc = await main(argv, {
    stdin: input,
    stdout: { write: (s: string) => void o.push(s) },
    stderr: { write: (s: string) => void e.push(s) },
  });
  if (profile === undefined) delete process.env.ANONA_PROFILE;
  else process.env.ANONA_PROFILE = profile;
  const out = o.join("");
  const err = e.join("");
  expect(out + err, `a credential reached the output of ${JSON.stringify(argv)}`).not.toContain("SECRETSECRET");
  return { rc, out, err };
}

/** A temp $HOME, and no ANONA_* variable leaking in from the environment running the tests. */
export function useDataEnv(): { dir: () => string } {
  const home = useTempHome();
  const names = ["ANONA_BASE_URL", "ANONA_API_KEY", "ANONA_SPACE_ID", "ANONA_PROFILE"];
  let saved: Record<string, string | undefined> = {};
  beforeEach(() => {
    saved = Object.fromEntries(names.map((n) => [n, process.env[n]]));
    for (const n of names) delete process.env[n];
  });
  afterEach(() => {
    for (const n of names) {
      if (saved[n] === undefined) delete process.env[n];
      else process.env[n] = saved[n];
    }
  });
  return home;
}

function anonaDir(profile?: string): string {
  const d = profile ? join(homedir(), ".anona", "profiles", profile) : join(homedir(), ".anona");
  mkdirSync(d, { recursive: true, mode: 0o700 });
  return d;
}

/** The MCP credential file, as `anona start` / `anona login --mcp` leave it. */
export function writeMcpFile(o: { base: string; signedIn?: boolean; tempHours?: number | null; profile?: string }): void {
  const doc: Record<string, unknown> = {
    access_token: o.signedIn ? "oauth_MCP_ONLY" : "",
    refresh_token: o.signedIn ? "r" : "",
    expires_at: o.signedIn ? Date.now() / 1000 + 3600 : 0,
    client_id: o.signedIn ? "c" : "",
    base_url: o.base,
  };
  if (o.tempHours !== null && o.tempHours !== undefined) {
    doc.temp_token = "anona_tmp_TEMPTOKEN";
    doc.temp_expires_at = Date.now() / 1000 + o.tempHours * 3600;
  }
  writeFileSync(join(anonaDir(o.profile), "credentials.json"), JSON.stringify(doc));
}

/** The command-line login file, as `anona login --cli` leaves it. */
export function writeCliFile(o: { base: string; access?: string; expiresIn?: number; profile?: string }): void {
  writeFileSync(
    join(anonaDir(o.profile), "cli-credentials.json"),
    JSON.stringify({
      access_token: o.access ?? "cli_ACCESS",
      refresh_token: "cli_REFRESH",
      expires_at: Date.now() / 1000 + (o.expiresIn ?? 3600),
      client_id: "cli-client",
      base_url: o.base,
    }),
  );
}

export function writeConfigEnv(text: string, mode = 0o600, profile?: string): void {
  const p = join(anonaDir(profile), "config.env");
  writeFileSync(p, text);
  chmodSync(p, mode);
}

/** stdout is exactly one ASCII-only JSON document. */
export function oneJson(out: string): any {
  expect(out.endsWith("\n") && out.split("\n").length === 2, JSON.stringify(out)).toBe(true);
  expect([...out].every((c) => c.charCodeAt(0) < 128), JSON.stringify(out)).toBe(true);
  return JSON.parse(out);
}
