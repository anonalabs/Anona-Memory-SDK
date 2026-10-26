/**
 * The resource verbs: spaces, memories, jobs, profile, model, user-profile and
 * usage. Every case asserts on the exact method, path, query and body that
 * reached a real local server. Stub bodies are the gateway's response models
 * (SpaceListResponse, MemoryListResponse, JobStatusResponse, ...), every field
 * present.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { oneJson, run, SECRET, startDataServer, useDataEnv, writeMcpFile, type DataServer, type Reply, type Seen } from "./data-helpers.js";

const ESC = "\x1b[2J";
const SPACE = { space_id: "notes", name: "notes", description: "Team notes", created_at: "2026-10-01T00:00:00Z", shared_by: null, qualified_id: null };
const SHARED = { space_id: "demo", name: "demo", description: null, created_at: null, shared_by: "Acme", qualified_id: "acme:demo" };
const ITEM = {
  id: "mem-0001-aaaa",
  content: "The user prefers pnpm.",
  text: "The user prefers pnpm.",
  context: null,
  date: null,
  type: "observation",
  entities: null,
  state: "active",
  metadata: null,
  source_ids: [],
  tags: [],
};
const JOB = {
  job_id: "job-0001-bbbb",
  status: "completed",
  created_at: null,
  completed_at: null,
  error: null,
  memory_count: 2,
  memory_ids: ["mem-a", "mem-b"],
  items_submitted: 3,
  zero_yield_items: null,
  progress: null,
};

const MODE = { status: 200, empty: false, hostile: false, jobNotFound: false };

function canned(s: Seen): Reply {
  const { method, path } = s;
  if (MODE.empty) {
    if (path === "/v1/spaces/") return { status: 200, body: { spaces: [], total: 0 } };
    if (path.endsWith("/memories")) return { status: 200, body: { items: [], total: 0, limit: 50, offset: 0 } };
    if (path.endsWith("/history")) return { status: 200, body: { memory_id: "mem-0001-aaaa", history: [] } };
  }
  if (path === "/v1/spaces/" && method === "GET") return { status: 200, body: { spaces: [SPACE, SHARED], total: 2 } };
  if (path === "/v1/spaces/" && method === "POST") return { status: 201, body: { ...SPACE, space_id: "fresh", name: "fresh", space_type: null } };
  if (/^\/v1\/spaces\/[^/]+$/.test(path) && method === "GET") return { status: 200, body: SPACE };
  if (path.endsWith("/memories") && method === "GET") return { status: 200, body: { items: [ITEM], total: 1, limit: 50, offset: 0 } };
  if (path.endsWith("/history")) {
    return {
      status: 200,
      body: {
        memory_id: "mem-0001-aaaa",
        history: [
          { previous_content: "The user prefers npm.", changed_at: "2026-10-02T00:00:00Z", previous_occurred_start: null, previous_occurred_end: null, reason: "corrected" },
        ],
      },
    };
  }
  if (path.includes("/memories/") && method === "PATCH") return { status: 200, body: { ...ITEM, content: "The user prefers bun.", text: "The user prefers bun." } };
  if (path.includes("/memories/") && method === "DELETE") return { status: 204 };
  if (path.includes("/jobs/") && method === "GET" && MODE.jobNotFound) {
    // The API's answer for an id the space does not hold: 200, not 404.
    return { status: 200, body: { job_id: "job-0001-bbbb", status: "not_found", memory_count: null, memory_ids: null } };
  }
  if (path.includes("/jobs/") && method === "GET") return { status: 200, body: JOB };
  if (path.includes("/jobs/") && method === "DELETE") return { status: 200, body: { job_id: "job-0001-bbbb", status: "cancelled", cancelled_parts: 2, running_parts: 1 } };
  if (path.endsWith("/users/alice/profile")) {
    return {
      status: 200,
      body: { space_id: "s", user_id: "alice", memory_count: 1, first_seen: "2026-09-01T00:00:00Z", last_active: "2026-10-01T00:00:00Z", memories: [ITEM], context: null, token_estimate: null },
    };
  }
  if (path.endsWith("/profile")) {
    return { status: 200, body: { space_id: "s", name: "s", mission: "Remember team decisions.", disposition: { skepticism: 3, literalism: 2, empathy: 4 } } };
  }
  if (path.includes("/models/")) {
    return {
      status: 200,
      body: { id: "mm-0001-cccc", space_id: "s", name: "Build habits", query: "How does this team build?", content: "Uses pnpm.\n\nShips Tuesdays.", tags: [], max_tokens: null, sources: [] },
    };
  }
  if (path === "/v1/usage/me") return { status: 200, body: { credits_remaining: 4200, credits_limit: 5000, credits_used: 800, rate_limit_per_min: 60, credits_banked: 0 } };
  return { status: 404, body: { error: { code: "not_found", message: "No route." } } };
}

function hostile(doc: unknown): unknown {
  if (Array.isArray(doc)) return doc.map(hostile);
  if (doc && typeof doc === "object") return Object.fromEntries(Object.entries(doc).map(([k, v]) => [k, hostile(v)]));
  return typeof doc === "string" ? doc + ESC : doc;
}

function respond(s: Seen): Reply {
  const st = MODE.status;
  if (st === 401) return { status: 401, body: { error: { code: "invalid_api_key", message: "Invalid API key." } } };
  if (st === 403) return { status: 403, body: { error: { code: "temp_profile_single_space", message: "A temporary profile has one space.", request_id: "req_9" } } };
  if (st === 404) return { status: 404, body: { error: { code: "not_found", message: "Not found.", request_id: "req_4" } } };
  if (st >= 400) return { status: st, body: { error: { code: "internal_error", message: "Something broke.", request_id: "req_1" } }, headers: { "retry-after": "0" } };
  const r = canned(s);
  return MODE.hostile && r.body !== undefined ? { ...r, body: hostile(r.body) } : r;
}

describe("resource verbs", () => {
  useDataEnv();
  let srv: DataServer;
  let KEYED: string[];
  let AUTH: string[];
  let VERBS: Array<[string[], string, string, Record<string, string>, unknown]>;
  beforeEach(async () => {
    MODE.status = 200;
    MODE.empty = false;
    MODE.hostile = false;
    MODE.jobNotFound = false;
    srv = await startDataServer(respond);
    KEYED = ["--base-url", srv.base, "--api-key", SECRET];
    AUTH = ["--space", "s", ...KEYED];
    VERBS = [
      [["spaces", "list", ...KEYED], "GET", "/v1/spaces/", {}, null],
      [["spaces", "create", "fresh", ...KEYED], "POST", "/v1/spaces/", {}, { name: "fresh", description: null }],
      [["spaces", "get", "notes", ...KEYED], "GET", "/v1/spaces/notes", {}, null],
      [["memories", "list", ...AUTH], "GET", "/v1/spaces/s/memories", { limit: "50", offset: "0" }, null],
      [["memories", "edit", "mem-0001-aaaa", "--text", "The user prefers bun.", ...AUTH], "PATCH", "/v1/spaces/s/memories/mem-0001-aaaa", {}, { text: "The user prefers bun." }],
      [["memories", "delete", "mem-0001-aaaa", "--yes", ...AUTH], "DELETE", "/v1/spaces/s/memories/mem-0001-aaaa", {}, null],
      [["memories", "history", "mem-0001-aaaa", ...AUTH], "GET", "/v1/spaces/s/memories/mem-0001-aaaa/history", {}, null],
      [["jobs", "get", "job-0001-bbbb", ...AUTH], "GET", "/v1/spaces/s/jobs/job-0001-bbbb", {}, null],
      [["jobs", "cancel", "job-0001-bbbb", "--yes", ...AUTH], "DELETE", "/v1/spaces/s/jobs/job-0001-bbbb", {}, null],
      [["profile", ...AUTH], "GET", "/v1/spaces/s/profile", {}, null],
      [["model", "mm-0001-cccc", ...AUTH], "GET", "/v1/spaces/s/models/mm-0001-cccc", {}, null],
      [["user-profile", "alice", ...AUTH], "GET", "/v1/spaces/s/users/alice/profile", {}, null],
      [["usage", ...KEYED], "GET", "/v1/usage/me", {}, null],
    ];
  });
  afterEach(() => srv.close());

  async function sent(argv: string[], wantRc = 0): Promise<[Seen, string, string]> {
    const n = srv.seen.length;
    const r = await run(argv);
    expect(r.rc, `${argv.join(" ")}: ${r.err}`).toBe(wantRc);
    expect(srv.seen.length, argv.join(" ")).toBe(n + 1);
    return [srv.seen.at(-1) as Seen, r.out, r.err];
  }

  async function refused(argv: string[], want = 2): Promise<string> {
    const n = srv.seen.length;
    const r = await run(argv);
    expect(r.rc, `${argv.join(" ")}: ${r.err}`).toBe(want);
    expect(srv.seen.length, `sent: ${argv.join(" ")}`).toBe(n);
    expect(r.out).toBe("");
    return r.err;
  }

  it("each verb sends exactly its request", async () => {
    for (const [argv, method, path, query, body] of VERBS) {
      const [s] = await sent(argv);
      expect([s.method, s.path], argv.join(" ")).toEqual([method, path]);
      expect(s.query, argv.join(" ")).toEqual(query);
      expect(s.body, argv.join(" ")).toEqual(body);
      expect(s.auth).toBe("Bearer " + SECRET);
    }
  });

  it("flags reach the request and unset ones stay out", async () => {
    let [s] = await sent([
      "memories", "list", "--limit", "5", "--offset", "10", "--q", "pnpm", "--type", "observation",
      "--no-prefer-observations", "--user-id", "u1", "--agent-id", "a1", "--session-id", "s1", ...AUTH,
    ]);
    expect(s.query).toEqual({
      limit: "5", offset: "10", q: "pnpm", type: "observation", prefer_observations: "false", user_id: "u1", agent_id: "a1", session_id: "s1",
    });
    [s] = await sent(["memories", "list", "--prefer-observations", ...AUTH]);
    expect(s.query.prefer_observations).toBe("true");
    [s] = await sent(["memories", "edit", "mem-1", "--text", "New.", "--reason", "typo", ...AUTH]);
    expect(s.body).toEqual({ text: "New.", reason: "typo" });
    [s] = await sent(["spaces", "create", "fresh", "--description", "d", ...KEYED]);
    expect(s.body).toEqual({ name: "fresh", description: "d" });
    [s] = await sent(["user-profile", "alice", "--limit", "3", "--offset", "6", ...AUTH]);
    expect(s.query).toEqual({ limit: "3", offset: "6" });
  });

  it("--json is one document on every verb, and it is the API's", async () => {
    for (const [argv] of VERBS) {
      const [, out] = await sent([...argv, "--json"]);
      expect(typeof oneJson(out)).toBe("object");
    }
    let [, out] = await sent(["spaces", "list", "--json", ...KEYED]);
    expect(oneJson(out)).toEqual({ spaces: [SPACE, SHARED], total: 2 });
    [, out] = await sent(["memories", "list", "--json", ...AUTH]);
    expect(oneJson(out).items[0].id).toBe("mem-0001-aaaa");
    [, out] = await sent(["jobs", "cancel", "job-0001-bbbb", "--yes", "--json", ...AUTH]);
    expect(oneJson(out).running_parts).toBe(1);
    [, out] = await sent(["memories", "delete", "mem-0001-aaaa", "--yes", "--json", ...AUTH]);
    expect(oneJson(out)).toEqual({ deleted: true, memory_id: "mem-0001-aaaa", space_id: "s" });
    expect(out).toBe('{"deleted": true, "memory_id": "mem-0001-aaaa", "space_id": "s"}\n');
  });

  it("plain output names full ids", async () => {
    const text = async (argv: string[]) => (await sent(argv))[1];
    let out = await text(["spaces", "list", ...KEYED]);
    expect(out).toBe("notes  Team notes\nacme:demo  (shared by Acme)\n");
    expect(await text(["spaces", "create", "fresh", ...KEYED])).toBe("Created space 'fresh'.\n");
    expect(await text(["spaces", "get", "notes", ...KEYED])).toBe(
      "space_id: notes\nname: notes\ndescription: Team notes\ncreated_at: 2026-10-01T00:00:00Z\n",
    );
    expect(await text(["memories", "list", ...AUTH])).toBe("mem-0001-aaaa  observation  The user prefers pnpm.\nShowing 1-1 of 1.\n");
    expect(await text(["memories", "edit", "mem-0001-aaaa", "--text", "x", ...AUTH])).toBe("Updated memory mem-0001-aaaa.\n");
    expect(await text(["memories", "delete", "mem-0001-aaaa", "--yes", ...AUTH])).toBe("Deleted memory mem-0001-aaaa from space 's'.\n");
    expect(await text(["memories", "history", "mem-0001-aaaa", ...AUTH])).toBe(
      "2026-10-02T00:00:00Z  was: The user prefers npm.  (reason: corrected)\n",
    );
    expect(await text(["jobs", "get", "job-0001-bbbb", ...AUTH])).toBe(
      "Job job-0001-bbbb: completed, 2 memories from 3 items.\nMemory ids: mem-a, mem-b\n",
    );
    expect(await text(["jobs", "cancel", "job-0001-bbbb", "--yes", ...AUTH])).toBe(
      "Cancelled 2 queued part(s) of job job-0001-bbbb; 1 already running will finish.\n",
    );
    expect(await text(["profile", ...AUTH])).toBe(
      "Space: s\nMission: Remember team decisions.\nDisposition: skepticism=3, literalism=2, empathy=4\n",
    );
    out = await text(["model", "mm-0001-cccc", ...AUTH]);
    expect(out).toBe("Build habits (mm-0001-cccc)\nQuery: How does this team build?\n\nUses pnpm.\n\nShips Tuesdays.\n");
    expect(await text(["user-profile", "alice", ...AUTH])).toBe(
      "User alice in space 's': 1 memories, first seen 2026-09-01T00:00:00Z, last active 2026-10-01T00:00:00Z.\n" +
        "mem-0001-aaaa  observation  The user prefers pnpm.\n",
    );
    expect(await text(["usage", ...KEYED])).toBe("Credits: 4200 remaining of 5000 (800 used).\nRate limit: 60 requests a minute.\n");
  });

  it("empty lists are success, with something on stdout", async () => {
    MODE.empty = true;
    for (const argv of [["spaces", "list", ...KEYED], ["memories", "list", ...AUTH], ["memories", "history", "mem-0001-aaaa", ...AUTH]]) {
      let [, out] = await sent(argv);
      expect(out.trim()).not.toBe("");
      [, out] = await sent([...argv, "--json"]);
      oneJson(out);
    }
  });

  it("server errors map to exit codes; stdout stays empty", async () => {
    for (const [status, want] of [[401, 4], [403, 4], [404, 5], [500, 7]] as const) {
      MODE.status = status;
      for (const [argv] of VERBS) {
        for (const extra of [[], ["--json"]]) {
          const n = srv.seen.length;
          const r = await run([...argv, ...extra]);
          expect(r.rc, `${argv.join(" ")} ${status}`).toBe(want);
          expect(srv.seen.length).toBeGreaterThan(n);
          expect(r.out).toBe("");
          expect(r.err.startsWith("anona: ")).toBe(true);
        }
      }
    }
    MODE.status = 404;
    const [, , err] = await sent(["memories", "history", "nope", ...AUTH], 5);
    expect(err.trim()).toBe("anona: not_found: Not found. (request_id req_4)");
  });

  it("jobs get on a job the space does not hold exits 5, nothing on stdout", async () => {
    MODE.jobNotFound = true;
    for (const extra of [[], ["--json"]]) {
      const r = await run(["jobs", "get", "job-0001-bbbb", ...AUTH, ...extra]);
      expect(r.rc).toBe(5);
      expect(r.out).toBe("");
      expect(r.err.trim()).toBe("anona: job_not_found: No job with that id in this space.");
    }
  });

  it("a 5xx on a read is retried, with a notice; on a write it is not", async () => {
    MODE.status = 500;
    let n = srv.seen.length;
    let r = await run(["memories", "list", ...AUTH]);
    expect(r.rc).toBe(7);
    expect(srv.seen.length - n).toBe(3);
    expect(r.err).toContain("anona: server error 500, retrying in 0 s (1/2)");
    n = srv.seen.length;
    r = await run(["spaces", "create", "x", ...KEYED]);
    expect(srv.seen.length - n).toBe(1);
    expect(r.err).not.toContain("retrying");
  });

  it("destructive verbs need --yes and send nothing without it", async () => {
    let err = await refused(["memories", "delete", "mem-0001-aaaa", ...AUTH]);
    expect(err).toBe("anona: this would delete memory mem-0001-aaaa in space 's'. Pass --yes to do it. Nothing was sent.\n");
    expect(await refused(["memories", "delete", "mem-0001-aaaa", "--json", ...AUTH])).toContain("--yes");
    err = await refused(["jobs", "cancel", "job-0001-bbbb", ...AUTH]);
    expect(err).toContain("job-0001-bbbb");
    expect(err).toContain("--yes");
    writeMcpFile({ base: srv.base, tempHours: 1 });
    expect(await refused(["memories", "delete", "mem-0001-aaaa", "--base-url", srv.base])).toContain("'default'");
    await refused(["jobs", "cancel", "job-0001-bbbb", "--base-url", srv.base]);
  });

  it("an API key needs a space for space verbs only", async () => {
    for (const [argv] of VERBS) {
      const i = argv.indexOf("--space");
      if (i < 0) continue;
      await refused([...argv.slice(0, i), ...argv.slice(i + 2)]);
    }
    process.env.ANONA_SPACE_ID = "envspace";
    const [s] = await sent(["profile", ...KEYED]);
    expect(s.path).toBe("/v1/spaces/envspace/profile");
    delete process.env.ANONA_SPACE_ID;
    await sent(["spaces", "list", ...KEYED]);
    await sent(["usage", ...KEYED]);
  });

  it("a temporary profile uses its default space; a second space is the server's call", async () => {
    writeMcpFile({ base: srv.base, tempHours: 1 });
    let [s] = await sent(["memories", "list", "--base-url", srv.base]);
    expect([s.path, s.auth]).toEqual(["/v1/spaces/default/memories", "Bearer anona_tmp_TEMPTOKEN"]);
    [s] = await sent(["usage", "--base-url", srv.base]);
    expect(s.path).toBe("/v1/usage/me");
    MODE.status = 403;
    const [, out, err] = await sent(["spaces", "create", "another", "--base-url", srv.base], 4);
    expect(out).toBe("");
    expect(err).toContain("temp_profile_single_space");
  });

  it("server text cannot reach the terminal raw", async () => {
    MODE.hostile = true;
    for (const [argv] of VERBS) {
      const [, out, err] = await sent(argv);
      expect(out + err, argv.join(" ")).not.toContain("\x1b");
    }
  });

  it("usage errors send nothing", async () => {
    expect(await refused(["memories", "edit", "mem-1", ...AUTH])).toContain("the following arguments are required: --text");
    expect(await refused(["memories", "edit", "mem-1", "--text", "  ", ...AUTH])).toBe("anona: the memory text is empty. Nothing was sent.\n");
    expect(await refused(["memories", "list", "--limit", "0", ...AUTH])).toContain("argument --limit: must be between 1 and 200");
    await refused(["memories", "list", "--limit", "201", ...AUTH]);
    expect(await refused(["memories", "list", "--offset", "-1", ...AUTH])).toContain("argument --offset: must be 0 or more");
    expect(await refused(["memories", "list", "--limit", "x", ...AUTH])).toContain("argument --limit: invalid parse value: 'x'");
    await refused(["spaces", "create", " ", ...KEYED]);
    // Before the action only the group's own options exist, so with --api-key
    // there the key itself reads as the action -- as argparse reads it.
    expect(await refused(["spaces", ...KEYED])).toContain("argument action: invalid choice: 'anona_live_…'");
    expect(await refused(["spaces", "--base-url", srv.base])).toContain(
      "anona spaces: error: the following arguments are required: action",
    );
    await refused(["memories", ...AUTH]);
    await refused(["jobs", ...AUTH]);
    expect(await refused(["spaces", "wat", ...KEYED])).toContain(
      "argument action: invalid choice: 'wat' (choose from 'list', 'create', 'get', 'delete', 'members')",
    );
    expect(await refused(["spaces", "--json", "list", ...KEYED])).toContain("anona: error: unrecognized arguments: --json");
  });

  it("every verb is in help, and each group lists its actions", async () => {
    const top = await run(["--help"]);
    expect(top.rc).toBe(0);
    for (const verb of ["record", "retrieve", "reason", "spaces", "memories", "jobs", "profile", "model", "user-profile", "usage"]) {
      expect(top.out).toMatch(new RegExp(`^\\s+${verb}\\s+\\S`, "m"));
    }
    for (const [group, actions] of [["spaces", ["list", "create", "get"]], ["memories", ["list", "edit", "delete", "history"]], ["jobs", ["get", "cancel"]]] as const) {
      const r = await run([group, "--help"]);
      expect(r.rc).toBe(0);
      for (const a of actions) expect(r.out).toMatch(new RegExp(`^\\s+${a}\\s+\\S`, "m"));
    }
  });
});
