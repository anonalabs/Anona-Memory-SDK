/**
 * The npm `anona` and the pip `anona` must behave the same. This runs both --
 * the Python CLI in a real python3, this one in-process -- against the same
 * local server, with the same credential files, and compares exit code, stdout
 * and stderr byte for byte; and compares `guide`, `guide --json` and the three
 * completion scripts, which are generated from each CLI's own grammar.
 *
 * Normalised out, because they are not behaviour: the argparse `usage:`
 * synopsis above a usage error (the `<prog>: error: <message>` line beneath it
 * is still compared), and, for `doctor`, the runtime's own name, version, path
 * and timings (pip runs on Python, this on Node). Transport failures are not
 * compared: the reason string comes from each runtime's socket layer.
 */
import { execFile } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { g3, pyJson } from "../../src/cli/py.js";
import { iso8601 } from "../../src/cli/args.js";
import { parseConfigEnv } from "../../src/cli/config-env.js";
import { redact } from "../../src/cli/credential.js";
import { jobTuning } from "../../src/cli/jobs.js";
import { run, startDataServer, useDataEnv, writeCliFile, writeConfigEnv, writeMcpFile, type DataServer, type Reply, type Seen } from "./data-helpers.js";

const exec = promisify(execFile);
const here = dirname(fileURLToPath(import.meta.url));
const pyRoot = [resolve(here, "../../../python"), resolve(here, "../../..")].find((p) => existsSync(resolve(p, "anona/cli/memory.py")));
const realUserBase = process.env.PYTHONUSERBASE ?? `${process.env.HOME}/.local`;
const BODIES = JSON.parse(readFileSync(join(here, "fixtures", "gateway-bodies.json"), "utf8"));

let pythonOk = false;
try {
  if (pyRoot) {
    await exec("python3", ["-c", "import anona.cli.main, anona.cli.guide, httpx"], { env: { ...process.env, PYTHONPATH: pyRoot } });
    pythonOk = true;
  }
} catch {
  /* skipped below */
}
if (!pythonOk && process.env.CI) throw new Error("CI must be able to run the CLI parity test (python3 + anona + httpx).");

const env = (extra: Record<string, string> = {}) => {
  const e: Record<string, string> = {};
  for (const [k, v] of Object.entries(process.env)) if (v !== undefined) e[k] = v;
  return { ...e, ...extra, PYTHONUSERBASE: realUserBase, PYTHONPATH: pyRoot ?? "", PYTHONIOENCODING: "utf-8" };
};

/** Run python3 with a JSON payload on stdin; returns its parsed JSON stdout. */
async function py(code: string, payload: unknown, extraEnv: Record<string, string> = {}): Promise<any> {
  const child = execFile("python3", ["-c", code], { env: env(extraEnv), maxBuffer: 64 * 1024 * 1024 });
  child.stdin?.end(JSON.stringify(payload));
  const out: string[] = [];
  child.stdout?.on("data", (c) => out.push(String(c)));
  const errs: string[] = [];
  child.stderr?.on("data", (c) => errs.push(String(c)));
  const code_ = await new Promise<number>((r) => child.on("close", (c) => r(c ?? 1)));
  if (code_ !== 0) throw new Error(`python3 failed: ${errs.join("")}`);
  return JSON.parse(out.join(""));
}

// Each case: [argv, stdin]. Every case starts from the same environment: a
// --profile in one must not leak into the next.
const RUN_MANY = `
import io, json, os, sys
from contextlib import redirect_stdout, redirect_stderr
from anona.cli import main as m, jobs
jobs.FIRST_INTERVAL = 0.01
jobs.MAX_INTERVAL = 0.01
base_profile = os.environ.get("ANONA_PROFILE")
results = []
for argv, stdin in json.load(sys.stdin):
    if base_profile is None:
        os.environ.pop("ANONA_PROFILE", None)
    else:
        os.environ["ANONA_PROFILE"] = base_profile
    out, err = io.StringIO(), io.StringIO()
    old = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = m.main(argv)
            except SystemExit as e:
                rc = e.code
    finally:
        sys.stdin = old
    results.append([rc, out.getvalue(), err.getvalue()])
print(json.dumps(results))
`;

/** Drop argparse's `usage:` synopsis (and its continuation lines) above an error. */
function normalise(err: string): string {
  const kept: string[] = [];
  let inUsage = false;
  for (const l of err.split("\n")) {
    if (l.startsWith("usage: ")) {
      inUsage = true;
      continue;
    }
    if (inUsage && l.startsWith(" ")) continue;
    inUsage = false;
    kept.push(l);
  }
  // Each package names its own version and upgrade command on an unknown
  // command; the rest of the message is the same.
  return kept
    .join("\n")
    .replace(
      /this copy \(anona [^)]*\) is older than they are: (?:pip install -U anona|npm install -g @anona-labs\/memory)/g,
      "this copy (anona V) is older than they are: UPGRADE",
    );
}

/** doctor: the runtime is legitimately different; everything else is compared. */
function normaliseDoctor(text: string): string {
  return text
    .replace(/^anona \S+ on (Python|Node) .*$/m, "anona VERSION on RUNTIME")
    .replace(/HTTP 200 in [0-9.e-]+ s/g, "HTTP 200 in T s")
    .replace(/"anona_version": "[^"]*"/, '"anona_version": "V"')
    .replace(/"(python|node)": "[^"]*"/, '"runtime": "R"')
    .replace(/"executable": "[^"]*"/, '"executable": "E"')
    .replace(/"package_path": "[^"]*"/, '"package_path": "P"')
    .replace(/"seconds": [0-9.e-]+/, '"seconds": 0')
    .replace(/"expires_in_s": -?\d+/, '"expires_in_s": 0');
}

const usage = { input_tokens: 1, output_tokens: 1, total_tokens: 2 };
const EVIL = "x\x1b[2K\x1b[1G\rspoofed\x9b31m\x07\u2028\u202e é";
const envelope = (status: number, code: string, message = "m"): Reply => ({ status, body: { error: { code, message, request_id: "req_1" } } });
const JOB_FOR: Record<string, unknown> = {
  "job-done": BODIES.job_completed,
  "job-empty": BODIES.job_completed_empty,
  "job-fail": BODIES.job_failed,
  "job-run": BODIES.job_processing,
};

function spaceOf(s: Seen): string {
  return (s.body && typeof s.body === "object" && "space_id" in s.body ? s.body.space_id : decodeURIComponent(s.path.split("/")[3] ?? "")) as string;
}

function respond(s: Seen): Reply {
  if (s.path === "/health") return { status: 200, body: { status: "ok" } };
  const space = spaceOf(s);
  const evil = space === "hostile";
  const tag = (v: string) => (evil ? v + EVIL : v);
  if (space === "e401") return envelope(401, "invalid_api_key", "Invalid API key.");
  if (space === "e403") return envelope(403, "space_owner_only");
  if (space === "e404") return envelope(404, "space_not_found");
  if (space === "e415") return envelope(415, "unsupported_file_type", "Not that.");
  if (space === "e429") return { status: 429, body: { error: { code: "credits_exhausted", message: "Out." } } };
  if (space === "e409") return envelope(409, "job_not_cancellable", "The job has finished; there is nothing left to cancel.");
  if (space === "e422") {
    return {
      status: 422,
      body: { error: { code: "validation_error", message: "Request validation failed", errors: [{ loc: ["body", "content"], msg: "too short" }], request_id: "req_1" } },
    };
  }
  if (space === "e500") return { status: 500, body: { error: { code: "internal_error", message: "Something broke." } }, headers: { "retry-after": "0" } };
  if (space === "eraw") return { status: 502, raw: "<html>bad \x1b[2J gateway</html>" };
  if (space === "edetail") return { status: 404, body: { detail: "Not Found" } };
  if (space === "eweird") return { status: 400, body: { zeta: 1, alpha: [2, "é"] } };
  const more = respondResources(s, space, tag);
  if (more) return more;
  const jobMatch = /\/jobs\/([^/]+)$/.exec(s.path);
  if (jobMatch && s.method === "GET") return { status: 200, body: { ...(JOB_FOR[jobMatch[1] as string] ?? BODIES.job_completed), job_id: jobMatch[1] } };
  if (s.path === "/v1/record") {
    const c = s.body.content;
    if (s.body.async) {
      const job = c.startsWith("async-") ? `job-${c.slice(6)}` : "job-done";
      return { status: 202, body: { ...BODIES.record_async, job_id: job } };
    }
    if (c === "nothing") return { status: 201, body: { memory_id: null, memory_ids: [], job_id: null, status: "nothing_extracted", message: tag("Nothing stored."), usage, routed_to: null } };
    if (c === "old") return { status: 201, body: { memory_id: null, memory_ids: null, job_id: null, status: "stored", message: null, usage, routed_to: null } };
    if (c === "queued") return { status: 201, body: { memory_id: null, memory_ids: null, job_id: "j1", status: "processing", message: null, usage, routed_to: null } };
    return { status: 201, body: { memory_id: "m1", memory_ids: [tag("m1"), "m2"], job_id: null, status: "stored", message: null, usage, routed_to: null } };
  }
  if (s.path === "/v1/record/batch") {
    const first = s.body.items?.[0]?.content as string;
    const ids = first === "empty" ? ["job-empty"] : ["job-done", "job-done"];
    return { status: 202, body: { ...BODIES.record_batch, job_id: ids[0], job_ids: ids } };
  }
  if (s.path === "/v1/retrieve") {
    const q = s.body.query;
    if (s.body.format === "block") return { status: 200, body: q === "none" ? { ...BODIES.search_block, context: "", results: [] } : BODIES.search_block };
    if (q === "none") return { status: 200, body: { results: [], usage, context: null, token_estimate: null, receipt_id: null } };
    const results = [
      { memory_id: "3f2a9c1e-0000-4000-8000-000000000001", content: tag("the user prefers pnpm"), relevance_score: q === "true-score" ? true : 1.0978, memory_type: "world", entities: [] },
      { memory_id: null, content: null, relevance_score: 0.00006, memory_type: "observation", entities: [] },
      { memory_id: "9b7d", content: "builds", relevance_score: 123456.0, memory_type: null, entities: [] },
    ];
    return { status: 200, body: { results, usage, context: null, token_estimate: null, receipt_id: null, searched: q === "routed" ? [{ space_id: "x", confidence: 0.5 }] : undefined } };
  }
  if (s.path === "/v1/reason") {
    const q = s.body.query;
    const insights =
      q === "blank" ? "   " : q === "none" ? null : q === "dict" ? { b: 1, a: ["é"] } : q === "list" ? [1, "two"] : q === "num" ? 42 : q === "false" ? false : tag("First paragraph.\r\n\n\tSecond.");
    return { status: 200, body: { job_id: null, status: null, insights, usage, sources: { models: ["mm1"], memories: 2, notes: 1 }, rules_applied: [], model: "m-x" } };
  }
  if (s.path === "/v1/models") return { status: 200, body: BODIES.llm_models };
  const item = { id: tag("mem-1"), content: tag("The user prefers pnpm."), text: "t", context: null, date: null, type: "observation", entities: null, state: "active", metadata: null, source_ids: [], tags: [] };
  if (s.path === "/v1/spaces/" && s.method === "GET") {
    return { status: 200, body: { spaces: [{ space_id: "notes", name: "notes", description: tag("Team notes"), created_at: null, shared_by: null, qualified_id: null }, { space_id: "demo", name: "demo", description: null, created_at: null, shared_by: "Acme", qualified_id: "acme:demo" }], total: 2 } };
  }
  if (s.path === "/v1/spaces/" && s.method === "POST") return { status: 201, body: { space_id: s.body.name, name: s.body.name, description: null, created_at: null, shared_by: null, qualified_id: null, space_type: "general" } };
  if (s.path === "/v1/usage/me") return { status: 200, body: { credits_remaining: 4200, credits_limit: 5000, credits_used: 800, rate_limit_per_min: 60, credits_banked: 25 } };
  if (/^\/v1\/spaces\/[^/]+$/.test(s.path)) {
    if (s.method === "DELETE") return { status: 204 };
    return { status: 200, body: { space_id: "notes", name: "notes", description: tag("d"), created_at: "2026-10-01T00:00:00Z", shared_by: null, qualified_id: null } };
  }
  const settings = /\/(chat-settings|reason-settings|extraction-settings)$/.exec(s.path);
  if (settings) {
    const kind = (settings[1] as string).replace("-", "_");
    if (s.method === "GET") return { status: 200, body: BODIES[kind] };
    if (s.method === "PUT") return { status: 200, body: { ...s.body, space_id: space } };
    return { status: 204 };
  }
  if (s.path.endsWith("/documents") && s.method === "POST") return { status: 202, body: BODIES.upload };
  if (s.path.endsWith("/documents")) return { status: 200, body: BODIES.document_list };
  if (s.path.includes("/documents/") && s.method === "DELETE") return { status: 204 };
  if (s.path.includes("/documents/")) return { status: 200, body: { id: "doc-1", text_length: 12, tags: ["q3"], source: tag("file"), memory_count: 3 } };
  if (s.path.endsWith("/memories")) {
    // A JSON `true` is not an offset of 1.
    return { status: 200, body: { items: [item, { ...item, id: null, content: "" }], total: 9, limit: 50, offset: space === "booloff" ? true : 4 } };
  }
  if (s.path.endsWith("/history")) return { status: 200, body: { memory_id: "mem-1", history: [{ previous_content: tag("old"), changed_at: "2026-10-02T00:00:00Z", previous_occurred_start: null, previous_occurred_end: null, reason: null }] } };
  if (s.method === "PATCH") return { status: 200, body: item };
  if (s.method === "DELETE" && s.path.includes("/memories/")) return { status: 204 };
  if (s.method === "DELETE") return { status: 200, body: { job_id: "j1", status: "cancelled", cancelled_parts: 0, running_parts: 3 } };
  if (s.path.endsWith("/users/alice/profile")) return { status: 200, body: { space_id: "s", user_id: "alice", memory_count: true, first_seen: null, last_active: null, memories: [item], context: null, token_estimate: null } };
  if (s.path.endsWith("/profile")) return { status: 200, body: { space_id: "s", name: "s", mission: tag("Remember."), disposition: { skepticism: 3, literalism: 2.5, empathy: 4 } } };
  if (s.path.includes("/models/")) return { status: 200, body: { id: "mm1", space_id: "s", name: tag("Habits"), query: null, content: s.path.endsWith("empty") ? "  " : tag("Uses pnpm.\n\nShips."), tags: [], max_tokens: null, sources: [] } };
  return { status: 404, body: { error: { code: "not_found", message: "No route." } } };
}


// The commands that cover the rest of the API: fixtures for each route, so both
// CLIs are shown the same replies. A key naming ERRSESSION is refused the way
// the gateway refuses an API key on an account route.
let serverBase = "";
const RESOURCE_PATH = [
  /^\/v1\/orgs/,
  /^\/v1\/projects/,
  /^\/v1\/api-keys/,
  /^\/v1\/members/,
  /^\/v1\/invitations\//,
  /^\/v1\/billing/,
  /^\/v1\/usage$/,
  /^\/v1\/usage\/breakdown$/,
  /^\/v1\/spaces\/[^/]+\/members(\/|$)/,
];

function respondResources(s: Seen, space: string, tag: (v: string) => string): Reply | undefined {
  const { method, path } = s;
  const none = (status = 204): Reply => ({ status });
  if (s.auth?.includes("ERRSESSION") && RESOURCE_PATH.some((re) => re.test(path))) {
    return envelope(401, "session_required", "This needs a dashboard session or `anona login --cli`; an API key or temporary profile cannot use it.");
  }
  const model = { id: tag("mm-1"), space_id: space, name: tag("Build habits"), query: tag("How does this team build?"), content: tag("Uses pnpm.\n\nShips."), tags: [] };
  const job = { model_id: "mm-1", job_id: "job-9", status: "processing" };
  if (/^\/v1\/spaces\/[^/]+\/models/.test(path)) {
    if (path.endsWith("/models")) return method === "GET" ? { status: 200, body: { items: [model, { ...model, id: "mm-2", name: null }], total: 2, limit: 50, offset: 0 } } : { status: 202, body: job };
    if (path.endsWith("/history")) return { status: 200, body: { entries: [{ previous_content: tag("x".repeat(100)), changed_at: "2026-10-02T00:00:00Z" }, { previous_content: null, changed_at: null }], total: 2 } };
    if (path.endsWith("/refresh")) return { status: 202, body: job };
    if (path.endsWith("/clear")) return { status: 200, body: model };
    if (method === "DELETE") return none();
    const delta = { ...model, trigger: { mode: "delta", refresh_on_new_memories: true, memory_type: ["fact"] } };
    return { status: 200, body: method === "GET" && path.endsWith("mm-delta") ? delta : model };
  }
  if (/^\/v1\/spaces\/[^/]+\/profile$/.test(path) && method !== "GET") {
    return method === "DELETE" ? none() : { status: 200, body: { space_id: space, name: space, mission: s.body.mission, disposition: s.body.disposition } };
  }
  if (/^\/v1\/spaces\/[^/]+\/rules/.test(path)) {
    if (method === "GET") return { status: 200, body: { items: [{ id: "r-1", space_id: space, name: "n", content: tag("Never X.") }, { id: null, name: "z", content: "" }], total: 2 } };
    if (method === "DELETE") return none();
    return { status: method === "POST" ? 201 : 200, body: { id: "r-1", space_id: space, name: "n", content: "Never X." } };
  }
  if (/^\/v1\/spaces\/[^/]+\/webhooks/.test(path)) {
    if (path.endsWith("/deliveries")) {
      return {
        status: 200,
        body: {
          items: [
            { id: "d-1", event_type: tag("memory.created"), status: "completed", attempts: 1, delivered_at: "2026-10-03T00:00:00Z" },
            { id: "d-2", event_type: "memory.consolidated", status: "failed", attempts: 3, delivered_at: null, last_attempt_at: "2026-10-04T00:00:00Z" },
          ],
          next_cursor: space === "s" ? "c2" : null,
        },
      };
    }
    if (method === "GET") {
      return { status: 200, body: { items: [{ id: "w-1", url: tag("https://x.example/h"), enabled: true, event_types: ["memory.created", "memory.consolidated"] }, { id: "w-2", url: "https://y.example", enabled: false, event_types: [] }] } };
    }
    if (method === "DELETE") return none();
    const hook = { id: "w-1", url: "https://x.example/h", enabled: true, event_types: ["memory.created"] };
    if (method === "POST") return { status: 201, body: s.body.url === "https://nosecret.example" ? hook : { ...hook, secret: "whsec_PARITY" } };
    return { status: 200, body: hook };
  }
  if (/^\/v1\/spaces\/[^/]+\/graph$/.test(path)) {
    if (method === "POST") {
      const q = s.body.request;
      if (q === "nothing") return { status: 200, body: { text: null, mermaid: null, diagram: null } };
      if (q === "mermaid") return { status: 200, body: { text: tag("Here."), mermaid: "graph TD;\n  A-->B", diagram: null } };
      if (q === "diagram") return { status: 200, body: { text: null, mermaid: null, diagram: "flowchart" } };
      return { status: 200, body: { text: tag("Here is the chart."), mermaid: null } };
    }
    const nodes = Array.from({ length: 52 }, (_, i) => ({ id: `e${i}`, label: i === 0 ? tag("Alice") : `n${i}`, mention_count: i + 1 }));
    return { status: 200, body: { nodes, edges: [], total_entities: 52, total_edges: 0 } };
  }
  if (/^\/v1\/spaces\/[^/]+\/entities/.test(path)) {
    if (path.endsWith("/entities")) return { status: 200, body: { items: [{ id: tag("e1"), name: tag("Alice"), mention_count: 3 }, { id: "e2", name: null, type: "person" }], total: 2, limit: 100, offset: 0 } };
    return { status: 200, body: { id: "e1", name: tag("Alice"), mention_count: 3, observations: space === "empty" ? [] : [{ text: tag("Likes tea."), mentioned_at: "2026-10-01" }] } };
  }
  if (/^\/v1\/receipts\//.test(path)) {
    if (path.endsWith("/explain")) return { status: 200, body: { memory_id: "m1", outcome: tag("excluded"), stage: "budget", detail: s.query.memory_id === "m-bare" ? null : tag("over budget") } };
    return { status: 200, body: { request_id: "req_1", space_id: tag("s"), included: [{ id: "a" }], excluded: [{ id: "b" }, { id: "c" }], token_accounting: { used: 40, budget: 100, nested: { a: 1 }, ok: true } } };
  }
  if (/^\/v1\/spaces\/[^/]+\/users\/[^/]+\/ask$/.test(path)) {
    const q = s.body.query;
    return { status: 200, body: { space_id: space, user_id: "alice", insights: q === "blank" ? "  " : q === "none" ? null : tag("She likes tea.\r\n\nA lot.") } };
  }
  if (path === "/v1/space-types") return { status: 200, body: { items: [{ space_type: "general", name: tag("General") }, { space_type: "support", name: "Support" }], total: 2 } };
  if (path === "/v1/chat/completions") {
    const m = s.body.messages?.[0]?.content;
    const content = m === "empty" ? null : tag("Hello!\n\nBye.");
    return { status: 200, body: { id: "c1", choices: m === "nochoice" ? [] : [{ message: { role: "assistant", content } }], usage: {} } };
  }
  if (path === "/v1/orgs/me") return { status: 200, body: { org_id: "o1", name: tag("Acme"), slug: "acme", plan_id: s.auth?.includes("NOPLAN") ? null : "developer" } };
  if (path === "/v1/orgs") return { status: 200, body: { orgs: [{ org_id: "o1", name: tag("Acme"), role: "owner" }, { id: "o2", name: "Beta", role: "viewer" }], max_orgs: 3 } };
  if (path === "/v1/projects") return { status: 200, body: [{ project_id: "p1", org_id: "o1", name: tag("Docs") }, { id: "p2", name: "Site" }] };
  if (path === "/v1/api-keys") {
    return {
      status: 200,
      body: [
        { id: "k1", name: tag("ci"), key_prefix: "anona_live_ab", env: "live", created_at: "2026-10-01", last_used_at: null, status: "active", key: "anona_live_LEAKEDLEAKED" },
        { id: "k2", name: "x", key_prefix: "anona_test_" + "z".repeat(40), created_at: "2026-10-02", last_used_at: "2026-10-03", status: "revoked" },
      ],
    };
  }
  if (path === "/v1/members") return { status: 200, body: [{ member_id: "m1", email: tag("a@x.example"), role: "owner" }, { id: "m2", email: "b@x.example", role: "viewer" }] };
  if (path === "/v1/members/me") return { status: 200, body: { member_id: "m1", email: "a@x.example", role: "owner" } };
  if (path === "/v1/members/invitations") return { status: 200, body: { invitations: space === "x" ? [] : [{ invitation_id: "i1", email: "b@x.example", role: "developer" }] } };
  if (path === "/v1/invitations/pending") {
    return {
      status: 200,
      body: { invitations: [{ token: "tok-1", kind: "space", org_name: "Acme", space_id: tag("notes"), role: "viewer" }, { token: "tok-2", kind: "org", org_name: "Beta", space_id: null, role: "developer" }] },
    };
  }
  if (path === "/v1/invitations/accept") return { status: 200, body: s.body.token === "tok-2" ? { org_id: "o2", org_name: tag("Beta"), space_id: null } : { org_id: "o1", space_id: null } };
  if (path === "/v1/invitations/decline") return none();
  if (path === "/v1/billing") {
    return {
      status: 200,
      body: { plan_id: "developer", plan_name: tag("Developer"), credits_limit: 5000, credits_used: 800, credits_remaining: 4200, credits_banked: s.auth?.includes("BANKED") ? 25 : 0, credits_reset_at: "2026-11-01T00:00:00Z" },
    };
  }
  if (path === "/v1/billing/topups") return { status: 200, body: { items: [{ id: "t1", purchased_at: "2026-10-05", pack: "small", credits: 1000, status: tag("active") }] } };
  if (path === "/v1/usage") return { status: 200, body: s.query.days === "1" ? { series: [], period_days: 1 } : { series: [{ date: "2026-10-01", ops: 12 }, { date: "2026-10-02", ops: 0 }], period_days: 7 } };
  if (path === "/v1/usage/breakdown") return { status: 200, body: s.query.days === "1" ? { breakdown: [], period_days: 1 } : { breakdown: [{ space_id: tag("s"), memories_added: 2, searches: 3, insights: 1 }], period_days: 30 } };
  if (/^\/v1\/spaces\/[^/]+\/exports\/[^/]+\/download$/.test(path)) {
    const job = path.split("/")[5];
    if (job === "j-link") return { status: 200, body: { download_url: serverBase + "/blob/export.zip", expires_in: 300 } };
    if (job === "j-relative") return { status: 200, body: { download_url: "/blob/export.zip" } };
    if (job === "j-stream") return { status: 200, raw: "ZIPDATA-STREAM", headers: { "content-type": "application/zip" } };
    if (job === "j-redirect") return { status: 302, headers: { location: serverBase + "/blob/export.zip" } };
    if (job === "j-nolink") return { status: 200, body: { expires_in: 300 } };
    if (job === "j-badlink") return { status: 200, body: { download_url: "ftp://x.example/export.zip" } };
    if (job === "j-deadlink") return { status: 200, body: { download_url: serverBase + "/blob/gone.zip" } };
    if (job === "j-slow") return { status: 200, body: { download_url: serverBase + "/blob/slow.zip" } };
    if (job === "j-die") return { status: 200, body: { download_url: serverBase + "/blob/die.zip" } };
    if (job === "j-refused") return { status: 200, body: { download_url: "http://127.0.0.1:1/blob/x.zip" } };
    return envelope(404, "export_not_found", "No such export.");
  }
  if (path === "/blob/export.zip") return { status: 200, raw: "ZIPDATA-LINK", headers: { "content-type": "application/zip" } };
  if (path === "/blob/gone.zip") return { status: 403, raw: "denied" };
  // Steady but slow: longer in all than a small --timeout, never silent for that long.
  if (path === "/blob/slow.zip") return { status: 200, stream: { chunks: ["aaaa", "bbbb", "cccc", "dddd", "eeee", "ffff"], delayMs: 150 } };
  // Promises 100 bytes, sends 10, and drops the connection.
  if (path === "/blob/die.zip") return { status: 200, stream: { chunks: ["abcde", "fghij"], delayMs: 20, length: 100, die: true } };
  if (/^\/v1\/spaces\/[^/]+\/exports$/.test(path)) {
    if (method === "POST") return { status: 202, body: { job_id: "j1", space_id: space, status: "pending" } };
    return { status: 200, body: { items: space === "empty" ? [] : [{ job_id: "j1", status: "completed", created_at: "2026-10-01" }, { job_id: tag("j2"), status: "pending", created_at: null }], total: 2 } };
  }
  if (/^\/v1\/spaces\/[^/]+\/members\/[^/]+$/.test(path) && method === "DELETE") return none();
  if (/^\/v1\/spaces\/[^/]+\/members$/.test(path)) {
    return { status: 200, body: space === "empty" ? [] : [{ member_id: "m1", email: tag("a@x.example"), role: "viewer", status: "active" }, { id: "m2", email: "b@x.example", role: "developer" }] };
  }
  if (/^\/v1\/spaces\/[^/]+\/routing-settings$/.test(path)) {
    const stored = { space_id: space, charter: tag("Billing"), topics: ["a", "b"], rules: [{ field: "agent_id", op: "equals", value: "x", space_id: "y" }], auto_route_enabled: true, shared_auto_route: false };
    if (method === "GET") return { status: 200, body: space === "empty" ? { ...stored, charter: null, topics: null, rules: null } : stored };
    if (method === "PUT") return { status: 200, body: { ...s.body, space_id: space } };
    return none();
  }
  return undefined;
}

type Case = string[] | [string[], string];

// Each case runs both CLIs; a few hundred of them outlast the 5s default.
describe.skipIf(!pythonOk)("npm and pip CLIs: same exit code, stdout and stderr", { timeout: 60_000 }, () => {
  useDataEnv();
  let srv: DataServer;
  beforeAll(async () => {
    srv = await startDataServer(respond);
    serverBase = srv.base;
    jobTuning.firstInterval = 0.01;
    jobTuning.maxInterval = 0.01;
  });
  afterAll(() => {
    jobTuning.firstInterval = 1;
    jobTuning.maxInterval = 5;
    return srv.close();
  });

  async function compare(cases: Case[], opts: { env?: Record<string, string>; doctor?: boolean } = {}): Promise<void> {
    const norm: Array<[string[], string]> = cases.map((c) => (Array.isArray(c[0]) ? (c as [string[], string]) : [c as string[], ""]));
    const python: Array<[number, string, string]> = await py(RUN_MANY, norm, opts.env);
    const saved: Record<string, string | undefined> = {};
    for (const [k, v] of Object.entries(opts.env ?? {})) {
      saved[k] = process.env[k];
      process.env[k] = v;
    }
    try {
      for (let i = 0; i < norm.length; i++) {
        const [argv, stdin] = norm[i] as [string[], string];
        const [prc, pout, perr] = python[i] as [number, string, string];
        const n = await run(argv, stdin);
        const label = JSON.stringify(argv);
        const fix = opts.doctor ? normaliseDoctor : (x: string) => x;
        expect(n.rc, `${label}\nnpm: ${n.err}\npip: ${perr}`).toBe(prc);
        expect(fix(n.out), label).toBe(fix(pout));
        expect(fix(normalise(n.err)), label).toBe(fix(normalise(perr)));
      }
    } finally {
      for (const [k, v] of Object.entries(saved)) {
        if (v === undefined) delete process.env[k];
        else process.env[k] = v;
      }
    }
  }

  const K = () => ["--base-url", srv.base, "--api-key", "anona_live_PARITYKEYPARITYKEY"];
  const S = (space: string) => ["--space", space, ...K()];

  it("record, retrieve, reason and the original resource verbs", async () => {
    const cases: Case[] = [];
    for (const json of [[], ["--json"]]) {
      cases.push(
        ["record", "x", ...S("s"), ...json],
        ["record", "nothing", ...S("s"), ...json],
        ["record", "nothing", ...S("hostile"), ...json],
        ["record", "old", ...S("s"), ...json],
        ["record", "queued", ...S("s"), ...json],
        ["record", "x", ...S("hostile"), ...json],
        ["record", "x", "--tags", "a,b", "--tags", "c", "--metadata", '{"k": [1, "é"]}', "--timestamp", "2026-06-01", "--user-id", "u", ...S("s"), ...json],
        ["retrieve", "q", ...S("s"), ...json],
        ["retrieve", "q", ...S("hostile"), ...json],
        ["retrieve", "none", ...S("s"), ...json],
        ["retrieve", "routed", ...S("s"), ...json],
        ["retrieve", "true-score", ...S("s"), ...json],
        ["retrieve", "q", "--limit", "5", "--min-score", "0.5", "--tags", "a", "--tags-match", "all", "--as-of", "2026-01-01T00:00:00Z", ...S("s"), ...json],
        ["reason", "q", ...S("s"), ...json],
        ["reason", "q", ...S("hostile"), ...json],
        ["reason", "blank", ...S("s"), ...json],
        ["reason", "none", ...S("s"), ...json],
        ["reason", "dict", ...S("s"), ...json],
        ["reason", "list", ...S("s"), ...json],
        ["reason", "num", ...S("s"), ...json],
        ["reason", "false", ...S("s"), ...json],
        ["reason", "q", "--depth", "thorough", "--user-id", "u", ...S("s"), ...json],
        ["record", "x", ...S("e401"), ...json],
        ["record", "x", ...S("e403"), ...json],
        ["record", "x", ...S("e404"), ...json],
        ["record", "x", ...S("e422"), ...json],
        ["record", "x", ...S("e429"), ...json],
        ["record", "x", ...S("e500"), ...json],
        ["record", "x", ...S("eraw"), ...json],
        ["record", "x", ...S("edetail"), ...json],
        ["record", "x", ...S("eweird"), ...json],
        ["reason", "q", ...S("e422"), ...json],
        ["spaces", "list", ...K(), ...json],
        ["spaces", "create", "fresh", "--description", "d", ...K(), ...json],
        ["spaces", "get", "notes", ...K(), ...json],
        ["usage", ...K(), ...json],
        ["memories", "list", "--limit", "5", "--no-prefer-observations", ...S("s"), ...json],
        ["memories", "list", ...S("hostile"), ...json],
        ["memories", "list", ...S("e500"), ...json],
        ["memories", "edit", "mem-1", "--text", "new", "--reason", "r", ...S("s"), ...json],
        ["memories", "delete", "mem-1", "--yes", ...S("s"), ...json],
        ["memories", "delete", "mem-1", ...S("s"), ...json],
        ["memories", "history", "mem-1", ...S("hostile"), ...json],
        ["jobs", "get", "job-fail", ...S("hostile"), ...json],
        ["jobs", "cancel", "j1", "--yes", ...S("s"), ...json],
        ["jobs", "cancel", "j1", ...S("s"), ...json],
        ["jobs", "cancel", "j1", "--yes", ...S("e409"), ...json],
        ["profile", ...S("hostile"), ...json],
        ["model", "mm1", ...S("hostile"), ...json],
        ["model", "empty", ...S("s"), ...json],
        ["user-profile", "alice", "--limit", "3", ...S("s"), ...json],
        ["memories", "list", ...S("booloff"), ...json],
        // A space name is caller text that reaches the terminal in messages.
        ["record", "x", ...S("evil\x1b[2Jname\u202e"), ...json],
        ["record", "queued", ...S("evil\x1b[2Jname"), ...json],
        ["record", "async-done", "--async", ...S("evil\x1b[2Jname"), ...json],
        ["reason", "q", ...S("evil\x1b[2Jname"), ...json],
      );
    }
    await compare(cases);
  });

  it("the new verbs: async, batch, wait, compound filters, block, documents, settings, llm-models", async () => {
    const home = process.env.HOME as string;
    const file = (name: string, text: string) => {
      writeFileSync(join(home, name), text);
      return join(home, name);
    };
    const items = file("items.jsonl", '{"content": "Alice owns billing.", "tags": ["team:billing"]}\n\n"Bob owns search."\n');
    const empty = file("empty.jsonl", '"empty"\n');
    const groups = '[{"or": [{"tags": ["project:alpha"]}, {"tags": ["project:beta"]}]}, {"not": {"tags": ["status:archived"]}}]';
    const groupsFile = file("groups.json", groups);
    const labels = file("labels.json", '[{"key": "name", "type": "multi-text", "tag": true}]');
    const guidance = file("guidance.txt", "Name the customer.\nAlways.");
    const a = file("a.md", "# Alpha notes");
    const b = file("b.txt", "Beta notes");
    const cases: Case[] = [];
    for (const json of [[], ["--json"]]) {
      cases.push(
        ["record", "async-done", "--async", ...S("s"), ...json],
        ["record", "async-done", "--async", "--wait", ...S("s"), ...json],
        ["record", "async-empty", "--async", "--wait", ...S("s"), ...json],
        ["record", "async-fail", "--async", "--wait", ...S("s"), ...json],
        ["record", "async-run", "--async", "--wait", "--wait-timeout", "0", ...S("s"), ...json],
        ["record", "async-done", "--async", "--wait", ...S("hostile"), ...json],
        ["record", "--batch", items, "--user-id", "u1", ...S("s"), ...json],
        ["record", "--batch", items, "--wait", ...S("s"), ...json],
        ["record", "--batch", empty, "--wait", ...S("s"), ...json],
        [["record", "--batch", "-", ...S("s"), ...json], '"one"\n"two"\n'],
        ["retrieve", "ship", "--tag-groups", groups, "--member-id", "me", "--no-prefer-observations", "--user-id", "u", ...S("s"), ...json],
        ["retrieve", "ship", "--tag-groups", '{"tags": ["a"], "match": "any"}', "--prefer-observations", ...S("s"), ...json],
        ["retrieve", "ship", "--tag-groups", "@" + groupsFile, ...S("s"), ...json],
        ["retrieve", "ship", "--format", "block", "--max-tokens", "500", "--tags", "a", ...S("s"), ...json],
        ["retrieve", "none", "--format", "block", ...S("s"), ...json],
        ["memories", "list", "--member-id", "me", "--tag-groups", groups, ...S("s"), ...json],
        ["spaces", "delete", "old", "--yes", ...K(), ...json],
        ["spaces", "delete", "e403", "--yes", ...K(), ...json],
        ["documents", "upload", a, b, "--user-id", "u1", "--tags", "q3,notes", ...S("s"), ...json],
        ["documents", "upload", a, "--wait", ...S("s"), ...json],
        ["documents", "upload", a, ...S("e415"), ...json],
        ["documents", "list", "--source", "file", ...S("s"), ...json],
        ["documents", "list", ...S("hostile"), ...json],
        ["documents", "get", "doc-1", ...S("hostile"), ...json],
        ["documents", "delete", "doc-1", "--yes", ...S("s"), ...json],
        ["reason-settings", "set", "--model", "fast", ...S("s"), ...json],
        ["reason-settings", "set", "--clear", "depth", ...S("s"), ...json],
        ["chat-settings", "set", "--memory-limit", "3", "--no-auto-record", ...S("s"), ...json],
        ["extraction-settings", "set", "--clear", "guidance", "--labels-file", labels, ...S("s"), ...json],
        ["extraction-settings", "set", "--guidance", "@" + guidance, "--mode", "custom", ...S("s"), ...json],
        ["llm-models", ...K(), ...json],
      );
      for (const kind of ["chat-settings", "reason-settings", "extraction-settings"]) {
        cases.push([kind, "get", ...S("s"), ...json], [kind, "reset", "--yes", ...S("s"), ...json], [kind, "get", ...S("e404"), ...json]);
      }
    }
    await compare(cases);
    // What reached the server for the settings writes: the whole record, with
    // only the named fields changed.
    const puts = srv.seen.filter((s) => s.method === "PUT" && s.path.endsWith("/reason-settings"));
    expect(puts.at(-1)?.body).toEqual({ model: "openai.gpt-oss-120b-1:0", depth: null });
  });

  it("the commands for the rest of the API: models, rules, webhooks, graph, entities, receipts, ask-user, chat, exports, account views", async () => {
    const home = process.env.HOME as string;
    const file = (name: string, text: string) => {
      writeFileSync(join(home, name), text);
      return join(home, name);
    };
    const rulesFile = file("routing-rules.json", '[{"field": "agent_id", "op": "equals", "value": "billing-bot", "space_id": "billing"}]');
    const charter = file("charter.txt", "Billing questions.\nOnly.");
    const ES = ["--base-url", srv.base, "--api-key", "anona_live_ERRSESSIONKEYKEY"];
    const out = (name: string) => join(home, name);
    const cases: Case[] = [];
    for (const json of [[], ["--json"]]) {
      cases.push(
        // memory models
        ["models", "list", ...S("s"), ...json],
        ["models", "list", "--limit", "5", "--offset", "2", "--tags", "a,b", ...S("s"), ...json],
        ["models", "list", ...S("hostile"), ...json],
        ["models", "create", "Build habits", "--query", "How?", ...S("s"), ...json],
        ["models", "create", "Build habits", "--query", "How?", "--tags", "a", "--mode", "delta", "--refresh-on-new-memories", "--memory-type", "fact", "--memory-type", "note", "--max-tokens", "512", "--model-id", "build", ...S("s"), ...json],
        ["models", "get", "mm-1", ...S("s"), ...json],
        ["models", "get", "mm-1", ...S("hostile"), ...json],
        ["models", "edit", "mm-1", "--name", "Renamed", ...S("s"), ...json],
        ["models", "edit", "mm-delta", "--no-refresh-on-new-memories", ...S("s"), ...json],
        ["models", "edit", "mm-1", "--mode", "delta", "--name", "N", ...S("s"), ...json],
        ["models", "delete", "mm-1", "--yes", ...S("s"), ...json],
        ["models", "delete", "mm-1", ...S("s"), ...json],
        ["models", "refresh", "mm-1", ...S("s"), ...json],
        ["models", "clear", "mm-1", "--yes", ...S("s"), ...json],
        ["models", "clear", "mm-1", ...S("s"), ...json],
        ["models", "history", "mm-1", ...S("s"), ...json],
        ["models", "history", "mm-1", ...S("hostile"), ...json],
        ["models", "list", ...S("e404"), ...json],
        ["models", "create", "n", "--query", "q", ...S("e422"), ...json],
        ["models", "delete", "mm-1", "--yes", ...S("e403"), ...json],
        // profile set / reset
        ["profile", "set", "--empathy", "5", ...S("s"), ...json],
        ["profile", "set", "--mission", "New mission.", "--skepticism", "1", ...S("s"), ...json],
        ["profile", "set", "--mission", "@" + charter, ...S("s"), ...json],
        ["profile", "reset", "--yes", ...S("s"), ...json],
        ["profile", "reset", "--yes", ...S("hostile"), ...json],
        ["profile", "reset", ...S("s"), ...json],
        ["profile", "set", "--empathy", "5", ...S("e404"), ...json],
        // rules
        ["rules", "list", ...S("s"), ...json],
        ["rules", "list", ...S("hostile"), ...json],
        ["rules", "add", "Never X.", ...S("s"), ...json],
        ["rules", "add", "  Never   X\t\u00a0again.  ", "--name", "N", "--priority", "5", "--no-active", "--tags", "a,b", ...S("s"), ...json],
        ["rules", "add", "w ".repeat(50), ...S("s"), ...json],
        ["rules", "edit", "r-1", "--text", "Y.", "--priority", "3", ...S("s"), ...json],
        ["rules", "edit", "r-1", "--active", "--tags", "z", ...S("s"), ...json],
        ["rules", "delete", "r-1", "--yes", ...S("s"), ...json],
        ["rules", "delete", "r-1", ...S("s"), ...json],
        ["rules", "add", "Never X.", ...S("e422"), ...json],
        // webhooks
        ["webhooks", "list", ...S("s"), ...json],
        ["webhooks", "list", ...S("hostile"), ...json],
        ["webhooks", "create", "https://x.example/h", "--events", "memory.created,memory.consolidated", "--no-enabled", ...S("s"), ...json],
        ["webhooks", "create", "https://nosecret.example", ...S("s"), ...json],
        ["webhooks", "edit", "w-1", "--url", "https://z.example", "--events", "memory.created", "--enabled", ...S("s"), ...json],
        ["webhooks", "delete", "w-1", "--yes", ...S("s"), ...json],
        ["webhooks", "delete", "w-1", ...S("s"), ...json],
        ["webhooks", "deliveries", "w-1", "--limit", "5", "--cursor", "c", ...S("s"), ...json],
        ["webhooks", "deliveries", "w-1", ...S("hostile"), ...json],
        ["webhooks", "create", "https://x.example/h", ...S("e403"), ...json],
        // graph and entities
        ["graph", ...S("s"), ...json],
        ["graph", "--limit", "5", "--min-count", "2", ...S("hostile"), ...json],
        ["graph", "draw", "by week", ...S("s"), ...json],
        ["graph", "draw", "by week", "--model", "m", ...S("hostile"), ...json],
        ["graph", "draw", "mermaid", ...S("s"), ...json],
        ["graph", "draw", "diagram", ...S("s"), ...json],
        ["graph", "draw", "nothing", ...S("s"), ...json],
        ["graph", ...S("e404"), ...json],
        ["entities", "list", "--limit", "5", "--offset", "1", ...S("s"), ...json],
        ["entities", "list", ...S("hostile"), ...json],
        ["entities", "get", "e1", ...S("s"), ...json],
        ["entities", "get", "e1", ...S("hostile"), ...json],
        ["entities", "get", "e1", ...S("empty"), ...json],
        ["entities", "get", "e1", ...S("e404"), ...json],
        // receipts, ask-user, space-types, chat
        ["receipts", "get", "req_1", ...K(), ...json],
        ["receipts", "get", "req_1", "--explain", "--memory-id", "m1", ...K(), ...json],
        ["receipts", "get", "req_1", "--explain", "--memory-id", "m-bare", ...K(), ...json],
        ["ask-user", "alice", "What does she like?", ...S("s"), ...json],
        ["ask-user", "alice", "What does she like?", "--model", "m", "--depth", "thorough", ...S("hostile"), ...json],
        ["ask-user", "alice", "blank", ...S("s"), ...json],
        ["ask-user", "alice", "none", ...S("s"), ...json],
        ["ask-user", "alice", "q", ...S("e404"), ...json],
        ["space-types", ...K(), ...json],
        ["chat", "hi", ...S("s"), ...json],
        ["chat", "hi", "--model", "m", "--no-memory", ...S("hostile"), ...json],
        ["chat", "empty", ...S("s"), ...json],
        ["chat", "nochoice", ...S("s"), ...json],
        ["chat", "hi", ...S("e429"), ...json],
        // routing settings
        ["routing-settings", "get", ...S("s"), ...json],
        ["routing-settings", "get", ...S("empty"), ...json],
        ["routing-settings", "get", ...S("hostile"), ...json],
        ["routing-settings", "set", "--charter", "Billing", "--topics", "a, b", "--no-auto-route", ...S("s"), ...json],
        ["routing-settings", "set", "--charter", "@" + charter, "--rules-file", rulesFile, "--shared-auto-route", ...S("s"), ...json],
        ["routing-settings", "set", "--clear", "topics", "--clear", "rules", ...S("s"), ...json],
        ["routing-settings", "reset", "--yes", ...S("s"), ...json],
        ["routing-settings", "reset", ...S("s"), ...json],
        ["routing-settings", "get", ...S("e404"), ...json],
        // account views
        ["org", ...K(), ...json],
        ["org", "--base-url", srv.base, "--api-key", "anona_live_NOPLANKEYKEYKEY", ...json],
        ["org", "--base-url", srv.base, "--api-key", "anona_live_ERRSESSIONKEYKEY", ...json],
        ["orgs", "list", ...K(), ...json],
        ["projects", "list", ...K(), ...json],
        ["keys", "list", ...K(), ...json],
        ["members", "list", ...K(), ...json],
        ["members", "me", ...K(), ...json],
        ["members", "invitations", ...K(), ...json],
        ["invitations", "pending", ...K(), ...json],
        ["invitations", "accept", "tok-1", ...K(), ...json],
        ["invitations", "accept", "tok-2", ...K(), ...json],
        ["invitations", "decline", "tok-1", ...K(), ...json],
        ["billing", ...K(), ...json],
        ["billing", "--base-url", srv.base, "--api-key", "anona_live_BANKEDKEYKEYKEY", ...json],
        ["billing", "topups", ...K(), ...json],
        ["usage", "--history", ...K(), ...json],
        ["usage", "--history", "--days", "1", ...K(), ...json],
        ["usage", "--breakdown", ...K(), ...json],
        ["usage", "--breakdown", "--days", "1", ...K(), ...json],
        ["exports", "list", ...S("s"), ...json],
        ["exports", "list", ...S("empty"), ...json],
        ["exports", "create", ...S("s"), ...json],
        ["spaces", "members", "list", "notes", ...K(), ...json],
        ["spaces", "members", "list", "empty", ...K(), ...json],
        // an API key or a temporary profile on an account route
        ...[["org"], ["orgs", "list"], ["projects", "list"], ["keys", "list"], ["members", "list"], ["members", "me"], ["members", "invitations"], ["invitations", "pending"], ["billing"], ["billing", "topups"], ["usage", "--history"], ["usage", "--breakdown"]].map((c) => [...c, ...ES, ...json]),
        ["spaces", "members", "list", "notes", ...ES, ...json],
        ["exports", "list", "--space", "s", ...ES, ...json],
        ["invitations", "accept", "tok-1", ...ES, ...json],
      );
      // exports download: the credential never goes to the link; --yes lets both CLIs overwrite
      for (const [job, name] of [["j-link", "link"], ["j-relative", "relative"], ["j-stream", "stream"], ["j-redirect", "redirect"]]) {
        cases.push(["exports", "download", job as string, "--out", out(`${name}.zip`), "--yes", ...S("s"), ...json]);
      }
      cases.push(
        ["exports", "download", "j-nolink", "--out", out("nolink.zip"), "--yes", ...S("s"), ...json],
        ["exports", "download", "j-badlink", "--out", out("badlink.zip"), "--yes", ...S("s"), ...json],
        ["exports", "download", "j-deadlink", "--out", out("deadlink.zip"), "--yes", ...S("s"), ...json],
        ["exports", "download", "j-slow", "--timeout", "0.5", "--out", out("slow.zip"), "--yes", ...S("s"), ...json],
        ["exports", "download", "j-die", "--out", out("die.zip"), "--yes", ...S("s"), ...json],
        ["exports", "download", "j-refused", "--out", out("refused.zip"), "--yes", ...S("s"), ...json],
        ["exports", "download", "j-missing", "--out", out("missing.zip"), "--yes", ...S("s"), ...json],
        ["exports", "download", "j-link", "--out", join(home, "no-such-dir", "x.zip"), "--yes", ...S("s"), ...json],
        ["exports", "download", "j-link", "--out", out("exists.zip"), ...S("s"), ...json],
      );
    }
    writeFileSync(out("exists.zip"), "PRECIOUS");
    // The dashboard-only verbs: nothing is sent, no credential is read, and extra words and --yes are ignored.
    for (const [cmd, act] of [
      ["keys", "create"], ["keys", "revoke"], ["billing", "upgrade"], ["billing", "checkout"], ["billing", "topup"], ["billing", "cancel"], ["billing", "portal"],
      ["org", "rename"], ["org", "delete"], ["org", "transfer"], ["org", "leave"], ["members", "invite"], ["members", "remove"],
    ]) {
      cases.push(
        [cmd as string, act as string],
        [cmd as string, act as string, "some-arg", "--json"],
        [cmd as string, act as string, ...K()],
        [cmd as string, act as string, "--yes"],
        [cmd as string, act as string, "some-arg", "--yes", ...K()],
      );
    }
    cases.push(
      ["spaces", "members", "remove"],
      ["spaces", "members", "remove", "notes", "m1", "--json"],
      ["spaces", "members", "remove", "notes", "m1", ...K()],
      ["spaces", "members", "remove", "notes", "m1", "--yes"],
      // The space's own page, its name escaped as one path segment.
      ["spaces", "members", "remove", "acme:a b/c?#", "m1"],
      ["spaces", "members", "remove", "it's(1)*!~ü", "m1"],
      // `projects create` is gone: a usage error in both.
      ["projects", "create", "Docs"],
    );
    const before = srv.seen.length;
    await compare(cases);
    expect(srv.seen.length).toBeGreaterThan(before);
    expect(readFileSync(out("link.zip"), "utf8")).toBe("ZIPDATA-LINK");
    expect(readFileSync(out("relative.zip"), "utf8")).toBe("ZIPDATA-LINK");
    expect(readFileSync(out("stream.zip"), "utf8")).toBe("ZIPDATA-STREAM");
    expect(readFileSync(out("redirect.zip"), "utf8")).toBe("ZIPDATA-LINK");
    expect(readFileSync(out("slow.zip"), "utf8")).toBe("aaaabbbbccccddddeeeeffff");
    // A download that failed leaves neither the target nor a temporary file.
    for (const name of ["die.zip", "refused.zip"]) expect(existsSync(out(name))).toBe(false);
    expect(readdirSync(home).filter((n) => n.startsWith(".anona-export-"))).toEqual([]);
    expect(readFileSync(out("exists.zip"), "utf8")).toBe("PRECIOUS");
    // The API credential reached the download endpoint and never the link.
    for (const seen of srv.seen.filter((x) => x.path.startsWith("/blob/"))) expect(seen.auth).toBeUndefined();
  });

  it("the new commands' usage errors: same message, same exit, nothing sent", async () => {
    const before = srv.seen.length;
    const k = S("s");
    const home = process.env.HOME as string;
    writeFileSync(join(home, "bad-rules.json"), '{"not": "an array"}');
    writeFileSync(join(home, "ok-rules.json"), "[]");
    await compare([
      ["models", ...K()],
      ["models", "wat", ...K()],
      ["models", "create", ...k],
      ["models", "create", "n", ...k],
      ["models", "create", "  ", "--query", "q", ...k],
      ["models", "create", "n", "--query", " ", ...k],
      ["models", "create", "n", "--query", "q", "--mode", "partial", ...k],
      ["models", "create", "n", "--query", "q", "--memory-type", "summary", ...k],
      ["models", "create", "n", "--query", "q", "--max-tokens", "10", ...k],
      ["models", "create", "n", "--query", "q", "--tags", "a,,b", ...k],
      ["models", "list", "--limit", "0", ...k],
      ["models", "list", "--offset", "-1", ...k],
      ["models", "edit", "mm-1", ...k],
      ["models", "get", ...k],
      ["models", "delete", "mm-1", ...k],
      ["profile", "set", ...k],
      ["profile", "wat", ...k],
      ["profile", "--mission", "x", ...k],
      ["profile", "reset", "--empathy", "3", "--yes", ...k],
      ["profile", "set", "--empathy", "9", ...k],
      ["profile", "set", "--skepticism", "x", ...k],
      ["rules", "add", ...k],
      ["rules", "add", "   ", ...k],
      ["rules", "add", "x", "--priority", "1001", ...k],
      ["rules", "edit", "r-1", ...k],
      ["rules", "edit", "r-1", "--text", "@/nonexistent/rule.txt", ...k],
      ["rules", "delete", "r-1", ...k],
      ["webhooks", "create", ...k],
      ["webhooks", "create", "https://x.example", "--events", "a,,b", ...k],
      ["webhooks", "edit", "w-1", ...k],
      ["webhooks", "delete", "w-1", ...k],
      ["webhooks", "deliveries", "w-1", "--limit", "201", ...k],
      ["graph", "draw", ...k],
      ["graph", "draw", "  ", ...k],
      ["graph", "chart", ...k],
      ["graph", "--limit", "0", ...k],
      ["graph", "--model", "m", ...k],
      ["entities", "list", "--limit", "501", ...k],
      ["entities", "get", ...k],
      ["receipts", "get", ...K()],
      ["receipts", "get", "req_1", "--explain", ...K()],
      ["receipts", "get", "req_1", "--memory-id", "m1", ...K()],
      ["ask-user", "alice", ...k],
      ["ask-user", " ", "q", ...k],
      ["ask-user", "alice", "  ", ...k],
      ["ask-user", "alice", "q", "--depth", "medium", ...k],
      ["chat", ...k],
      ["chat", "  ", ...k],
      ["routing-settings", ...k],
      ["routing-settings", "set", ...k],
      ["routing-settings", "set", "--clear", "auto_route_enabled", ...k],
      ["routing-settings", "set", "--clear", "shared_auto_route", ...k],
      ["routing-settings", "set", "--topics", "a,,b", ...k],
      ["routing-settings", "set", "--charter", "x", "--clear", "charter", ...k],
      ["routing-settings", "set", "--rules-file", join(home, "bad-rules.json"), ...k],
      ["routing-settings", "set", "--rules-file", "/nonexistent/rules.json", ...k],
      ["routing-settings", "reset", ...k],
      ["org", "wat", ...K()],
      ["billing", "wat", ...K()],
      ["orgs", ...K()],
      ["keys", ...K()],
      ["keys", "list", "--space", "s", ...K()],
      ["members", "wat", ...K()],
      ["invitations", "accept", ...K()],
      ["usage", "--days", "3", ...K()],
      ["usage", "--history", "--breakdown", ...K()],
      ["usage", "--history", "--days", "0", ...K()],
      ["usage", "--history", "--days", "366", ...K()],
      ["exports", ...k],
      ["exports", "download", ...k],
      ["spaces", "members", ...K()],
      ["spaces", "members", "wat", ...K()],
      ["spaces", "members", "list", ...K()],
      ["spaces", "members", "remove", "notes", "m1", "--yes", ...K()],
      ["keys", "create", "--wat"],
      ["members", "invite", "--space"],
      ["space-types", "extra", ...K()],
    ]);
    expect(srv.seen.length).toBe(before);
  });

  it("--help of the new commands says the same thing in both CLIs, wrapping aside", async () => {
    // argparse and the hand-rolled renderer break lines at different places
    // (including inside a hyphenated word), so the words are compared with
    // every space and line break removed: names, order, choices and help text.
    const squash = (text: string) => text.replace(/\s+/g, "");
    const topics = [
      ["models"], ["models", "create"], ["models", "edit"], ["rules", "add"], ["rules", "edit"], ["webhooks", "create"], ["webhooks", "deliveries"],
      ["graph"], ["entities", "list"], ["receipts", "get"], ["ask-user"], ["chat"], ["profile"], ["usage"], ["org"], ["billing"], ["keys"], ["keys", "create"],
      ["members", "invite"], ["invitations", "accept"], ["exports", "download"], ["spaces"], ["spaces", "members"], ["spaces", "members", "remove"],
      ["routing-settings"], ["routing-settings", "set"], ["routing-settings", "reset"], ["space-types"], ["projects"],
    ];
    const python: Array<[number, string, string]> = await py(RUN_MANY, topics.flatMap((w) => [[[...w, "--help"], ""], [["help", ...w], ""]]));
    for (let i = 0; i < topics.length; i++) {
      const w = topics[i] as string[];
      const viaFlag = await run([...w, "--help"]);
      const viaHelp = await run(["help", ...w]);
      expect([viaFlag.rc, viaFlag.err], w.join(" ")).toEqual([0, ""]);
      expect(squash(viaFlag.out), w.join(" ")).toBe(squash(python[2 * i]![1]));
      expect(squash(viaHelp.out), w.join(" ")).toBe(squash(python[2 * i + 1]![1]));
    }
  });

  it("usage errors: same message, same exit, nothing sent", async () => {
    const before = srv.seen.length;
    const home = process.env.HOME as string;
    const bad = (name: string, text: string) => {
      writeFileSync(join(home, name), text);
      return join(home, name);
    };
    const ok = bad("ok.jsonl", '"fine"\n');
    const k = S("s");
    await compare([
      ["record", ...k],
      ["record", "", ...k],
      ["record", "x", "--file", "/nonexistent/file", ...k],
      ["record", "--file", "/nonexistent/file", ...k],
      ["record", "x", "--tags", "a,,b", ...k],
      ["record", "x", "--t", "a", ...k],
      ["record", "x", "--metadata", "[1]", ...k],
      ["record", "x", "--metadata", "{", ...k],
      ["record", "x", "--timestamp", "2026-02-30", ...k],
      ["record", "x", "--json=1", ...k],
      ["record", "x", "y", ...k],
      ["record", "x", "--space"],
      ["record", "x", "--wait", ...k],
      ["record", "x", "--async", "--wait-timeout", "5", ...k],
      ["record", "x", "--async", "--wait", "--wait-timeout", "-1", ...k],
      ["record", "x", "--batch", ok, ...k],
      ["record", "--batch", ok, "--async", ...k],
      ["record", "--batch", ok, "--async", "--wait", ...k],
      ["record", "--batch", ok, "--tags", "a", "--timestamp", "2026-01-01", ...k],
      ["record", "--batch", bad("b1.jsonl", '{"content": "a", "colour": "red", "Zed": 1}\n'), ...k],
      ["record", "--batch", bad("b2.jsonl", '{"content": ""}\n'), ...k],
      ["record", "--batch", bad("b3.jsonl", "not json\n"), ...k],
      ["record", "--batch", bad("b4.jsonl", "[1, 2]\n"), ...k],
      ["record", "--batch", bad("b5.jsonl", "\n\n"), ...k],
      ["record", "--batch", bad("b6.jsonl", Array.from({ length: 101 }, (_, i) => `"m${i}"\n`).join("")), ...k],
      ["record", "--batch", join(home, "missing.jsonl"), ...k],
      ["retrieve", ...k],
      ["retrieve", "  ", ...k],
      ["retrieve", "q", "--limit", "0", ...k],
      ["retrieve", "q", "--limit", "x", ...k],
      ["retrieve", "q", "--min-score", "inf", ...k],
      ["retrieve", "q", "--tags-match", "all", ...k],
      ["retrieve", "q", "--tags-match", "nope", "--tags", "a", ...k],
      ["retrieve", "q", "--max-tokens", "50", ...k],
      ["retrieve", "q", "--format", "block", "--max-tokens", "0", ...k],
      ["retrieve", "q", "--format", "html", ...k],
      ["retrieve", "q", "--tag-groups", "not json", ...k],
      ["retrieve", "q", "--tag-groups", "[]", ...k],
      ["retrieve", "q", "--tag-groups", "[1]", ...k],
      ["retrieve", "q", "--tag-groups", "@/nonexistent/groups.json", ...k],
      ["reason", "q", "--depth", "medium", ...k],
      ["spaces", ...K()],
      ["spaces", "wat", ...K()],
      ["spaces", "list", "extra", ...K()],
      ["spaces", "create", "  ", ...K()],
      ["spaces", "delete", "old", ...K()],
      ["memories", "edit", "m", ...k],
      ["memories", "edit", "m", "--text", " ", ...k],
      ["memories", "list", "--offset", "-1", ...k],
      ["memories", "list", "--limit", "y", ...k],
      ["memories", "list", "--q", "-x", ...k],
      ["memories", "delete", "anona_live_PASTEDPASTEDPASTED", ...k],
      ["user-profile", ...k],
      ["user-profile", "  ", ...k],
      ["documents", "upload", ...k],
      ["documents", "upload", join(home, "missing.pdf"), ...k],
      ["documents", "upload", ok, "--wait-timeout", "3", ...k],
      ["documents", "delete", "doc-1", ...k],
      ["documents", "list", "--source", "web", ...k],
      ["reason-settings", "set", ...k],
      ["reason-settings", "set", "--depth", "fast", "--clear", "depth", ...k],
      ["reason-settings", "set", "--depth", "slow", ...k],
      ["reason-settings", "set", "--clear", "colour", ...k],
      ["chat-settings", "set", "--memory-limit", "21", ...k],
      ["chat-settings", "set", "--memory=1", ...k],
      ["chat-settings", "reset", ...k],
      ["extraction-settings", "set", "--labels-file", "/nonexistent/labels.json", ...k],
      ["extraction-settings", "set", "--labels-file", ok, ...k],
      ["record", "x", "--space", "", ...K()],
      ["record", "x", "--user-id", " ", ...k],
      ["retrieve", "q", "--session-id", "", ...k],
      ["memories", "list", "--agent-id", "", ...k],
      ["record", "x", "--version"],
      ["--wat", "record", "x"],
      ["wat"],
      [],
      ["login", "--cli", "--mcp"],
      ["status", "--force"],
      ["record", "x", "--timeout", "0", ...k],
      ["record", "x", "--timeout", "soon", ...k],
      ["record", "x", "--retries", "11", ...k],
      ["record", "x", "--retries", "x", ...k],
      ["record", "x", "--profile", "bad name", ...k],
      ["record", "x", "--profile", "-x", ...k],
      ["help", "--json"],
      ["help", "nope"],
      ["help", "spaces", "nope"],
      ["help", "record", "extra"],
      ["help", "anona_live_PASTEDPASTEDPASTED"],
      ["help", "record", "--llm"],
      ["help", "spaces", "delete", "--json"],
      ["completion", "tcsh"],
      ["record", "x", "--base-url", srv.base, "--api-key", "anona_live_K"],
      ["memories", "list", "--base-url", srv.base, "--api-key", "anona_live_K"],
    ]);
    await compare([["record", "x", ...k], ["status"]], { env: { ANONA_PROFILE: "../escape" } });
    expect(srv.seen.length).toBe(before);
  });

  it("globals: --quiet, --retries, profiles", async () => {
    const k = S("s");
    await compare([
      ["reason", "q", "-q", ...k],
      ["reason", "q", "--quiet", ...k],
      ["memories", "list", ...S("e500")],
      ["memories", "list", "--retries", "0", ...S("e500")],
      ["memories", "list", "--retries", "1", "-q", ...S("e500")],
      ["record", "async-run", "--async", "--wait", "--wait-timeout", "0.05", ...k],
      ["record", "async-run", "--async", "--wait", "--wait-timeout", "0.05", "-q", ...k],
    ]);
    // A named profile is its own directory: credentials, command-line login,
    // config.env. The default directory is untouched by it, and vice versa.
    writeMcpFile({ base: srv.base, tempHours: 5, profile: "work" });
    writeConfigEnv(`ANONA_SPACE_ID='fromdefault'\n`);
    mkdirSync(join(process.env.HOME as string, ".anona", "profiles", "other"), { recursive: true });
    writeConfigEnv(`ANONA_API_KEY='anona_live_OTHERPROFILEKEYX'\nANONA_SPACE_ID='other'\nANONA_BASE_URL='${srv.base}'\n`, 0o600, "other");
    await compare([
      ["record", "x", "--profile", "work"],
      ["record", "x", "--profile", "default"],
      ["record", "x", "--profile", "other"],
      ["status", "--profile", "work"],
      ["status", "--profile", "other"],
      ["record", "x"],
    ]);
    await compare([["record", "x"], ["memories", "list"]], { env: { ANONA_PROFILE: "work" } });
  });

  it("credentials: none, an MCP login, a temporary profile, a command-line login, config.env, refusals", async () => {
    await compare([["record", "x"], ["spaces", "list"], ["status"]]);
    writeMcpFile({ base: srv.base, signedIn: true });
    await compare([["record", "x"], ["usage"], ["record", "x", "--space", "w"]]);
    writeMcpFile({ base: srv.base, tempHours: -1 });
    await compare([["record", "x"], ["memories", "list"]]);
    writeMcpFile({ base: srv.base, tempHours: 5 });
    await compare([["record", "x"], ["memories", "list"], ["memories", "delete", "m"], ["spaces", "delete", "x"], ["record", "x", "--base-url", "https://other.example"]]);
    writeCliFile({ base: srv.base });
    await compare([["record", "x"], ["record", "x", "--space", "w"], ["usage"], ["memories", "list", "--space", "w"], ["record", "x", "--space", "w", "--base-url", "https://other.example"]]);
    // No ANONA_BASE_URL beside this key, so a data command would go to the
    // real api.anonalabs.com: only the local refusals and status run here.
    writeConfigEnv(`ANONA_API_KEY='anona_live_FROMFILEFROMFILE'\nANONA_SPACE_ID='cfg'\n`, 0o644);
    await compare([["status"], ["record", "x", "--base-url", srv.base], ["status", "--base-url", srv.base]]);
    writeConfigEnv(`ANONA_API_KEY='anona_live_FROMFILEFROMFILE'\nANONA_SPACE_ID='cfg'\nANONA_BASE_URL='${srv.base}'\n`);
    await compare([["record", "x"], ["profile"], ["status"]]);
    await compare([["record", "x"], ["status"]], { env: { ANONA_SPACE_ID: "" } });
    await compare([["record", "x"], ["record", "x", "--base-url", "http://10.1.2.3"], ["status"], ["status", "--api-key", "short"]], {
      env: { ANONA_API_KEY: "anona_live_FROMENVFROMENV" },
    });
  });

  it("doctor: the same report, runtime aside", async () => {
    const d = (extra: string[] = []) => ["doctor", ...extra];
    await compare([d(), d(["--json"])], { doctor: true });
    writeMcpFile({ base: srv.base, signedIn: true });
    await compare([d(), d(["--json"])], { doctor: true });
    writeMcpFile({ base: srv.base, tempHours: 5 });
    await compare([d(), d(["--json"]), d(["--space", "e404"]), d(["--space", "e404", "--json"])], { doctor: true });
    writeCliFile({ base: srv.base });
    await compare([d(), d(["--space", "w"]), d(["--space", "w", "--json"]), d(["--base-url", "https://other.example"])], { doctor: true });
    await compare(
      [
        d(["--api-key", "anona_live_PARITYKEYPARITYKEY", "--base-url", srv.base, "--space", "s"]),
        d(["--api-key", "anona_live_PARITYKEYPARITYKEY", "--base-url", srv.base, "--space", "s", "--json"]),
        d(["--api-key", "anona_live_PARITYKEYPARITYKEY", "--base-url", srv.base]),
        d(["--api-key", "anona_live_PARITYKEYPARITYKEY", "--base-url", srv.base, "--space", "e403"]),
        d(["--api-key", "anona_live_PARITYKEYPARITYKEY", "--base-url", "http://evil.example", "--space", "s"]),
      ],
      { doctor: true },
    );
  });

  it("guide, guide --json and completion: generated from each CLI's own grammar, and the same", async () => {
    const python: Array<[number, string, string]> = await py(RUN_MANY, [
      [["guide"], ""],
      [["guide", "--json"], ""],
      [["help", "--llm", "--json"], ""],
      [["completion", "bash"], ""],
      [["completion", "zsh"], ""],
      [["completion", "fish"], ""],
    ]);
    // The one legitimate difference: doctor's --json reports Node, not Python.
    const doc = (s: string) => s.replace('{\\"anona_version\\",\\"python\\"', '{\\"anona_version\\",\\"RUNTIME\\"').replace('{\\"anona_version\\",\\"node\\"', '{\\"anona_version\\",\\"RUNTIME\\"');
    const text = (s: string) => s.replace('{"anona_version","python"', '{"anona_version","RUNTIME"').replace('{"anona_version","node"', '{"anona_version","RUNTIME"');
    const [guide, guideJson, helpJson, bash, zsh, fish] = await Promise.all(
      [["guide"], ["guide", "--json"], ["help", "--llm", "--json"], ["completion", "bash"], ["completion", "zsh"], ["completion", "fish"]].map((a) => run(a)),
    );
    expect(text(guide!.out)).toBe(text(python[0]![1]));
    expect(doc(guideJson!.out)).toBe(doc(python[1]![1]));
    expect(doc(helpJson!.out)).toBe(doc(python[2]![1]));
    // The trees, structurally: every command, flag, metavar, choice and help.
    const strip = (g: any) => JSON.parse(JSON.stringify(g), (k, v) => (k === "json_output" && typeof v === "string" && v.includes("anona_version") ? "DOCTOR" : v));
    expect(strip(JSON.parse(guideJson!.out))).toEqual(strip(JSON.parse(python[1]![1])));
    expect(bash!.out).toBe(python[3]![1]);
    expect(zsh!.out).toBe(python[4]![1]);
    expect(fish!.out).toBe(python[5]![1]);
  });

  it("help COMMAND is that command's --help, in both CLIs", async () => {
    const topics = [["record"], ["spaces"], ["spaces", "delete"], ["chat-settings", "set"], ["help"], ["completion"]];
    const python: Array<[number, string, string]> = await py(
      RUN_MANY,
      topics.flatMap((w) => [[["help", ...w], ""], [[...w, "--help"], ""]]),
    );
    for (let i = 0; i < topics.length; i++) {
      const w = topics[i] as string[];
      const [phrc, phout, pherr] = python[2 * i] as [number, string, string];
      const [, pdout] = python[2 * i + 1] as [number, string, string];
      expect([phrc, pherr, phout === pdout], `pip help ${w.join(" ")}`).toEqual([0, "", true]);
      const viaHelp = await run(["help", ...w]);
      const viaFlag = await run([...w, "--help"]);
      expect([viaHelp.rc, viaHelp.err], w.join(" ")).toEqual([0, ""]);
      expect(viaHelp.out, w.join(" ")).toBe(viaFlag.out);
      expect(viaHelp.out.startsWith(`usage: anona ${w.join(" ")} `)).toBe(true);
    }
  });

  it("the helpers agree with Python's own: fromisoformat, %.3g, json.dumps, config.env, redact", async () => {
    const isoCases = [
      "2026-06-01", "2026-06-01T09:00:00Z", "20260601", "2026-W01-1", "2026W011", "2026-W53", "2021-W53-1", "2026-06-01 09:00",
      "2026-06-01T09", "2026-06-01T0900", "2026-06-01T09:00:00.123", "2026-06-01T09:00:00,123", "2026-06-01T09:00:00.1234567",
      "2026-06-01T09:00:00+05", "2026-06-01T09:00:00+0530", "2026-06-01T09:00:00+05:30:15", "2026-06-01x09:00", "2026-06-01T24:00",
      "2025-02-30", "2024-02-29", "2026-6-1", "2026-06", "2026", "yesterday", " 2026-06-01T09:00:00 ", "2026-06-01T09:00:00Z+00:00",
      "2026-06-01T25:00", "2026-06-01T09:60", "+2026-06-01", "2026-06-01T090000", "2026-06-01T09:00:00-00:00", "2026-06-01T09:00:00+24:00",
      "2026-001", "2026-06-01T09:00Z", "2026-06-01z", "2026-06-01T09:00:00.", "2026-06-01T09:5", "2026-06-01T09:00:00+5", "0000-01-01",
      "2026-06-01T09:00:00+23:59", "2026-06-01T09:00:00+05:99", "2026-06-01T09-05", "2026-06-01Tab", "2026-06-01T",
    ];
    const g3Cases = [1.0978, 0.00006, 123456.0, 0.5, 1, 0, 0.0001234, 999.5, 1000, 0.1, 2.675, 1e-10, -3.14159, 100, 99.95];
    const docs = [{ a: "é\u{1f600}\x7f\x1b\u2028", b: [1, 2.5, null, true, false], c: { d: "\"\\/\n" } }, [], {}, "x", 0.000012, 1e16, 12345678.9];
    const cfg = "export ANONA_API_KEY='a'\\''b'\r\nANONA_SPACE_ID=\nANONA_SPACE_ID=s\u2028ANONA_BASE_URL=u\nANONA_API_KEY=second";
    const texts = ["key anona_live_abc-DEF_1 and anona_tmp_x.y, notanona_test_q", "anona_test_", "(anona_live_X)"];
    const python = await py(
      `
import json, sys
from datetime import datetime
from anona.cli import config_env, credential
p = json.load(sys.stdin)
def iso(s):
    t = s.strip()
    try:
        datetime.fromisoformat(t[:-1] + "+00:00" if t.endswith(("Z", "z")) else t)
        return True
    except ValueError:
        return False
print(json.dumps({
    "iso": [iso(s) for s in p["iso"]],
    "g3": [format(x, ".3g") for x in p["g3"]],
    "json": [json.dumps(d, ensure_ascii=True) for d in p["docs"]],
    "json_sorted": json.dumps(p["docs"][0], ensure_ascii=True, sort_keys=True),
    "json_compact": json.dumps(p["docs"][0], separators=(",", ":")),
    "cfg": config_env.parse(p["cfg"]),
    "redact": [credential.redact(t) for t in p["texts"]],
}))
`,
      { iso: isoCases, g3: g3Cases, docs, cfg, texts },
    );
    const okIso = (s: string) => {
      try {
        iso8601(s);
        return true;
      } catch {
        return false;
      }
    };
    expect(isoCases.filter((s, i) => okIso(s) !== python.iso[i])).toEqual([]);
    expect(g3Cases.map(g3)).toEqual(python.g3);
    // 1e16 is a float to Python (1e+16) and an integer to JSON.parse; skip it.
    expect(docs.map((x) => pyJson(x)).filter((_, i) => i !== 5)).toEqual(python.json.filter((_: unknown, i: number) => i !== 5));
    expect(pyJson(docs[0], { sortKeys: true })).toBe(python.json_sorted);
    expect(pyJson(docs[0], { compact: true })).toBe(python.json_compact);
    expect(parseConfigEnv(cfg)).toEqual(python.cfg);
    expect(texts.map(redact)).toEqual(python.redact);
  });
});
