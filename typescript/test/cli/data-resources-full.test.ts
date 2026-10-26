/**
 * The CLI's coverage of the rest of the API: memory models, rules, webhooks,
 * the entity graph, receipts, ask-user, chat, exports, and the account views.
 *
 * Every case runs a real local HTTP server and asserts on the exact method,
 * path, query and body that reached it and on the exact text printed, so a verb
 * that called the wrong route, sent a flag that was not given, printed a line
 * other than the documented one, or sent anything at all when it should have
 * refused, fails here. Mirrors the pip package's test of the same commands.
 */
import { existsSync, mkdirSync, readdirSync, readFileSync, symlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { oneJson, run, SECRET, startDataServer, useDataEnv, writeMcpFile, type DataServer, type Reply, type Seen } from "./data-helpers.js";

const LEAK = "anona_live_LEAKEDSECRET";
const MODEL = { id: "mm-1", space_id: "s", name: "Build habits", query: "How does this team build?", content: "Uses pnpm.", tags: [] };
const PROFILE = { space_id: "s", name: "s", mission: "M", disposition: { skepticism: 3, literalism: 2, empathy: 4 } };
const ROUTING = {
  space_id: "s",
  charter: null,
  topics: null,
  rules: null,
  auto_route_enabled: true,
  shared_auto_route: false,
  observed_terms: null,
  inferred_terms: null,
  rendered_criteria: null,
};
const JOB = { model_id: "mm-1", job_id: "job-9", status: "processing" };

type Route = [method: string, pattern: RegExp, status: number, body: unknown];
const ROUTES: Route[] = [
  ["GET", /^\/v1\/spaces\/s\/models$/, 200, { items: [MODEL], total: 1, limit: 50, offset: 0 }],
  ["POST", /^\/v1\/spaces\/s\/models$/, 202, JOB],
  ["GET", /^\/v1\/spaces\/s\/models\/mm-1$/, 200, MODEL],
  ["PATCH", /^\/v1\/spaces\/s\/models\/mm-1$/, 200, MODEL],
  ["DELETE", /^\/v1\/spaces\/s\/models\/mm-1$/, 204, null],
  ["POST", /^\/v1\/spaces\/s\/models\/mm-1\/refresh$/, 202, JOB],
  ["POST", /^\/v1\/spaces\/s\/models\/mm-1\/clear$/, 200, MODEL],
  ["GET", /^\/v1\/spaces\/s\/models\/mm-1\/history$/, 200, { entries: [{ previous_content: "x".repeat(100), changed_at: "2026-10-02T00:00:00Z" }], total: 1 }],
  ["GET", /^\/v1\/spaces\/s\/profile$/, 200, PROFILE],
  ["PUT", /^\/v1\/spaces\/s\/profile$/, 200, PROFILE],
  ["DELETE", /^\/v1\/spaces\/s\/profile$/, 204, null],
  ["GET", /^\/v1\/spaces\/s\/rules$/, 200, { items: [{ id: "r-1", space_id: "s", name: "n", content: "Never X." }], total: 1 }],
  ["POST", /^\/v1\/spaces\/s\/rules$/, 201, { id: "r-1", space_id: "s", name: "n", content: "Never X." }],
  ["PATCH", /^\/v1\/spaces\/s\/rules\/r-1$/, 200, { id: "r-1", space_id: "s", name: "n", content: "Y." }],
  ["DELETE", /^\/v1\/spaces\/s\/rules\/r-1$/, 204, null],
  [
    "GET",
    /^\/v1\/spaces\/s\/webhooks$/,
    200,
    { items: [{ id: "w-1", url: "https://x.example/h", enabled: true, event_types: ["memory.created", "memory.consolidated"] }] },
  ],
  ["POST", /^\/v1\/spaces\/s\/webhooks$/, 201, { id: "w-1", url: "https://x.example/h", enabled: true, event_types: ["memory.created"], secret: "whsec_TESTSECRET" }],
  ["PATCH", /^\/v1\/spaces\/s\/webhooks\/w-1$/, 200, { id: "w-1", url: "https://x.example/h", enabled: false, event_types: ["memory.created"] }],
  ["DELETE", /^\/v1\/spaces\/s\/webhooks\/w-1$/, 204, null],
  [
    "GET",
    /^\/v1\/spaces\/s\/webhooks\/w-1\/deliveries$/,
    200,
    { items: [{ id: "d-1", event_type: "memory.created", status: "completed", attempts: 1, delivered_at: "2026-10-03T00:00:00Z" }], next_cursor: null },
  ],
  [
    "GET",
    /^\/v1\/spaces\/s\/graph$/,
    200,
    { nodes: [{ id: "e1", label: "Alice", mention_count: 3 }], edges: [], total_entities: 1, total_edges: 0 },
  ],
  ["POST", /^\/v1\/spaces\/s\/graph$/, 200, { text: "Here is the chart.", chart: null, diagram: null, mermaid: null, model: "m" }],
  ["GET", /^\/v1\/spaces\/s\/entities$/, 200, { items: [{ id: "e1", name: "Alice", mention_count: 3 }], total: 1, limit: 100, offset: 0 }],
  [
    "GET",
    /^\/v1\/spaces\/s\/entities\/e1$/,
    200,
    { id: "e1", name: "Alice", mention_count: 3, observations: [{ text: "Likes tea.", mentioned_at: "2026-10-01" }] },
  ],
  [
    "GET",
    /^\/v1\/receipts\/req_1$/,
    200,
    { request_id: "req_1", space_id: "s", included: [{ id: "a" }], excluded: [{ id: "b" }], token_accounting: { used: 40, budget: 100 } },
  ],
  ["GET", /^\/v1\/receipts\/req_1\/explain$/, 200, { memory_id: "m1", outcome: "excluded", stage: "budget", detail: "over budget" }],
  ["POST", /^\/v1\/spaces\/s\/users\/alice\/ask$/, 200, { space_id: "s", user_id: "alice", insights: "She likes tea." }],
  ["GET", /^\/v1\/space-types$/, 200, { items: [{ space_type: "general", name: "General" }], total: 1 }],
  ["POST", /^\/v1\/chat\/completions$/, 200, { id: "c1", choices: [{ message: { role: "assistant", content: "Hello!" } }], usage: {} }],
  ["GET", /^\/v1\/orgs\/me$/, 200, { org_id: "o1", name: "Acme", slug: "acme", plan_id: "developer" }],
  ["GET", /^\/v1\/orgs$/, 200, { orgs: [{ org_id: "o1", name: "Acme", role: "owner" }], max_orgs: 3 }],
  ["GET", /^\/v1\/projects$/, 200, [{ project_id: "p1", org_id: "o1", name: "Docs" }]],
  [
    "GET",
    /^\/v1\/api-keys$/,
    200,
    [{ id: "k1", name: "ci", key_prefix: "anona_live_ab", env: "live", created_at: "2026-10-01", last_used_at: null, status: "active", key: LEAK }],
  ],
  ["GET", /^\/v1\/members$/, 200, [{ member_id: "m1", email: "a@x.example", role: "owner" }]],
  ["GET", /^\/v1\/members\/me$/, 200, { member_id: "m1", email: "a@x.example", role: "owner" }],
  ["GET", /^\/v1\/members\/invitations$/, 200, { invitations: [{ invitation_id: "i1", email: "b@x.example", role: "developer" }] }],
  [
    "GET",
    /^\/v1\/invitations\/pending$/,
    200,
    { invitations: [{ token: "tok-1", kind: "space", org_name: "Acme", space_id: "notes", role: "viewer", expires_at: "x" }] },
  ],
  ["POST", /^\/v1\/invitations\/accept$/, 200, { org_id: "o1", space_id: null }],
  ["POST", /^\/v1\/invitations\/decline$/, 204, null],
  [
    "GET",
    /^\/v1\/billing$/,
    200,
    {
      plan_id: "developer",
      plan_name: "Developer",
      credits_limit: 5000,
      credits_used: 800,
      credits_remaining: 4200,
      credits_banked: 0,
      credits_reset_at: "2026-11-01T00:00:00Z",
    },
  ],
  ["GET", /^\/v1\/billing\/topups$/, 200, { items: [{ id: "t1", purchased_at: "2026-10-05", pack: "small", credits: 1000, status: "active" }] }],
  ["GET", /^\/v1\/usage$/, 200, { series: [{ date: "2026-10-01", ops: 12 }], period_days: 7 }],
  ["GET", /^\/v1\/usage\/breakdown$/, 200, { breakdown: [{ space_id: "s", memories_added: 2, searches: 3, insights: 1 }], period_days: 30 }],
  ["GET", /^\/v1\/spaces\/s\/exports$/, 200, { items: [{ job_id: "j1", status: "completed", created_at: "2026-10-01" }], total: 1 }],
  ["POST", /^\/v1\/spaces\/s\/exports$/, 202, { job_id: "j1", space_id: "s", status: "pending" }],
  ["GET", /^\/v1\/spaces\/notes\/members$/, 200, [{ member_id: "m1", email: "a@x.example", role: "viewer", status: "active" }]],
  ["DELETE", /^\/v1\/spaces\/notes\/members\/m1$/, 204, null],
  ["GET", /^\/v1\/spaces\/s\/routing-settings$/, 200, ROUTING],
  ["PUT", /^\/v1\/spaces\/s\/routing-settings$/, 200, ROUTING],
  ["DELETE", /^\/v1\/spaces\/s\/routing-settings$/, 204, null],
];

const MODE = { status: 200, stream: false, blob: 200 };
let base = "";
let routes: Route[] = ROUTES;

function canned(s: Seen): Reply {
  if (s.path === "/v1/spaces/s/exports/j1/download") {
    if (MODE.stream) return { status: 200, raw: "ZIPDATA", headers: { "content-type": "application/zip" } };
    return { status: 200, body: { download_url: base + "/blob/j1", expires_in: 300 } };
  }
  if (s.path === "/v1/spaces/s/exports/jd/download") {
    return { status: 200, stream: { chunks: ["abcde"], delayMs: 10, length: 100, die: true } };
  }
  if (s.path === "/blob/j1") {
    if (MODE.blob !== 200) return { status: MODE.blob, raw: "<Error><Code>AccessDenied</Code></Error>", headers: { "content-type": "application/xml" } };
    return { status: 200, raw: "ZIPDATA", headers: { "content-type": "application/zip" } };
  }
  for (const [method, pattern, status, body] of routes) {
    if (method === s.method && pattern.test(s.path)) return body === null ? { status } : { status, body };
  }
  return { status: 404, body: { error: { code: "not_found", message: "No route." } } };
}

function respond(s: Seen): Reply {
  if (MODE.status === 401) {
    return {
      status: 401,
      body: {
        error: {
          code: "session_required",
          message: "This needs a dashboard session or `anona login --cli`; an API key or temporary profile cannot use it.",
        },
      },
    };
  }
  return canned(s);
}

const bodyOf = (method: string, path: string): unknown => routes.find(([m, re]) => m === method && re.test(path))?.[3];

describe("resource commands for the rest of the API", () => {
  useDataEnv();
  let srv: DataServer;
  beforeAll(async () => {
    srv = await startDataServer(respond);
    base = srv.base;
  });
  afterAll(() => srv.close());
  beforeEach(() => {
    MODE.status = 200;
    MODE.stream = false;
    MODE.blob = 200;
    routes = ROUTES;
    srv.seen.length = 0;
  });

  const KEYED = () => ["--base-url", srv.base, "--api-key", SECRET];
  const AUTH = () => ["--space", "s", ...KEYED()];

  /** Run one command that must send exactly one request. */
  async function sent(argv: string[], wantRc = 0) {
    const before = srv.seen.length;
    const r = await run(argv);
    expect(r.rc, `${JSON.stringify(argv)}\n${r.err}`).toBe(wantRc);
    expect(srv.seen.slice(before).length, JSON.stringify(argv)).toBe(1);
    return { seen: srv.seen[srv.seen.length - 1] as Seen, ...r };
  }

  /** Run one command that must refuse with exit `want`, send nothing and print nothing on stdout. */
  async function refused(argv: string[], want = 2) {
    const before = srv.seen.length;
    const r = await run(argv);
    expect(r.rc, `${JSON.stringify(argv)}\n${r.out}${r.err}`).toBe(want);
    expect(srv.seen.length, `sent: ${JSON.stringify(argv)}`).toBe(before);
    expect(r.out, JSON.stringify(argv)).toBe("");
    return r.err;
  }

  const LONG = "x".repeat(80);
  // [argv, method, path, query, request body, exact stdout]
  type Case = [string[], string, string, Record<string, string>, unknown, string];
  const cases = (): Case[] => {
    const AUTHED = AUTH();
    const KEY = KEYED();
    return [
      [["models", "list", ...AUTHED], "GET", "/v1/spaces/s/models", {}, null, "mm-1  Build habits  How does this team build?\n"],
      [
        ["models", "list", "--limit", "5", "--offset", "2", "--tags", "a", ...AUTHED],
        "GET",
        "/v1/spaces/s/models",
        { limit: "5", offset: "2", tags: "a" },
        null,
        "mm-1  Build habits  How does this team build?\n",
      ],
      [
        ["models", "create", "Build habits", "--query", "How?", ...AUTHED],
        "POST",
        "/v1/spaces/s/models",
        {},
        { name: "Build habits", query: "How?" },
        "Creating memory model mm-1 (job job-9).\n",
      ],
      [
        ["models", "create", "Build habits", "--query", "How?", "--tags", "a", "--mode", "delta", "--refresh-on-new-memories", "--memory-type", "fact", "--max-tokens", "512", "--model-id", "build", ...AUTHED],
        "POST",
        "/v1/spaces/s/models",
        {},
        {
          name: "Build habits",
          query: "How?",
          model_id: "build",
          tags: ["a"],
          max_tokens: 512,
          trigger: { mode: "delta", refresh_on_new_memories: true, memory_type: ["fact"] },
        },
        "Creating memory model mm-1 (job job-9).\n",
      ],
      [["models", "get", "mm-1", ...AUTHED], "GET", "/v1/spaces/s/models/mm-1", {}, null, "Build habits (mm-1)\nQuery: How does this team build?\n\nUses pnpm.\n"],
      [["models", "edit", "mm-1", "--name", "Renamed", ...AUTHED], "PATCH", "/v1/spaces/s/models/mm-1", {}, { name: "Renamed" }, "Updated memory model mm-1.\n"],
      [["models", "delete", "mm-1", "--yes", ...AUTHED], "DELETE", "/v1/spaces/s/models/mm-1", {}, null, "Deleted memory model mm-1.\n"],
      [["models", "refresh", "mm-1", ...AUTHED], "POST", "/v1/spaces/s/models/mm-1/refresh", {}, null, "Refreshing mm-1 (job job-9).\n"],
      [["models", "clear", "mm-1", "--yes", ...AUTHED], "POST", "/v1/spaces/s/models/mm-1/clear", {}, null, "Cleared mm-1; its definition is kept.\n"],
      [["models", "history", "mm-1", ...AUTHED], "GET", "/v1/spaces/s/models/mm-1/history", {}, null, `2026-10-02T00:00:00Z  ${LONG}\n`],
      [["profile", "reset", "--yes", ...AUTHED], "DELETE", "/v1/spaces/s/profile", {}, null, "Reset the profile of space 's'.\n"],
      [["rules", "list", ...AUTHED], "GET", "/v1/spaces/s/rules", {}, null, "r-1  Never X.\n"],
      [["rules", "add", "Never X.", ...AUTHED], "POST", "/v1/spaces/s/rules", {}, { name: "Never X.", content: "Never X." }, "Added rule r-1.\n"],
      [
        ["rules", "add", "Never X.", "--name", "N", "--priority", "5", "--no-active", "--tags", "a", ...AUTHED],
        "POST",
        "/v1/spaces/s/rules",
        {},
        { name: "N", content: "Never X.", priority: 5, is_active: false, tags: ["a"] },
        "Added rule r-1.\n",
      ],
      [["rules", "edit", "r-1", "--text", "Y.", "--priority", "3", ...AUTHED], "PATCH", "/v1/spaces/s/rules/r-1", {}, { content: "Y.", priority: 3 }, "Updated rule r-1.\n"],
      [["rules", "delete", "r-1", "--yes", ...AUTHED], "DELETE", "/v1/spaces/s/rules/r-1", {}, null, "Deleted rule r-1.\n"],
      [["webhooks", "list", ...AUTHED], "GET", "/v1/spaces/s/webhooks", {}, null, "w-1  https://x.example/h  memory.created,memory.consolidated\n"],
      [
        ["webhooks", "create", "https://x.example/h", "--events", "memory.created", ...AUTHED],
        "POST",
        "/v1/spaces/s/webhooks",
        {},
        { url: "https://x.example/h", event_types: ["memory.created"] },
        "Created webhook w-1.\nSigning secret: whsec_TESTSECRET\nSave this secret now; it is not shown again.\n",
      ],
      [["webhooks", "edit", "w-1", "--no-enabled", ...AUTHED], "PATCH", "/v1/spaces/s/webhooks/w-1", {}, { enabled: false }, "Updated webhook w-1.\n"],
      [["webhooks", "delete", "w-1", "--yes", ...AUTHED], "DELETE", "/v1/spaces/s/webhooks/w-1", {}, null, "Deleted webhook w-1.\n"],
      [
        ["webhooks", "deliveries", "w-1", "--limit", "5", "--cursor", "c", ...AUTHED],
        "GET",
        "/v1/spaces/s/webhooks/w-1/deliveries",
        { limit: "5", cursor: "c" },
        null,
        "2026-10-03T00:00:00Z  completed  memory.created\n",
      ],
      [["graph", ...AUTHED], "GET", "/v1/spaces/s/graph", {}, null, "1 entities, 0 links\nAlice (3)\n"],
      [["graph", "--limit", "5", "--min-count", "2", ...AUTHED], "GET", "/v1/spaces/s/graph", { limit: "5", min_count: "2" }, null, "1 entities, 0 links\nAlice (3)\n"],
      [["graph", "draw", "by week", ...AUTHED], "POST", "/v1/spaces/s/graph", {}, { request: "by week" }, "Here is the chart.\n"],
      [["graph", "draw", "by week", "--model", "m", ...AUTHED], "POST", "/v1/spaces/s/graph", {}, { request: "by week", model: "m" }, "Here is the chart.\n"],
      [["entities", "list", "--limit", "5", "--offset", "1", ...AUTHED], "GET", "/v1/spaces/s/entities", { limit: "5", offset: "1" }, null, "e1  Alice  3\n"],
      [["entities", "get", "e1", ...AUTHED], "GET", "/v1/spaces/s/entities/e1", {}, null, "Alice (e1)\nMentions: 3\n2026-10-01  Likes tea.\n"],
      [
        ["receipts", "get", "req_1", ...KEY],
        "GET",
        "/v1/receipts/req_1",
        {},
        null,
        "Receipt req_1 for space 's'\nIncluded: 1, excluded: 1\nTokens: used=40, budget=100\n",
      ],
      [
        ["receipts", "get", "req_1", "--explain", "--memory-id", "m1", ...KEY],
        "GET",
        "/v1/receipts/req_1/explain",
        { memory_id: "m1" },
        null,
        "Memory m1: excluded\nStage: budget\nDetail: over budget\n",
      ],
      [
        ["ask-user", "alice", "What does she like?", "--model", "m", "--depth", "thorough", ...AUTHED],
        "POST",
        "/v1/spaces/s/users/alice/ask",
        {},
        { query: "What does she like?", model: "m", depth: "thorough" },
        "She likes tea.\n",
      ],
      [["ask-user", "alice", "What does she like?", ...AUTHED], "POST", "/v1/spaces/s/users/alice/ask", {}, { query: "What does she like?" }, "She likes tea.\n"],
      [["space-types", ...KEY], "GET", "/v1/space-types", {}, null, "general  General\n"],
      [["chat", "hi", ...AUTHED], "POST", "/v1/chat/completions", {}, { messages: [{ role: "user", content: "hi" }], space_id: "s" }, "Hello!\n"],
      [
        ["chat", "hi", "--model", "m", "--no-memory", ...AUTHED],
        "POST",
        "/v1/chat/completions",
        {},
        { messages: [{ role: "user", content: "hi" }], space_id: "s", model: "m", memory: false, auto_record: false },
        "Hello!\n",
      ],
      [["org", ...KEY], "GET", "/v1/orgs/me", {}, null, "o1  Acme  acme\n"],
      [["orgs", "list", ...KEY], "GET", "/v1/orgs", {}, null, "o1  Acme  owner\n"],
      [["projects", "list", ...KEY], "GET", "/v1/projects", {}, null, "p1  Docs\n"],
      [["keys", "list", ...KEY], "GET", "/v1/api-keys", {}, null, "anona_live_ab…  ci  2026-10-01  -\n"],
      [["members", "list", ...KEY], "GET", "/v1/members", {}, null, "m1  a@x.example  owner\n"],
      [["members", "me", ...KEY], "GET", "/v1/members/me", {}, null, "m1  a@x.example  owner\n"],
      [["members", "invitations", ...KEY], "GET", "/v1/members/invitations", {}, null, "i1  b@x.example  developer\n"],
      [["invitations", "pending", ...KEY], "GET", "/v1/invitations/pending", {}, null, "tok-1  space  notes  viewer\n"],
      [["invitations", "accept", "tok-1", ...KEY], "POST", "/v1/invitations/accept", {}, { token: "tok-1" }, "Joined o1.\n"],
      [["invitations", "decline", "tok-1", ...KEY], "POST", "/v1/invitations/decline", {}, { token: "tok-1" }, "Declined.\n"],
      [
        ["billing", ...KEY],
        "GET",
        "/v1/billing",
        {},
        null,
        "Plan: Developer\nCredits: 4200 remaining of 5000 (800 used).\nPeriod ends: 2026-11-01T00:00:00Z\n",
      ],
      [["billing", "topups", ...KEY], "GET", "/v1/billing/topups", {}, null, "t1  2026-10-05  small  1000 credits  active\n"],
      [["usage", "--history", "--days", "7", ...KEY], "GET", "/v1/usage", { days: "7" }, null, "2026-10-01  12 ops\n"],
      [["usage", "--breakdown", ...KEY], "GET", "/v1/usage/breakdown", {}, null, "s  2 added  3 searches  1 insights\n"],
      [["exports", "list", ...AUTHED], "GET", "/v1/spaces/s/exports", {}, null, "j1  completed  2026-10-01\n"],
      [["exports", "create", ...AUTHED], "POST", "/v1/spaces/s/exports", {}, null, "Export j1 started.\n"],
      [["spaces", "members", "list", "notes", ...KEY], "GET", "/v1/spaces/notes/members", {}, null, "m1  a@x.example  viewer\n"],
      [
        ["routing-settings", "get", ...AUTHED],
        "GET",
        "/v1/spaces/s/routing-settings",
        {},
        null,
        "routing-settings for space 's':\n  charter: (platform default)\n  topics: (platform default)\n  rules: (platform default)\n  auto_route_enabled: True\n  shared_auto_route: False\n",
      ],
      [
        ["routing-settings", "reset", "--yes", ...AUTHED],
        "DELETE",
        "/v1/spaces/s/routing-settings",
        {},
        null,
        "Reset the routing-settings of space 's' to the platform defaults.\n",
      ],
    ];
  };

  it("each verb sends exactly its request and prints its line", async () => {
    for (const [argv, method, path, query, body, text] of cases()) {
      const { seen, out, err } = await sent(argv);
      const label = JSON.stringify(argv);
      expect([seen.method, seen.path], label).toEqual([method, path]);
      expect(seen.query, label).toEqual(query);
      expect(seen.body, label).toEqual(body);
      expect(seen.auth, label).toBe(`Bearer ${SECRET}`);
      expect(out, label).toBe(text);
      expect(err, label).toBe("");
    }
  });

  it("--json passes the document through", async () => {
    for (const [argv, method, path] of cases()) {
      const { out } = await sent([...argv, "--json"]);
      const doc = oneJson(out);
      const canned = bodyOf(method, path);
      const label = JSON.stringify(argv);
      if (canned !== null && canned !== undefined) expect(doc, label).toEqual(canned);
      else expect(typeof doc === "object" && doc !== null, label).toBe(true);
    }
  });

  it("repeated tags and memory types travel as lists", async () => {
    let r = await sent(["models", "list", "--tags", "a,b", ...AUTH()]);
    expect(r.seen.search).toBe("?tags=a&tags=b");
    r = await sent(["models", "create", "n", "--query", "q", "--memory-type", "fact", "--memory-type", "note", ...AUTH()]);
    expect((r.seen.body as { trigger: unknown }).trigger).toEqual({ memory_type: ["fact", "note"] });
  });

  it("a webhook reply without a secret prints no secret lines", async () => {
    routes = [["POST", /^\/v1\/spaces\/s\/webhooks$/, 201, { id: "w-2", url: "https://x.example/h", enabled: true, event_types: [] }], ...ROUTES];
    const { out } = await sent(["webhooks", "create", "https://x.example/h", ...AUTH()]);
    expect(out).toBe("Created webhook w-2.\n");
  });

  const DASHBOARD_ONLY: Array<[string, string, string, string]> = [
    ["keys", "create", "creating an API key", "/dashboard/keys"],
    ["keys", "revoke", "revoking an API key", "/dashboard/keys"],
    ["billing", "upgrade", "upgrading your plan", "/dashboard/billing"],
    ["billing", "checkout", "checking out a plan", "/dashboard/billing"],
    ["billing", "topup", "buying credits", "/dashboard/billing"],
    ["billing", "cancel", "cancelling your subscription", "/dashboard/billing"],
    ["billing", "portal", "managing payment details", "/dashboard/billing"],
    ["org", "rename", "renaming the organization", "/dashboard/profile"],
    ["org", "delete", "deleting the organization", "/dashboard/profile"],
    ["org", "transfer", "transferring the organization", "/dashboard/profile"],
    ["org", "leave", "leaving the organization", "/dashboard/profile"],
    ["members", "invite", "inviting a member", "/dashboard/members"],
    ["members", "remove", "removing a member", "/dashboard/members"],
  ];

  it("dashboard-only verbs send nothing and exit 4", async () => {
    for (const [cmd, act, what, path] of DASHBOARD_ONLY) {
      // `--yes` is accepted and ignored: still nothing sent, still exit 4.
      for (const extra of [[], ["some-arg"], ["--json"], ["--yes"], ["some-arg", "--yes"]]) {
        const before = srv.seen.length;
        const r = await run([cmd as string, act as string, ...extra, ...KEYED()]);
        const label = `${cmd} ${act} ${extra.join(" ")}`;
        expect(r.rc, label).toBe(4);
        expect(r.out, label).toBe("");
        expect(r.err, label).toBe(`anona: ${what} is done in the dashboard: https://memory.anonalabs.com${path}\n`);
        expect(srv.seen.length, label).toBe(before);
      }
    }
    // Nested under `spaces members`; SPACE and MEMBER_ID still parse.
    // With a SPACE the line names that space's page, its id escaped as a path segment.
    const pages: Array<[string[], string]> = [
      [[], ""],
      [["notes", "m1"], "/notes"],
      [["notes", "m1", "--json"], "/notes"],
      [["notes", "m1", "--yes"], "/notes"],
      [["acme:a b/c", "m1"], "/acme%3Aa%20b%2Fc"],
      [["it's(1)*!", "m1"], "/it%27s%281%29%2A%21"],
    ];
    for (const [extra, page] of pages) {
      const before = srv.seen.length;
      const r = await run(["spaces", "members", "remove", ...extra, ...KEYED()]);
      const label = `spaces members remove ${extra.join(" ")}`;
      expect(r.rc, label).toBe(4);
      expect(r.out, label).toBe("");
      expect(r.err, label).toBe(`anona: removing a space member is done in the dashboard: https://memory.anonalabs.com/dashboard/spaces${page}\n`);
      expect(srv.seen.length, label).toBe(before);
    }
    // `projects create` is gone: no dashboard page makes a project.
    const gone = await run(["projects", "create", "Docs", ...KEYED()]);
    expect(gone.rc).toBe(2);
    expect(gone.err).toContain("invalid choice");
    // And with no credential at all: it needs none.
    const bare = await run(["keys", "create"]);
    expect([bare.rc, bare.out]).toEqual([4, ""]);
  });

  it("destructive verbs need --yes", async () => {
    const guarded = [
      ["models", "delete", "mm-1", ...AUTH()],
      ["models", "clear", "mm-1", ...AUTH()],
      ["profile", "reset", ...AUTH()],
      ["rules", "delete", "r-1", ...AUTH()],
      ["webhooks", "delete", "w-1", ...AUTH()],
      ["routing-settings", "reset", ...AUTH()],
    ];
    for (const argv of guarded) expect(await refused(argv), JSON.stringify(argv)).toContain("Pass --yes to do it. Nothing was sent.");
  });

  it("usage mistakes exit 2 and send nothing", async () => {
    const bad = [
      ["models", "edit", "mm-1", ...AUTH()],
      ["rules", "edit", "r-1", ...AUTH()],
      ["rules", "add", "   ", ...AUTH()],
      ["webhooks", "edit", "w-1", ...AUTH()],
      ["profile", "set", ...AUTH()],
      ["profile", "--empathy", "3", ...AUTH()],
      ["graph", "draw", ...AUTH()],
      ["graph", "--model", "m", ...AUTH()],
      ["usage", "--days", "3", ...KEYED()],
      ["usage", "--history", "--breakdown", ...KEYED()],
      ["receipts", "get", "req_1", "--memory-id", "m1", ...KEYED()],
      ["receipts", "get", "req_1", "--explain", ...KEYED()],
      ["chat", " ", ...AUTH()],
      ["ask-user", "alice", " ", ...AUTH()],
      ["models", "create", "n", "--query", "q", "--max-tokens", "10", ...AUTH()],
      ["profile", "set", "--empathy", "9", ...AUTH()],
    ];
    for (const argv of bad) await refused(argv);
  });

  it("profile set with one dial keeps the mission", async () => {
    let before = srv.seen.length;
    let r = await run(["profile", "set", "--empathy", "5", ...AUTH()]);
    expect(r.rc, r.err).toBe(0);
    expect(srv.seen.slice(before).map((s) => `${s.method} ${s.path}`)).toEqual(["GET /v1/spaces/s/profile", "PUT /v1/spaces/s/profile"]);
    expect(srv.seen[srv.seen.length - 1]?.body).toEqual({ mission: "M", disposition: { skepticism: 3, literalism: 2, empathy: 5 } });
    expect(r.out.startsWith("Space: s\nMission: M\n")).toBe(true);
    // And the mission alone keeps the dials.
    r = await run(["profile", "set", "--mission", "New mission.", ...AUTH()]);
    expect(r.rc, r.err).toBe(0);
    expect(srv.seen[srv.seen.length - 1]?.body).toEqual({ mission: "New mission.", disposition: { skepticism: 3, literalism: 2, empathy: 4 } });
    // The bare form is the unchanged read.
    before = srv.seen.length;
    r = await run(["profile", ...AUTH()]);
    expect(r.rc).toBe(0);
    expect(srv.seen.slice(before).map((s) => s.method)).toEqual(["GET"]);
  });

  it("routing-settings set reads, then writes every field", async () => {
    const before = srv.seen.length;
    const r = await run(["routing-settings", "set", "--charter", "Billing", "--topics", "a,b", "--no-auto-route", ...AUTH()]);
    expect(r.rc, r.err).toBe(0);
    expect(srv.seen.slice(before).map((s) => `${s.method} ${s.path}`)).toEqual([
      "GET /v1/spaces/s/routing-settings",
      "PUT /v1/spaces/s/routing-settings",
    ]);
    expect(srv.seen[srv.seen.length - 1]?.body).toEqual({
      charter: "Billing",
      topics: ["a", "b"],
      rules: null,
      auto_route_enabled: false,
      shared_auto_route: false,
    });
    // The switches are booleans the API will not take as null.
    await refused(["routing-settings", "set", "--clear", "auto_route_enabled", ...AUTH()]);
  });

  it("exports download saves a link and a stream without sending the credential on", async () => {
    const dir = process.env.HOME as string;
    mkdirSync(dir, { recursive: true });
    const target = join(dir, "bundle.zip");
    const before = srv.seen.length;
    let r = await run(["exports", "download", "j1", "--out", target, ...AUTH()]);
    expect(r.rc, r.err).toBe(0);
    expect(r.out).toBe(`Saved 7 bytes to ${target}.\n`);
    expect(readFileSync(target, "utf8")).toBe("ZIPDATA");
    const [api, blob] = srv.seen.slice(before);
    expect([api?.path, api?.auth]).toEqual(["/v1/spaces/s/exports/j1/download", `Bearer ${SECRET}`]);
    // The presigned link is fetched bare: the API key must not go to it.
    expect([blob?.path, blob?.auth]).toEqual(["/blob/j1", undefined]);
    expect(existsSync(target + ".part")).toBe(false);
    // A body that is the bundle itself.
    MODE.stream = true;
    const stream = join(dir, "stream.zip");
    r = await run(["exports", "download", "j1", "--out", stream, "--json", ...AUTH()]);
    expect(r.rc, r.err).toBe(0);
    expect(oneJson(r.out)).toEqual({ job_id: "j1", path: stream, bytes: 7 });
    expect(readFileSync(stream, "utf8")).toBe("ZIPDATA");
  });

  it("an expired download link is exit 7, not the storage status mapped as the API's", async () => {
    for (const status of [403, 401, 404]) {
      MODE.blob = status;
      const target = join(process.env.HOME as string, `gone-${status}.zip`);
      const before = srv.seen.length;
      const r = await run(["exports", "download", "j1", "--out", target, ...AUTH()]);
      expect(r.rc, r.err).toBe(7);
      expect(r.out).toBe("");
      expect(r.err).toBe(`anona: download link expired or refused (HTTP ${status}); run download again.\n`);
      expect(srv.seen.slice(before).map((s) => s.path)).toEqual(["/v1/spaces/s/exports/j1/download", "/blob/j1"]);
      expect(existsSync(target)).toBe(false);
    }
  });

  it("exports download refuses to overwrite", async () => {
    const target = join(process.env.HOME as string, "keep.zip");
    writeFileSync(target, "PRECIOUS");
    const err = await refused(["exports", "download", "j1", "--out", target, ...AUTH()]);
    expect(err).toContain("already exists. Pass --yes to overwrite it. Nothing was sent.");
    expect(readFileSync(target, "utf8")).toBe("PRECIOUS");
    const r = await run(["exports", "download", "j1", "--out", target, "--yes", ...AUTH()]);
    expect(r.rc, r.err).toBe(0);
    expect(readFileSync(target, "utf8")).toBe("ZIPDATA");
  });

  it("exports download never writes through a planted link at the old temporary name", async () => {
    const dir = join(process.env.HOME as string, "planted");
    mkdirSync(dir, { recursive: true });
    const victim = join(dir, "victim.txt");
    writeFileSync(victim, "KEEP");
    const target = join(dir, "bundle.zip");
    symlinkSync(victim, target + ".part");
    const r = await run(["exports", "download", "j1", "--out", target, ...AUTH()]);
    expect(r.rc, r.err).toBe(0);
    expect(readFileSync(victim, "utf8")).toBe("KEEP");
    expect(readFileSync(target, "utf8")).toBe("ZIPDATA");
    expect(readdirSync(dir).filter((n) => n.startsWith(".anona-export-"))).toEqual([]);
  });

  it("a download that dies half way is a transport failure and leaves no file behind", async () => {
    const dir = join(process.env.HOME as string, "dying");
    mkdirSync(dir, { recursive: true });
    const target = join(dir, "bundle.zip");
    const r = await run(["exports", "download", "jd", "--out", target, ...AUTH()]);
    expect(r.rc).toBe(7);
    expect(r.out).toBe("");
    expect(r.err).toMatch(/^anona: could not reach 127\.0\.0\.1:\d+: RemoteProtocolError\n$/);
    expect(readdirSync(dir)).toEqual([]);
  });

  it("an unwritable target is write_failed, not a network failure", async () => {
    const r = await run(["exports", "download", "j1", "--out", join(process.env.HOME as string, "no-such-dir", "x.zip"), ...AUTH()]);
    expect(r.rc).toBe(1);
    expect(r.err).toContain("anona: write_failed: could not write ");
  });

  it("session_required is exit 4, with the server asked", async () => {
    MODE.status = 401;
    for (const argv of [["keys", "list"], ["org"], ["billing"], ["usage", "--history"], ["exports", "list", "--space", "s"]]) {
      const before = srv.seen.length;
      const r = await run([...argv, ...KEYED()]);
      const label = JSON.stringify(argv);
      expect(r.rc, label).toBe(4);
      expect(r.out, label).toBe("");
      expect(r.err.startsWith("anona: session_required: "), label).toBe(true);
      expect(r.err, label).toContain("anona login --cli");
      expect(r.err, label).not.toContain("Error:");
      // Not pre-refused: the server was asked, and decides.
      expect(srv.seen.length - before, label).toBe(1);
    }
  });

  it("keys list never prints a secret", async () => {
    const { out } = await sent(["keys", "list", ...KEYED()]);
    expect(out).not.toContain(LEAK);
    expect(out).not.toContain("LEAKEDSECRET");
    expect(out).toBe("anona_live_ab…  ci  2026-10-01  -\n");
    // --json is the API's document as it came.
    const json = await sent(["keys", "list", "--json", ...KEYED()]);
    expect((oneJson(json.out) as Array<{ key: string }>)[0]?.key).toBe(LEAK);
  });

  it("a temporary profile reaches the space verbs", async () => {
    writeMcpFile({ base: srv.base, tempHours: 5 });
    routes = [["GET", /^\/v1\/spaces\/default\/graph$/, 200, { nodes: [], edges: [], total_entities: 0, total_edges: 0 }], ...ROUTES];
    const { seen, out } = await sent(["graph", "--base-url", srv.base]);
    expect([seen.path, seen.auth]).toEqual(["/v1/spaces/default/graph", "Bearer anona_tmp_TEMPTOKEN"]);
    expect(out).toBe("0 entities, 0 links\n");
  });

  it("models edit merges the trigger instead of replacing it", async () => {
    const held = { ...MODEL, trigger: { mode: "delta", refresh_on_new_memories: true, memory_type: ["fact"] } };
    routes = [["GET", /^\/v1\/spaces\/s\/models\/mm-1$/, 200, held], ...ROUTES];
    let before = srv.seen.length;
    let r = await run(["models", "edit", "mm-1", "--no-refresh-on-new-memories", ...AUTH()]);
    expect([r.rc, r.out], r.err).toEqual([0, "Updated memory model mm-1.\n"]);
    expect(srv.seen.slice(before).map((s) => `${s.method} ${s.path}`)).toEqual(["GET /v1/spaces/s/models/mm-1", "PATCH /v1/spaces/s/models/mm-1"]);
    expect(srv.seen[srv.seen.length - 1]?.body).toEqual({ trigger: { mode: "delta", refresh_on_new_memories: false, memory_type: ["fact"] } });
    // A model with no trigger yet: only what was named, nothing invented.
    routes = [["GET", /^\/v1\/spaces\/s\/models\/mm-1$/, 200, MODEL], ...ROUTES];
    before = srv.seen.length;
    r = await run(["models", "edit", "mm-1", "--mode", "delta", "--name", "N", ...AUTH()]);
    expect(r.rc, r.err).toBe(0);
    expect(srv.seen[srv.seen.length - 1]?.body).toEqual({ name: "N", trigger: { mode: "delta" } });
    // No trigger flag, no read.
    before = srv.seen.length;
    await run(["models", "edit", "mm-1", "--name", "N", ...AUTH()]);
    expect(srv.seen.slice(before).map((s) => s.method)).toEqual(["PATCH"]);
  });

  it("models create with a null model_id reply falls back to the flag", async () => {
    routes = [["POST", /^\/v1\/spaces\/s\/models$/, 202, { model_id: null, job_id: "job-9", status: "processing" }], ...ROUTES];
    let r = await sent(["models", "create", "n", "--query", "q", "--model-id", "mine", ...AUTH()]);
    expect(r.out).toBe("Creating memory model mine (job job-9).\n");
    r = await sent(["models", "create", "n", "--query", "q", ...AUTH()]);
    expect(r.out).toBe("Creating memory model - (job job-9).\n");
  });

  it("receipts get --explain needs a memory id, and ask-user and chat reject blank text", async () => {
    expect(await refused(["receipts", "get", "req_1", "--explain", ...KEYED()])).toContain("--explain needs --memory-id.");
    expect(await refused(["receipts", "get", "req_1", "--memory-id", "m1", ...KEYED()])).toContain("--memory-id needs --explain.");
  });
});
