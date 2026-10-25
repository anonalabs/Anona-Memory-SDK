/**
 * Which credential a data command uses, where it may be sent, and what
 * `anona status` says about it. Precedence is `--api-key` > ANONA_API_KEY >
 * ~/.anona/config.env > the command-line login > a temporary profile; the MCP
 * login is never a data credential. Every case asserts on what reached a real
 * local server -- which bearer, which space, or that nothing arrived.
 */
import { existsSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { forApiKey, TargetError } from "../../src/cli/target.js";
import { parseConfigEnv } from "../../src/cli/config-env.js";
import {
  run,
  SECRET,
  startDataServer,
  useDataEnv,
  writeCliFile,
  writeConfigEnv,
  writeMcpFile,
  type DataServer,
  type Reply,
  type Seen,
} from "./data-helpers.js";

const MODE = { status: 200 };

function respond(s: Seen): Reply {
  if (s.path === "/.well-known/oauth-authorization-server") {
    return {
      status: 200,
      body: {
        issuer: BASE.url,
        authorization_endpoint: BASE.url + "/oauth/authorize",
        token_endpoint: BASE.url + "/oauth/token",
        registration_endpoint: BASE.url + "/oauth/register",
        code_challenge_methods_supported: ["S256"],
      },
    };
  }
  if (s.path === "/oauth/token") return TOKEN.reply;
  const st = MODE.status;
  if (st === 401) return { status: 401, body: { error: { code: "invalid_api_key", message: "Invalid API key." } } };
  if (st >= 400) return { status: st, body: { error: { code: "internal_error", message: "Something broke." } }, headers: { "retry-after": "0" } };
  if (s.path.endsWith("reason")) {
    return { status: 200, body: { job_id: null, status: null, insights: "An answer.", usage: null, sources: { models: [], memories: 0, notes: 0 }, rules_applied: [], model: null } };
  }
  if (s.path.endsWith("retrieve")) {
    return { status: 200, body: { results: [{ memory_id: "m1", content: "the user prefers pnpm" }], usage: null, context: null, token_estimate: null, receipt_id: null } };
  }
  if (s.path === "/v1/record") {
    return { status: 201, body: { memory_id: "m1", memory_ids: ["m1"], job_id: null, status: "stored", message: null, usage: null, routed_to: null } };
  }
  return { status: 200, body: {} };
}

const BASE = { url: "" };
const TOKEN: { reply: Reply } = { reply: { status: 400, body: { error: "invalid_grant" } } };

describe("data command credentials", () => {
  useDataEnv();
  let srv: DataServer;
  beforeEach(async () => {
    MODE.status = 200;
    TOKEN.reply = { status: 400, body: { error: "invalid_grant" } };
    srv = await startDataServer(respond);
    BASE.url = srv.base;
  });
  afterEach(() => srv.close());
  const auths = () => srv.seen.filter((s) => s.path.startsWith("/v1/")).map((s) => s.auth);
  const data = () => srv.seen.filter((s) => s.path.startsWith("/v1/"));

  it("flag beats env beats config.env beats the temporary profile", async () => {
    writeMcpFile({ base: srv.base, tempHours: 1 });
    writeConfigEnv(`ANONA_API_KEY='anona_live_FROMFILE'\nANONA_BASE_URL='${srv.base}'\n`);
    expect((await run(["record", "x", "--space", "s"])).rc).toBe(0);
    expect(auths().at(-1)).toBe("Bearer anona_live_FROMFILE");
    process.env.ANONA_API_KEY = "anona_live_FROMENV";
    process.env.ANONA_BASE_URL = srv.base;
    expect((await run(["retrieve", "x", "--space", "s"])).rc).toBe(0);
    expect(auths().at(-1)).toBe("Bearer anona_live_FROMENV");
    expect((await run(["reason", "x", "--space", "s", "--api-key", "anona_live_FROMFLAG", "--base-url", srv.base])).rc).toBe(0);
    expect(auths().at(-1)).toBe("Bearer anona_live_FROMFLAG");
    delete process.env.ANONA_API_KEY;
    writeConfigEnv("");
    const r = await run(["record", "x"]);
    expect(r.rc, r.err).toBe(0);
    expect(auths().at(-1)).toBe("Bearer anona_tmp_TEMPTOKEN");
    expect(data().at(-1)?.body.space_id).toBe("default");
  });

  it("an empty ANONA_API_KEY is absent, not a credential", async () => {
    writeMcpFile({ base: srv.base, tempHours: 1 });
    process.env.ANONA_API_KEY = "";
    expect((await run(["record", "x"])).rc).toBe(0);
    expect(auths()).toEqual(["Bearer anona_tmp_TEMPTOKEN"]);
  });

  it("an API key beats an MCP login, whose token is never sent", async () => {
    writeMcpFile({ base: srv.base, signedIn: true, tempHours: 1 });
    process.env.ANONA_API_KEY = "anona_live_K";
    process.env.ANONA_BASE_URL = srv.base;
    for (const v of ["record", "retrieve", "reason"]) expect((await run([v, "x", "--space", "work"])).rc).toBe(0);
    expect(auths()).toEqual(Array(3).fill("Bearer anona_live_K"));
    expect(data().every((s) => s.body.space_id === "work")).toBe(true);
  });

  it("an MCP login alone is refused, naming every key route", async () => {
    writeMcpFile({ base: srv.base, signedIn: true });
    for (const v of ["record", "retrieve", "reason"]) {
      const r = await run([v, "x"]);
      expect(r.rc).toBe(4);
      for (const needle of ["ANONA_API_KEY", "--api-key", "config.env", "anona start", "anona mcp", "anona login --cli"]) {
        expect(r.err).toContain(needle);
      }
    }
    expect(srv.seen).toEqual([]);
  });

  it("a 401 on a key is an auth exit", async () => {
    MODE.status = 401;
    const r = await run(["record", "x", "--space", "s", "--api-key", "anona_live_K", "--base-url", srv.base]);
    expect(r.rc).toBe(4);
    expect(r.err).toContain("invalid_api_key");
  });

  it("config.env is parsed, never executed", async () => {
    const marker = join(process.env.HOME as string, "pwned");
    writeConfigEnv(
      "# written by hand\nexport OTHER=1\n" +
        `$(touch ${marker})\n\`touch ${marker}\`\n` +
        `ANONA_API_KEY='anona_live_K'\nANONA_SPACE_ID='$(touch ${marker})'\nANONA_BASE_URL='${srv.base}'\nANONA_SOMETHING_ELSE='ignored'\n`,
    );
    expect((await run(["record", "x"])).rc).toBe(0);
    expect(existsSync(marker)).toBe(false);
    expect(data().at(-1)?.auth).toBe("Bearer anona_live_K");
    expect(data().at(-1)?.body.space_id).toBe(`$(touch ${marker})`);
  });

  it("config.env: unrelated names ignored, first assignment wins, installer quoting undone", async () => {
    expect(
      parseConfigEnv(
        "ANONA_API_KEY_OLD='x'\nOTHER_ANONA_API_KEY=x\nexport ANONA_API_KEY=anona_live_RIGHT\nANONA_API_KEY=second\r\n" +
          "ANONA_SPACE_ID='it'\\''s a $(space)'\nANONA_BASE_URL=\n",
      ),
    ).toEqual({ ANONA_API_KEY: "anona_live_RIGHT", ANONA_SPACE_ID: "it's a $(space)" });
  });

  it("a config.env others can read is warned about, without the value", async () => {
    writeConfigEnv(`ANONA_API_KEY='${SECRET}'\nANONA_BASE_URL='${srv.base}'\n`, 0o644);
    let r = await run(["record", "x", "--space", "s"]);
    expect(r.rc).toBe(0);
    expect(r.err).toContain("config.env");
    expect(r.err).toContain("chmod 600");
    writeConfigEnv(`ANONA_API_KEY='${SECRET}'\nANONA_BASE_URL='${srv.base}'\n`, 0o600);
    r = await run(["record", "x", "--space", "s"]);
    expect(r.err).not.toContain("chmod");
  });

  it("an API key without a space is a usage error that sends nothing", async () => {
    writeMcpFile({ base: srv.base, tempHours: 1 });
    for (const v of ["record", "retrieve", "reason"]) {
      const r = await run([v, "x", "--api-key", "anona_live_K", "--base-url", srv.base]);
      expect(r.rc).toBe(2);
      expect(r.err).toContain("--space");
      expect(r.err).toContain("ANONA_SPACE_ID");
    }
    expect(srv.seen).toEqual([]);
  });

  it("ANONA_SPACE_ID from config.env, then the environment, then the flag", async () => {
    writeConfigEnv(`ANONA_API_KEY='anona_live_K'\nANONA_SPACE_ID='fromfile'\nANONA_BASE_URL='${srv.base}'\n`);
    await run(["record", "x"]);
    expect(data().at(-1)?.body.space_id).toBe("fromfile");
    process.env.ANONA_SPACE_ID = "fromenv";
    await run(["record", "x"]);
    expect(data().at(-1)?.body.space_id).toBe("fromenv");
    await run(["record", "x", "--space", "fromflag"]);
    expect(data().at(-1)?.body.space_id).toBe("fromflag");
  });

  it("a key only goes to a foreign host named in the same place as the key", async () => {
    process.env.ANONA_API_KEY = "anona_live_K";
    for (const v of ["record", "retrieve", "reason"]) {
      const r = await run([v, "x", "--space", "s", "--base-url", srv.base]);
      expect(r.rc).toBe(4);
      expect(r.err).toContain("--api-key");
    }
    delete process.env.ANONA_API_KEY;
    writeConfigEnv("ANONA_API_KEY='anona_live_K'\nANONA_SPACE_ID='s'\n");
    process.env.ANONA_BASE_URL = srv.base;
    expect((await run(["record", "x"])).rc).toBe(4);
    writeConfigEnv(`ANONA_BASE_URL='${srv.base}'\n`);
    delete process.env.ANONA_BASE_URL;
    process.env.ANONA_API_KEY = "anona_live_K";
    expect((await run(["record", "x", "--space", "s"])).rc).toBe(4);
    delete process.env.ANONA_API_KEY;
    process.env.ANONA_BASE_URL = srv.base;
    const r = await run(["record", "x", "--space", "s", "--api-key", "anona_live_K"]);
    expect(r.rc).toBe(4);
    expect(r.err).toContain("--base-url");
    expect(srv.seen).toEqual([]);
    delete process.env.ANONA_BASE_URL;
    process.env.ANONA_API_KEY = "anona_live_ENV";
    expect((await run(["record", "x", "--space", "s", "--api-key", "anona_live_K", "--base-url", srv.base])).rc).toBe(0);
    expect(auths()).toEqual(["Bearer anona_live_K"]);
  });

  it("plain http to a remote host is refused even with a flag key", async () => {
    let r = await run(["record", "x", "--space", "s", "--api-key", "anona_live_K", "--base-url", "http://memory.anonalabs.com"]);
    expect(r.rc).toBe(4);
    expect(r.err).toContain("https://");
    process.env.ANONA_API_KEY = "anona_live_K";
    process.env.ANONA_BASE_URL = "http://10.0.0.5:3001";
    r = await run(["record", "x", "--space", "s"]);
    expect(r.rc).toBe(4);
    expect(r.err).toContain("https://");
  });

  it("only the Anona hosts take a key from elsewhere", () => {
    expect(forApiKey(undefined, undefined, "env")).toBe("https://api.anonalabs.com");
    for (const url of ["https://api.anonalabs.com", "https://memory.anonalabs.com/", "https://API.anonalabs.com", "https://api.anonalabs.com:443"]) {
      expect(forApiKey(url, "flag", "env")).toBe(url);
      expect(forApiKey(url, "env", "config.env")).toBe(url);
    }
    for (const url of [
      "https://api.anonalabs.com.evil.example",
      "https://evil.example",
      "https://api.anonalabs.com:8443",
      "https://x@api.anonalabs.com",
      "https://@api.anonalabs.com",
      "http://api.anonalabs.com",
    ]) {
      for (const source of ["env", "config.env"] as const) expect(() => forApiKey(url, "flag", source), url).toThrow(TargetError);
    }
    expect(forApiKey("https://self.example", "env", "env")).toBe("https://self.example");
    expect(forApiKey("https://self.example", "config.env", "config.env")).toBe("https://self.example");
    expect(forApiKey("https://self.example", "flag", "flag")).toBe("https://self.example");
  });

  it("status names the key's source and only its prefix", async () => {
    const key = "anona_live_abc1" + "Z".repeat(40);
    process.env.ANONA_API_KEY = key;
    let r = await run(["status"]);
    expect(r.rc).toBe(0);
    expect(r.out).toContain("API key for record/retrieve/reason: anona_live_abc1… (from ANONA_API_KEY).");
    expect(r.out).toContain("It is sent to https://api.anonalabs.com.");
    expect(r.out).toContain("Space: none set; pass --space or set ANONA_SPACE_ID.");
    expect(r.out + r.err).not.toContain("ZZZZ");
    delete process.env.ANONA_API_KEY;
    writeConfigEnv(`ANONA_API_KEY='${key}'\nANONA_SPACE_ID='work'\n`);
    r = await run(["status"]);
    expect(r.rc).toBe(0);
    expect(r.out).toContain("config.env");
    expect(r.out).toContain("Space: work.");
    expect(r.out + r.err).not.toContain("ZZZZ");
    r = await run(["status", "--api-key", key]);
    expect(r.rc).toBe(0);
    expect(r.out).toContain("--api-key");
    expect(r.out + r.err).not.toContain("ZZZZ");
    r = await run(["status", "--api-key", key, "--base-url", "https://evil.example"]);
    expect(r.rc).toBe(0); // same source: the flag named both
    process.env.ANONA_API_KEY = key;
    r = await run(["status", "--base-url", "https://evil.example"]);
    expect(r.rc).toBe(4);
  });

  it("status without a key is unchanged", async () => {
    let r = await run(["status"]);
    expect([r.rc, r.out.trim()]).toEqual([1, "Not logged in. Run `anona login`."]);
    writeMcpFile({ base: srv.base, tempHours: 1 });
    r = await run(["status"]);
    expect(r.rc).toBe(0);
    expect(r.out.startsWith("Temporary profile for")).toBe(true);
    expect(r.out).not.toContain("API key");
  });

  it("status with a key describes a command-line login as shadowed", async () => {
    writeCliFile({ base: srv.base });
    process.env.ANONA_API_KEY = "anona_live_K";
    const r = await run(["status"]);
    expect(r.rc).toBe(0);
    expect(r.out).toContain(`Command-line login for ${srv.base} (not used while an API key is set).`);
    expect(r.out).toContain("No `anona login` credential is stored for MCP");
  });

  // --- the command-line login ---------------------------------------------------

  it("the command-line login is used, and like a key it needs a space", async () => {
    writeCliFile({ base: srv.base });
    writeMcpFile({ base: srv.base, tempHours: 1 });
    let r = await run(["record", "x"]);
    expect(r.rc).toBe(2);
    expect(r.err).toContain("name a space");
    expect(srv.seen).toEqual([]);
    r = await run(["record", "x", "--space", "work"]);
    expect(r.rc, r.err).toBe(0);
    expect(auths()).toEqual(["Bearer cli_ACCESS"]);
    expect(data().at(-1)?.body.space_id).toBe("work");
    // An account-level verb needs none.
    expect((await run(["usage"])).rc).toBe(0);
    expect(auths().at(-1)).toBe("Bearer cli_ACCESS");
  });

  it("the command-line login is pinned to its deployment", async () => {
    writeCliFile({ base: srv.base });
    const r = await run(["record", "x", "--space", "s", "--base-url", "https://other.example"]);
    expect(r.rc).toBe(4);
    expect(r.err).toContain("other.example");
    expect(srv.seen).toEqual([]);
  });

  it("a 401 renews the command-line login once and retries once", async () => {
    writeCliFile({ base: srv.base, access: "cli_OLD" });
    TOKEN.reply = { status: 200, body: { access_token: "cli_NEW", refresh_token: "cli_R2", expires_in: 3600, token_type: "Bearer" } };
    let calls = 0;
    srv.respond = (s) => {
      if (s.path.startsWith("/v1/") && calls++ === 0) return { status: 401, body: { error: { code: "invalid_token", message: "Token rejected." } } };
      return respond(s);
    };
    const r = await run(["record", "x", "--space", "s"]);
    expect(r.rc, r.err).toBe(0);
    expect(auths()).toEqual(["Bearer cli_OLD", "Bearer cli_NEW"]);
    expect(srv.seen.filter((s) => s.path === "/oauth/token").length).toBe(1);
  });

  it("a refused renewal is an auth exit with what to do", async () => {
    writeCliFile({ base: srv.base, access: "cli_OLD" });
    srv.respond = (s) => (s.path.startsWith("/v1/") ? { status: 401, body: { error: { code: "invalid_token", message: "no" } } } : respond(s));
    const r = await run(["record", "x", "--space", "s"]);
    expect(r.rc).toBe(4);
    expect(r.err).toContain("anona login --cli");
    expect(auths()).toEqual(["Bearer cli_OLD"]);
  });

  it("an expired command-line token is refreshed before the request", async () => {
    writeCliFile({ base: srv.base, access: "cli_OLD", expiresIn: -10 });
    TOKEN.reply = { status: 200, body: { access_token: "cli_NEW", refresh_token: "cli_R2", expires_in: 3600, token_type: "Bearer" } };
    const r = await run(["record", "x", "--space", "s"]);
    expect(r.rc, r.err).toBe(0);
    expect(auths()).toEqual(["Bearer cli_NEW"]);
  });

  // --- no secret, anywhere --------------------------------------------------------

  it("the key never appears in output, through every outcome", async () => {
    const flag = ["--api-key", SECRET];
    const flags: Record<string, string[]> = {
      record: ["--tags", "a,b", "--timestamp", "2025-06-01T09:00:00Z", "--metadata", '{"k": "v"}', "--user-id", "u", "--agent-id", "a", "--session-id", "s"],
      retrieve: ["--tags", "a", "--tags-match", "all", "--as-of", "2026-01-01", "--occurred-after", "2025-01-01", "--occurred-before", "2025-12-31", "--user-id", "u"],
      reason: ["--depth", "thorough", "--user-id", "u"],
    };
    const check = async (argv: string[], want: number) => expect((await run(argv)).rc, argv.join(" ")).toBe(want);
    for (const verb of ["record", "retrieve", "reason"]) {
      for (const extra of [[], ["--json", ...(flags[verb] as string[])]]) {
        const base = ["--space", "s", "--base-url", srv.base];
        MODE.status = 200;
        await check([verb, "x", ...base, ...flag, ...extra], 0);
        MODE.status = 401;
        await check([verb, "x", ...base, ...flag, ...extra], 4);
        MODE.status = 500;
        await check([verb, "x", ...base, ...flag, ...extra], 7);
        await check([verb, "x", "--base-url", srv.base, ...flag, ...extra], 2);
        await check([verb, "", ...base, ...flag, ...extra], 2);
        process.env.ANONA_API_KEY = SECRET;
        await check([verb, "x", ...base, ...extra], 4);
        delete process.env.ANONA_API_KEY;
        await check([verb, "x", "--space", "s", "--base-url", "http://evil.example", ...flag, ...extra], 4);
        await check([verb, "x", "--space", "s", "--base-url", "http://127.0.0.1:1", ...flag, ...extra], 7);
      }
    }
    MODE.status = 200;
    const base = ["--space", "s", "--base-url", srv.base, ...flag];
    // Ids and values pasted from the wrong place come back redacted.
    await check(["memories", "delete", SECRET, ...base], 2);
    await check(["jobs", "cancel", SECRET, ...base], 2);
    await check(["memories", "delete", SECRET, "--yes", ...base], 0);
    await check(["memories", "edit", SECRET, "--text", "t", ...base], 0);
    await check(["memories", "list", "--limit", SECRET, ...base], 2);
    await check(["record", "x", "--timestamp", SECRET, ...base], 2);
    await check(["record", "x", "--metadata", SECRET, ...base], 2);
    await check(["record", "x", "--tags", SECRET + ",,", ...base], 2);
    await check(["record", "--file", SECRET, ...base], 2);
    await check(["record", "x", "--file", SECRET, ...base], 2);
    await check(["retrieve", "x", "--as-of", SECRET, ...base], 2);
    await check(["retrieve", "x", "--tags-match", SECRET, "--tags", "a", ...base], 2);
    await check(["reason", "x", "--depth", SECRET, ...base], 2);
    await check(["record", "x", SECRET, ...base], 2);
    process.env.ANONA_API_KEY = SECRET;
    await check(["status"], 0);
  });
});
