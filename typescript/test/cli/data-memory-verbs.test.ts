/**
 * `anona record`, `retrieve` and `reason`: the same behaviour as the pip
 * package's verbs, asserted on what reached a real local server. Stub bodies
 * use the gateway's own response models (AddMemoryResponse, SearchResponse,
 * InsightsResponse), every field present, so a verb that reads a field the API
 * does not send fails here.
 */
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { oneJson, run, SECRET, startDataServer, useDataEnv, writeMcpFile, type DataServer, type Reply, type Seen } from "./data-helpers.js";

const usage = { input_tokens: 10, output_tokens: 5, total_tokens: 15 };

/** AddMemoryResponse. */
function recorded(o: Partial<{ memory_id: string | null; memory_ids: string[] | null; job_id: string | null; status: string; message: string | null }> = {}) {
  return { memory_id: "m1", memory_ids: ["m1"], job_id: null, status: "stored", message: null, usage, routed_to: null, ...o };
}

/** SearchResult. */
function row(o: Record<string, unknown>) {
  return {
    memory_id: null,
    content: null,
    created_at: null,
    timestamp: null,
    context: null,
    relevance_score: null,
    memory_type: null,
    entities: [],
    user_id: null,
    agent_id: null,
    session_id: null,
    ...o,
  };
}

/** SearchResponse. */
const searched = (results: unknown[], extra: Record<string, unknown> = {}) => ({
  results,
  usage,
  context: null,
  token_estimate: null,
  receipt_id: null,
  ...extra,
});

/** InsightsResponse. */
const reasoned = (insights: unknown, extra: Record<string, unknown> = {}) => ({
  job_id: null,
  status: null,
  insights,
  usage,
  sources: { models: [], memories: 2, notes: 1 },
  rules_applied: [],
  model: "m-x",
  ...extra,
});

function defaults(s: Seen): Reply {
  if (s.path.endsWith("reason")) return { status: 200, body: reasoned("First paragraph.\n\nSecond paragraph.") };
  if (s.path.endsWith("retrieve")) return { status: 200, body: searched([row({ content: "the user prefers pnpm" })]) };
  return { status: 201, body: recorded() };
}

describe("record / retrieve / reason", () => {
  useDataEnv();
  let srv: DataServer;
  beforeEach(async () => {
    srv = await startDataServer(defaults);
  });
  afterEach(() => srv.close());
  const temp = () => writeMcpFile({ base: srv.base, tempHours: 60 });
  const KEY = ["--space", "s", "--base-url", "", "--api-key", SECRET];
  const keyed = () => ((KEY[3] = srv.base), KEY);

  it("with no credential says what to do, exits 4, sends nothing", async () => {
    const r = await run(["record", "the user prefers pnpm"]);
    expect(r.rc).toBe(4);
    expect(r.err).toContain("anona start");
    expect(srv.seen).toEqual([]);
  });

  it("every verb has --help, exit 0 on stdout", async () => {
    for (const verb of ["record", "retrieve", "reason"]) {
      const r = await run([verb, "--help"]);
      expect(r.rc).toBe(0);
      expect(r.out).toContain(`usage: anona ${verb}`);
    }
    const flat = (await run(["record", "--help"])).out.split(/\s+/).join(" ");
    expect(flat).toContain("Required with an API key or `anona login` unless ANONA_SPACE_ID is set");
    expect(flat).toContain("a temporary profile uses `default`");
  });

  it("a temporary profile records into `default` with its token", async () => {
    temp();
    const r = await run(["record", "the user prefers pnpm"]);
    expect(r.rc).toBe(0);
    const s = srv.seen.at(-1) as Seen;
    expect(s.path).toBe("/v1/record");
    expect(s.auth).toBe("Bearer anona_tmp_TEMPTOKEN");
    expect(s.body.space_id).toBe("default");
    expect(r.out).toContain("default");
  });

  it("names every id a write created", async () => {
    temp();
    srv.respond = () => ({ status: 201, body: recorded({ memory_ids: ["m1", "m2"] }) });
    const r = await run(["record", "the user prefers pnpm"]);
    expect(r.rc).toBe(0);
    expect(r.out).toBe("Stored 2 memories in space 'default': m1, m2\n");
  });

  it("a write that stored nothing exits 3 with the server's own message on stderr", async () => {
    temp();
    srv.respond = () => ({
      status: 201,
      body: recorded({
        memory_id: null,
        memory_ids: null,
        status: "nothing_extracted",
        message: "No facts could be extracted from that content, so nothing was stored in space 'default'. Add a sentence of context.",
      }),
    });
    const r = await run(["record", "Thanks!"]);
    expect(r.rc).toBe(3);
    expect(r.out).not.toContain("Stored");
    expect(r.err).toContain("No facts could be extracted");
    expect(r.err).toContain("Add a sentence of context.");
  });

  it("an older server reporting `stored` with no ids is still caught, in our words", async () => {
    temp();
    srv.respond = () => ({ status: 201, body: recorded({ memory_id: null, memory_ids: null, status: "stored" }) });
    const r = await run(["record", "Thanks!"]);
    expect(r.rc).toBe(3);
    expect(r.err).toContain("Nothing was stored in space 'default'");
    expect(r.err).toContain("sentence of context");
  });

  it("the server's message is stripped of terminal escapes", async () => {
    temp();
    srv.respond = () => ({
      status: 201,
      body: recorded({ memory_id: null, memory_ids: null, status: "nothing_extracted", message: "nothing stored\x1b[2K\x1b[1Gspoofed: verified OK" }),
    });
    const r = await run(["record", "x"]);
    expect(r.rc).toBe(3);
    expect(r.err).not.toContain("\x1b");
    expect(r.err).toContain("spoofed: verified OK");
  });

  it("a queued write is not empty: exit 0, `Queued`", async () => {
    temp();
    srv.respond = () => ({ status: 201, body: recorded({ memory_id: null, memory_ids: null, job_id: "j1", status: "processing" }) });
    const r = await run(["record", "x"]);
    expect(r.rc).toBe(0);
    expect(r.err).not.toContain("Nothing was stored");
    expect(r.out).toContain("Queued in space 'default'");
  });

  it("returned ids are stripped of terminal escapes", async () => {
    temp();
    srv.respond = () => ({ status: 201, body: recorded({ memory_ids: ["m1\x1b[2K\x1b[1Gspoofed: verified OK"] }) });
    const r = await run(["record", "x"]);
    expect(r.rc).toBe(0);
    expect(r.out).not.toContain("\x1b");
    expect(r.out).toContain("spoofed: verified OK");
  });

  it("retrieve on a temporary profile prints `-` for a row with no id or score", async () => {
    temp();
    const r = await run(["retrieve", "package manager"]);
    expect(r.rc).toBe(0);
    const s = srv.seen.at(-1) as Seen;
    expect(s.body).toEqual({ space_id: "default", query: "package manager", limit: 10, mode: "accurate" });
    expect(r.out).toContain("1. -  -  the user prefers pnpm");
  });

  it("an MCP login is refused before any request, and --space does not buy past it", async () => {
    writeMcpFile({ base: srv.base, signedIn: true });
    for (const argv of [["record", "x"], ["retrieve", "x"], ["reason", "x"], ["record", "x", "--space", "work"]]) {
      const r = await run(argv);
      expect(r.rc).toBe(4);
      expect(r.err).toContain("temporary profile");
      expect(r.err).toContain("anona mcp");
      expect(r.err).toContain("API key");
      expect(r.err).not.toContain("oauth_MCP_ONLY");
    }
    expect((await run(["record", "x", "--space", "work"])).err).not.toContain("--space");
    expect(srv.seen).toEqual([]);
  });

  it("an MCP login beside a live temp token is still refused", async () => {
    writeMcpFile({ base: srv.base, signedIn: true, tempHours: 60 });
    const r = await run(["record", "x"]);
    expect(r.rc).toBe(4);
    expect(r.err).toContain("temporary profile");
    expect(srv.seen).toEqual([]);
  });

  it("an expired temporary profile is a message and exit 4", async () => {
    writeMcpFile({ base: srv.base, tempHours: -1 });
    const r = await run(["record", "x"]);
    expect(r.rc).toBe(4);
    expect(r.err).toContain("expired");
    expect(srv.seen).toEqual([]);
  });

  it("a refused connection is `could not reach`, not an HTTP answer", async () => {
    temp();
    await srv.close();
    for (const verb of ["record", "reason", "retrieve"]) {
      const r = await run([verb, "x"]);
      expect(r.rc).toBe(7); // unavailable
      expect(r.err).toContain("could not reach");
      expect(r.err.toLowerCase()).toContain("refused");
      expect(r.err).not.toContain("HTTP 503");
      expect(r.err).not.toContain("the server answered");
    }
  });

  it("retrieve flattens every control character, one memory per line", async () => {
    temp();
    srv.respond = () => ({ status: 200, body: searched([row({ content: "benign\x1b[2K\x1b[1Gspoofed: verified OK\x9b31m\r\nline2\x07 ‮" })]) });
    const r = await run(["retrieve", "q"]);
    expect(r.rc).toBe(0);
    for (const ch of ["\x1b", "\x9b", "\r", "\x07", " ", "‮"]) expect(r.out).not.toContain(ch);
    expect(r.out).toContain("spoofed: verified OK");
    expect(r.out.split("\n").length - 1).toBe(1);
  });

  it("a non-envelope error body is sanitized", async () => {
    temp();
    srv.respond = () => ({ status: 404, raw: "no space \x1b[2Kforged" });
    const r = await run(["record", "x"]);
    expect(r.rc).toBe(5); // not found
    expect(r.err).not.toContain("\x1b");
    expect(r.err).toContain("forged");
  });

  it("reason on a temporary profile asks `default` with the temp token", async () => {
    temp();
    const r = await run(["reason", "what does the user prefer"]);
    expect(r.rc).toBe(0);
    const s = srv.seen.at(-1) as Seen;
    expect(s.path).toBe("/v1/reason");
    expect(s.auth).toBe("Bearer anona_tmp_TEMPTOKEN");
    expect(s.body).toEqual({ space_id: "default", query: "what does the user prefer" });
  });

  it("reason keeps paragraph breaks; the progress note is stderr only", async () => {
    temp();
    const r = await run(["reason", "q"]);
    expect(r.rc).toBe(0);
    expect(r.out).toBe("First paragraph.\n\nSecond paragraph.\n");
    expect(r.err).toContain("Reasoning over space 'default'");
  });

  it("reason strips escapes and carriage returns but keeps newlines and tabs", async () => {
    temp();
    srv.respond = () => ({ status: 200, body: reasoned("real answer\x1b[2K\x1b[1G\rspoofed: verified OK\x9b31m\x07 \n\n\tkept") });
    const r = await run(["reason", "q"]);
    expect(r.rc).toBe(0);
    for (const ch of ["\x1b", "\x9b", "\r", "\x07", " "]) expect(r.out).not.toContain(ch);
    expect(r.out).toContain("spoofed: verified OK");
    expect(r.out).toContain("\n\n\tkept");
  });

  it("reason with a blank answer is not an error", async () => {
    temp();
    srv.respond = () => ({ status: 200, body: reasoned("   ") });
    const r = await run(["reason", "q"]);
    expect(r.rc).toBe(0);
    expect(r.out).toBe("No answer. The space may hold nothing on that topic yet.\n");
  });

  it("an API error reports its code, the offending fields and the request id", async () => {
    temp();
    srv.respond = () => ({
      status: 422,
      body: {
        error: {
          code: "validation_error",
          message: "Request validation failed",
          errors: [{ loc: ["body", "content"], msg: "String should have at least 1 character" }],
          request_id: "req_abc123",
        },
      },
    });
    const r = await run(["record", "x"]);
    expect(r.rc).toBe(1);
    expect(r.err).toBe(
      "anona: validation_error: Request validation failed -- content: String should have at least 1 character (request_id req_abc123)\n",
    );
  });

  it("a retry says so on stderr while it waits", async () => {
    temp();
    let n = 0;
    srv.respond = () =>
      ++n <= 2
        ? { status: 429, body: { error: { code: "rate_limited", message: "slow down" } }, headers: { "retry-after": "0" } }
        : { status: 201, body: recorded() };
    const r = await run(["record", "x"]);
    expect(r.rc).toBe(0);
    expect(r.err).toBe("anona: rate limited, retrying in 0 s (1/2)\nanona: rate limited, retrying in 0 s (2/2)\n");
    expect(r.out).not.toContain("retrying");
    expect(srv.seen.length).toBe(3);
  });

  it("a 5xx on a write is never re-sent; credits_exhausted is never retried", async () => {
    temp();
    srv.respond = () => ({ status: 503, body: { error: { code: "write_status_unknown", message: "m" } }, headers: { "retry-after": "0" } });
    expect((await run(["record", "x"])).rc).toBe(7);
    expect(srv.seen.length).toBe(1);
    srv.respond = () => ({ status: 429, body: { error: { code: "credits_exhausted", message: "m" } }, headers: { "retry-after": "0" } });
    const r = await run(["record", "x"]);
    expect(r.rc).toBe(6); // out of credits
    expect(r.err).not.toContain("retrying");
    expect(srv.seen.length).toBe(2);
  });

  it("empty input is refused locally with exit 2", async () => {
    temp();
    for (const [argv, word] of [
      [["record", ""], "memory"],
      [["retrieve", "   "], "query"],
      [["reason", ""], "question"],
    ] as const) {
      const r = await run([...argv]);
      expect(r.rc).toBe(2);
      expect(r.err).toContain(`the ${word} is empty`);
    }
    expect(srv.seen).toEqual([]);
  });

  it("an answer with no envelope still reports its status", async () => {
    temp();
    srv.respond = () => ({ status: 500, raw: "<html>502 Bad Gateway</html>" });
    const r = await run(["record", "x"]);
    expect(r.rc).toBe(7);
    expect(r.err).toBe("anona: the server answered HTTP 500: <html>502 Bad Gateway</html>\n");
  });

  it("a hostile error code cannot forge output", async () => {
    temp();
    srv.respond = () => ({ status: 400, body: { error: { code: "nope\x1b[2K\x1b[1Gstored: verified OK\x9b31m\r", message: "m" } } });
    const r = await run(["record", "x"]);
    expect(r.rc).toBe(1);
    for (const ch of ["\x1b", "\x9b", "\r"]) expect(r.err).not.toContain(ch);
    expect(r.err).toContain("stored: verified OK");
  });

  // --- retrieve, readable by an agent ---------------------------------------

  const ROWS = [
    row({ memory_id: "3f2a9c1e-0000-4000-8000-000000000001", content: "the user prefers pnpm", relevance_score: 1.0978, created_at: "2026-10-19T10:00:00Z", user_id: "alice", memory_type: "world" }),
    row({ memory_id: "9b7d0e44-0000-4000-8000-000000000002", content: "builds run on Node 22", relevance_score: 0.00006, created_at: "2026-10-19T10:01:00Z", user_id: "alice", memory_type: "observation" }),
  ];

  it("retrieve text shows a 3-significant-figure score and an 8-char id", async () => {
    temp();
    srv.respond = () => ({ status: 200, body: searched(ROWS) });
    const r = await run(["retrieve", "pm"]);
    expect(r.out.split("\n")).toEqual(["1. 1.1  3f2a9c1e  the user prefers pnpm", "2. 6e-05  9b7d0e44  builds run on Node 22", ""]);
  });

  it("retrieve --json is the API's rows, plus `searched` when sent, nothing else", async () => {
    temp();
    srv.respond = () => ({ status: 200, body: searched(ROWS) });
    let r = await run(["retrieve", "pm", "--json"]);
    expect(oneJson(r.out)).toEqual({ results: ROWS });
    const where = [{ space_id: "default", confidence: 0.9, stage: "rule", reason: "r" }];
    srv.respond = () => ({ status: 200, body: searched(ROWS, { searched: where }) });
    r = await run(["retrieve", "pm", "--json"]);
    expect(oneJson(r.out).searched).toEqual(where);
    srv.respond = () => ({ status: 200, body: searched([]) });
    r = await run(["retrieve", "pm", "--json"]);
    expect(r.rc).toBe(0);
    expect(oneJson(r.out)).toEqual({ results: [] });
  });

  it("retrieve --json escapes control characters and still round-trips", async () => {
    temp();
    const content = "benign\x1b[2Kspoofed\x9b31m\r\n é \u{1f600}";
    srv.respond = () => ({ status: 200, body: searched([row({ content })]) });
    const r = await run(["retrieve", "pm", "--json"]);
    for (const ch of ["\x1b", "\x9b", "\r"]) expect(r.out).not.toContain(ch);
    expect(oneJson(r.out).results[0].content).toBe(content);
    // Python's json.dumps spelling: ", " and ": ", lowercase \u escapes.
    expect(r.out).toContain('"content": "benign\\u001b[2Kspoofed\\u009b31m\\r\\n \\u00e9 \\ud83d\\ude00"');
  });

  it("retrieve flags pass straight through; unset ones stay out", async () => {
    temp();
    await run(["retrieve", "pm", "--limit", "3", "--min-score", "0.1", "--user-id", "alice", "--agent-id", "coder", "--session-id", "s1"]);
    const b = (srv.seen.at(-1) as Seen).body;
    expect([b.limit, b.min_score, b.user_id, b.agent_id, b.session_id]).toEqual([3, 0.1, "alice", "coder", "s1"]);
    await run(["retrieve", "pm"]);
    const bare = (srv.seen.at(-1) as Seen).body;
    for (const k of ["min_score", "user_id", "agent_id", "session_id"]) expect(bare).not.toHaveProperty(k);
  });

  it("a bad limit or score is a usage error that sends nothing", async () => {
    temp();
    for (const argv of [["--limit", "0"], ["--limit", "101"], ["--limit", "x"], ["--min-score", "-1"], ["--min-score", "nan"]]) {
      const r = await run(["retrieve", "pm", ...argv]);
      expect(r.rc, argv.join(" ")).toBe(2);
      expect(r.out).toBe("");
    }
    expect(srv.seen).toEqual([]);
  });

  // --- the request flags and --json (contract) ------------------------------

  async function body(argv: string[], stdin = ""): Promise<any> {
    const n = srv.seen.length;
    const r = await run([...argv, ...keyed()], stdin);
    expect(r.rc, r.err).toBe(0);
    expect(srv.seen.length).toBe(n + 1);
    return (srv.seen.at(-1) as Seen).body;
  }

  async function refused(argv: string[], stdin = ""): Promise<string> {
    const n = srv.seen.length;
    const r = await run([...argv, ...keyed()], stdin);
    expect(r.rc, `${argv.join(" ")}: ${r.err}`).toBe(2);
    expect(srv.seen.length).toBe(n);
    expect(r.out).toBe("");
    return r.err;
  }

  it("record: a bare command sends what it always did, flags reach the body", async () => {
    expect(await body(["record", "The user prefers pnpm."])).toEqual({ space_id: "s", content: "The user prefers pnpm.", metadata: {} });
    const b = await body([
      "record", "x", "--tags", "a,b", "--tags", "c", "--timestamp", "2025-06-01T09:00:00Z",
      "--metadata", '{"source": "cli", "n": 1}', "--user-id", "u1", "--agent-id", "a1", "--session-id", "s1",
    ]);
    expect(b).toEqual({
      space_id: "s", content: "x", metadata: { source: "cli", n: 1 }, tags: ["a", "b", "c"],
      user_id: "u1", agent_id: "a1", session_id: "s1", timestamp: "2025-06-01T09:00:00Z",
    });
    expect(Object.keys(await body(["record", "x", "--timestamp", "2025-06-01"]))).toEqual(["space_id", "content", "metadata", "timestamp"]);
  });

  it("record reads stdin for `-` and a file for --file", async () => {
    expect((await body(["record", "-"], "From stdin, line one.\nLine two.\n")).content).toBe("From stdin, line one.\nLine two.\n");
    const p = join(process.env.HOME as string, "note.txt");
    writeFileSync(p, "From a file.");
    expect((await body(["record", "--file", p])).content).toBe("From a file.");
  });

  it("record usage errors send nothing", async () => {
    const home = process.env.HOME as string;
    const p = join(home, "note.txt");
    writeFileSync(p, "x");
    const e = join(home, "empty.txt");
    writeFileSync(e, "");
    const bad = join(home, "bad.txt");
    writeFileSync(bad, Buffer.from([0xff, 0xfe, 0x41]));
    await refused(["record", "x", "--file", p]);
    await refused(["record"]);
    await refused(["record", "-"], "   \n");
    await refused(["record", "--file", e]);
    expect(await refused(["record", "--file", join(home, "missing.txt")])).toContain("FileNotFoundError");
    expect(await refused(["record", "--file", bad])).toContain("UnicodeDecodeError");
    await refused(["record", "x", "--timestamp", "yesterday"]);
    await refused(["record", "x", "--timestamp", "2025-13-01"]);
    await refused(["record", "x", "--metadata", "{not json"]);
    await refused(["record", "x", "--metadata", "[1, 2]"]);
    await refused(["record", "x", "--metadata", '"a string"']);
    await refused(["record", "x", "--tags", ""]);
    expect(await refused(["record", "x", "--tags", "a,,b"])).toContain(
      "anona record: error: argument --tags: must be a comma-separated list of non-empty tags",
    );
  });

  it("record --json: success, nothing stored (exit 3, document still printed), failures", async () => {
    let r = await run(["record", "x", "--json", ...keyed()]);
    expect(r.rc).toBe(0);
    expect(oneJson(r.out)).toEqual(recorded());
    srv.respond = () => ({ status: 201, body: recorded({ memory_id: null, memory_ids: [], status: "nothing_extracted", message: "Nothing was stored." }) });
    r = await run(["record", "x", "--json", ...keyed()]);
    expect(r.rc).toBe(3);
    expect(oneJson(r.out).status).toBe("nothing_extracted");
    expect(r.err).toContain("Nothing was stored.");
    srv.respond = () => ({ status: 500, body: { error: { code: "internal_error", message: "Something broke.", request_id: "req_1" } } });
    r = await run(["record", "x", "--json", ...keyed()]);
    expect([r.rc, r.out]).toEqual([7, ""]);
    expect(r.err).toBe("anona: internal_error: Something broke. (request_id req_1)\n");
    srv.respond = () => ({ status: 401, body: { error: { code: "invalid_api_key", message: "Invalid API key." } } });
    r = await run(["record", "x", "--json", ...keyed()]);
    expect([r.rc, r.out]).toEqual([4, ""]);
  });

  it("retrieve: bare body, flags, and usage errors", async () => {
    expect(await body(["retrieve", "q"])).toEqual({ space_id: "s", query: "q", limit: 10, mode: "accurate" });
    const b = await body([
      "retrieve", "q", "--tags", "a,b", "--tags-match", "all_strict", "--as-of", "2026-01-01T00:00:00+00:00",
      "--occurred-after", "2025-01-01", "--occurred-before", "2025-12-31T23:59:59Z",
    ]);
    expect(b).toEqual({
      space_id: "s", query: "q", limit: 10, mode: "accurate", tags: ["a", "b"], tags_match: "all_strict",
      as_of: "2026-01-01T00:00:00+00:00", occurred_after: "2025-01-01", occurred_before: "2025-12-31T23:59:59Z",
    });
    await refused(["retrieve", "q", "--as-of", "last week"]);
    await refused(["retrieve", "q", "--occurred-after", "2025-02-30"]);
    await refused(["retrieve", "q", "--occurred-before", "soon"]);
    expect(await refused(["retrieve", "q", "--tags", "a", "--tags-match", "some"])).toContain(
      "argument --tags-match: invalid choice: 'some' (choose from 'any', 'all', 'any_strict', 'all_strict', 'exact')",
    );
    expect(await refused(["retrieve", "q", "--tags-match", "all"])).toContain("--tags-match needs --tags");
    await refused(["retrieve", "q", "--tags", ","]);
  });

  it("reason: bare body, flags, receipt --json", async () => {
    expect(await body(["reason", "q"])).toEqual({ space_id: "s", query: "q" });
    expect(await body(["reason", "q", "--depth", "thorough", "--user-id", "u1", "--agent-id", "a1", "--session-id", "s1"])).toEqual({
      space_id: "s", query: "q", user_id: "u1", agent_id: "a1", session_id: "s1", depth: "thorough",
    });
    await refused(["reason", "q", "--depth", "medium"]);
    srv.respond = () => ({ status: 200, body: reasoned("An answer.\nSecond line.", { usage: { credits: 3 } }) });
    const r = await run(["reason", "q", "--json", ...keyed()]);
    expect(r.rc).toBe(0);
    expect(oneJson(r.out)).toEqual({
      insights: "An answer.\nSecond line.",
      sources: { models: [], memories: 2, notes: 1 },
      rules_applied: [],
      model: "m-x",
      usage: { credits: 3 },
    });
  });

  it("--json escapes the control and bidi characters in an answer", async () => {
    srv.respond = () => ({ status: 200, body: reasoned("a\x1b[2Jb‮") });
    const r = await run(["reason", "q", "--json", ...keyed()]);
    expect(r.out).not.toContain("\x1b");
    expect(r.out).not.toContain("‮");
    expect(oneJson(r.out).insights).toBe("a\x1b[2Jb‮");
  });
});
