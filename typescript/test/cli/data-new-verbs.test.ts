/**
 * The newer commands and global flags, asserted on what reached a real local
 * server: record --async / --batch / --wait, retrieve's compound filters and
 * block format, documents, the three space settings, llm-models, spaces
 * delete, --retries / --timeout / --quiet, and --profile. Stub bodies are the
 * gateway's own (fixtures/gateway-bodies.json is checked against the gateway's
 * models by the pip package's suite).
 */
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { jobTuning } from "../../src/cli/jobs.js";
import { oneJson, run, SECRET, startDataServer, useDataEnv, writeConfigEnv, writeMcpFile, type DataServer, type Reply, type Seen } from "./data-helpers.js";

const BODIES = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "fixtures", "gateway-bodies.json"), "utf8"));

type Route = (s: Seen) => Reply;

describe("new verbs and global flags", () => {
  const home = useDataEnv();
  let srv: DataServer;
  let routes: Array<[string, RegExp, Route]>;
  const route = (method: string, path: string, fn: Route) => routes.unshift([method, new RegExp(`^${path}$`), fn]);
  beforeEach(async () => {
    routes = [];
    srv = await startDataServer((s) => {
      const hit = routes.find(([m, p]) => m === s.method && p.test(s.path));
      return hit ? hit[2](s) : { status: 404, body: { error: { code: "not_found", message: "No route." } } };
    });
    jobTuning.firstInterval = 0.01;
    jobTuning.maxInterval = 0.01;
  });
  afterEach(() => {
    jobTuning.firstInterval = 1;
    jobTuning.maxInterval = 5;
    return srv.close();
  });
  const KEYED = () => ["--base-url", srv.base, "--api-key", SECRET];
  const AUTH = () => ["--space", "s", ...KEYED()];
  const ok = (body: unknown, status = 200): Route => () => ({ status, body });

  async function sent(argv: string[], o: { rc?: number; n?: number; stdin?: string } = {}): Promise<[Seen[], string, string]> {
    const before = srv.seen.length;
    const r = await run(argv, o.stdin ?? "");
    expect(r.rc, `${argv.join(" ")}: ${r.err}`).toBe(o.rc ?? 0);
    const reqs = srv.seen.slice(before);
    expect(reqs.length, argv.join(" ")).toBe(o.n ?? 1);
    return [reqs, r.out, r.err];
  }

  async function refused(argv: string[]): Promise<string> {
    const before = srv.seen.length;
    const r = await run(argv);
    expect(r.rc, `${argv.join(" ")}: ${r.err}`).toBe(2);
    expect(srv.seen.length).toBe(before);
    expect(r.out).toBe("");
    return r.err;
  }

  it("record --async queues and names the job; without it nothing async is sent", async () => {
    route("POST", "/v1/record", ok(BODIES.record_async, 202));
    let [reqs, out] = await sent(["record", "The team ships on Tuesdays.", "--async", ...AUTH()]);
    expect(reqs[0]?.body).toMatchObject({ space_id: "s", async: true });
    expect(out).toBe("Queued in space 's'. Job: job-rec-1\nCheck it with `anona jobs get job-rec-1 --space s`.\n");
    [, out] = await sent(["record", "x", "--async", "--json", ...AUTH()]);
    expect(oneJson(out)).toEqual(BODIES.record_async);
    route("POST", "/v1/record", ok({ ...BODIES.record_async, job_id: null, status: "stored", memory_id: "m1", memory_ids: ["m1"] }, 201));
    [reqs] = await sent(["record", "x", ...AUTH()]);
    expect(reqs[0]?.body).not.toHaveProperty("async");
  });

  it("record --async --wait polls to the outcome: 0, 3, 1 or 8", async () => {
    route("POST", "/v1/record", ok(BODIES.record_async, 202));
    let polls = 0;
    route("GET", "/v1/spaces/s/jobs/job-rec-1", () => ({ status: 200, body: ++polls < 2 ? BODIES.job_processing : BODIES.job_completed }));
    let [reqs, out, err] = await sent(["record", "x", "--async", "--wait", ...AUTH()], { n: 3 });
    expect(reqs.map((r) => r.method)).toEqual(["POST", "GET", "GET"]);
    expect(out).toContain("completed, 2 memories");
    expect(out).toContain("mem-a, mem-b");
    expect(err).toBe("Waiting for 1 job(s) to finish...\n");
    [, out] = await sent(["record", "x", "--async", "--wait", "--json", ...AUTH()], { n: 2 });
    expect(oneJson(out)).toEqual({ submitted: BODIES.record_async, jobs: [BODIES.job_completed] });
    route("GET", "/v1/spaces/s/jobs/job-rec-1", ok(BODIES.job_completed_empty));
    [, , err] = await sent(["record", "x", "--async", "--wait", ...AUTH()], { rc: 3, n: 2 });
    expect(err).toContain("nothing was stored");
    route("GET", "/v1/spaces/s/jobs/job-rec-1", ok(BODIES.job_failed));
    [, out] = await sent(["record", "x", "--async", "--wait", ...AUTH()], { rc: 1, n: 2 });
    expect(out).toContain("Extraction failed.");
    route("GET", "/v1/spaces/s/jobs/job-rec-1", ok(BODIES.job_processing));
    [, , err] = await sent(["record", "x", "--async", "--wait", "--wait-timeout", "0", ...AUTH()], { rc: 8, n: 2 });
    expect(err).toContain("still running");
  });

  it("record --batch sends the file as items, from a file or stdin, and --wait polls every job", async () => {
    const f = join(home.dir(), "items.jsonl");
    writeFileSync(f, '{"content": "Alice owns billing.", "tags": ["team:billing"]}\n\n"Bob owns search."\n');
    route("POST", "/v1/record/batch", ok(BODIES.record_batch, 202));
    let [reqs, out] = await sent(["record", "--batch", f, "--user-id", "u1", ...AUTH()]);
    expect(reqs[0]?.body).toEqual({
      space_id: "s",
      items: [{ content: "Alice owns billing.", tags: ["team:billing"] }, { content: "Bob owns search." }],
      user_id: "u1",
    });
    expect(out).toContain("job-bat-1, job-bat-2");
    route("GET", "/v1/spaces/s/jobs/job-bat-[12]", ok(BODIES.job_completed));
    [reqs, out] = await sent(["record", "--batch", "-", "--wait", "--json", ...AUTH()], { n: 3, stdin: '"one"\n"two"\n' });
    expect(reqs.slice(1).map((r) => r.path)).toEqual(["/v1/spaces/s/jobs/job-bat-1", "/v1/spaces/s/jobs/job-bat-2"]);
    expect(reqs[0]?.body.items).toEqual([{ content: "one" }, { content: "two" }]);
    expect(oneJson(out).jobs).toHaveLength(2);
  });

  it("retrieve: compound filters and block format reach the body; unset flags stay out", async () => {
    route("POST", "/v1/retrieve", (s) => ({ status: 200, body: s.body.format === "block" ? BODIES.search_block : BODIES.search_results }));
    const groups = '[{"or": [{"tags": ["project:alpha"]}, {"tags": ["project:beta"]}]}, {"not": {"tags": ["status:archived"]}}]';
    let [reqs] = await sent(["retrieve", "ship", "--tag-groups", groups, "--member-id", "me", "--no-prefer-observations", ...AUTH()]);
    expect(reqs[0]?.body).toMatchObject({ tag_groups: JSON.parse(groups), member_id: "me", prefer_observations: false });
    [reqs] = await sent(["retrieve", "ship", ...AUTH()]);
    for (const k of ["tag_groups", "member_id", "prefer_observations", "format", "context_max_tokens"]) expect(reqs[0]?.body).not.toHaveProperty(k);
    let out: string;
    [reqs, out] = await sent(["retrieve", "ship", "--format", "block", "--max-tokens", "500", ...AUTH()]);
    expect(reqs[0]?.body).toMatchObject({ format: "block", context_max_tokens: 500, limit: 10, mode: "accurate" });
    expect(out).toBe(BODIES.search_block.context + "\n");
  });

  it("memories list sends member_id and the tag-group expression as compact JSON", async () => {
    route("GET", "/v1/spaces/s/memories", ok(BODIES.memory_list));
    const [reqs] = await sent(["memories", "list", "--member-id", "me", "--tag-groups", '[{"not": {"tags": ["status:archived"]}}]', ...AUTH()]);
    expect(reqs[0]?.query).toEqual({ limit: "50", offset: "0", member_id: "me", tag_groups: '[{"not":{"tags":["status:archived"]}}]' });
  });

  it("spaces delete: --yes, then a DELETE and a document of what went", async () => {
    route("DELETE", "/v1/spaces/old", () => ({ status: 204 }));
    expect(await refused(["spaces", "delete", "old", ...KEYED()])).toBe(
      "anona: this would delete space 'old' and every memory in it, permanently. Pass --yes to do it. Nothing was sent.\n",
    );
    const [reqs, out] = await sent(["spaces", "delete", "old", "--yes", "--json", ...KEYED()]);
    expect([reqs[0]?.method, reqs[0]?.path]).toEqual(["DELETE", "/v1/spaces/old"]);
    expect(oneJson(out)).toEqual({ deleted: true, space_id: "old" });
  });

  it("documents upload sends every file and the scope in one multipart request, never retried", async () => {
    const a = join(home.dir(), "a.md");
    const b = join(home.dir(), "b.txt");
    writeFileSync(a, "# Alpha notes");
    writeFileSync(b, "Beta notes");
    route("POST", "/v1/spaces/s/documents", ok(BODIES.upload, 202));
    const [reqs, out] = await sent(["documents", "upload", a, b, "--user-id", "u1", "--tags", "q3, notes,", ...AUTH()]);
    const raw = reqs[0]?.raw.toString() as string;
    expect(reqs[0]?.contentType).toMatch(/^multipart\/form-data/);
    expect(raw.match(/name="files"/g)).toHaveLength(2);
    for (const needle of ['filename="a.md"', 'filename="b.txt"', "# Alpha notes", "Beta notes", 'name="user_id"', 'name="tags"', "q3,notes"]) {
      expect(raw).toContain(needle);
    }
    expect(raw).not.toContain('name="agent_id"');
    expect(out).toContain("job-up-1");
    for (const [status, rc] of [[429, 6], [503, 7]] as const) {
      route("POST", "/v1/spaces/s/documents", () => ({ status, body: { error: { code: "x", message: "m" } }, headers: { "retry-after": "0" } }));
      await sent(["documents", "upload", a, ...AUTH()], { rc });
    }
  });

  it("jobs cancel is DELETE /v1/spaces/{id}/jobs/{job_id}; a 409 job_not_cancellable exits 1", async () => {
    route("DELETE", "/v1/spaces/s/jobs/j1", ok({ job_id: "j1", status: "cancelled", cancelled_parts: 2, running_parts: 0 }));
    let [reqs] = await sent(["jobs", "cancel", "j1", "--yes", ...AUTH()]);
    expect([reqs[0]?.method, reqs[0]?.path, reqs[0]?.body]).toEqual(["DELETE", "/v1/spaces/s/jobs/j1", null]);
    route("DELETE", "/v1/spaces/s/jobs/j1", () => ({
      status: 409,
      body: { error: { code: "job_not_cancellable", message: "The job has finished.", request_id: "req_1" } },
    }));
    // A 409 is not retried: DELETE is idempotent, but only a 5xx or 429 is re-sent.
    const [, out, err] = await sent(["jobs", "cancel", "j1", "--yes", ...AUTH()], { rc: 1 });
    expect([out, err]).toEqual(["", "anona: job_not_cancellable: The job has finished. (request_id req_1)\n"]);
  });

  it("record --batch --async is refused: a batch is always queued", async () => {
    const f = join(home.dir(), "one.jsonl");
    writeFileSync(f, '"one"\n');
    expect(await refused(["record", "--batch", f, "--async", ...AUTH()])).toBe(
      "anona: --batch is always queued; drop --async. Nothing was sent.\n",
    );
  });

  it("documents upload refuses an oversized file locally", async () => {
    const big = join(home.dir(), "big.txt");
    writeFileSync(big, Buffer.alloc(25 * 1024 * 1024 + 1));
    const err = await refused(["documents", "upload", big, ...AUTH()]);
    expect(err).toBe("anona: 'big.txt' is 25 MB, over the 25 MB per-file limit. Nothing was sent.\n");
  });

  it("settings set reads, changes only the named fields, and writes the whole record back", async () => {
    let stored: Record<string, unknown> = { ...BODIES.reason_settings };
    route("GET", "/v1/spaces/s/reason-settings", () => ({ status: 200, body: stored }));
    route("PUT", "/v1/spaces/s/reason-settings", (s) => {
      stored = { ...stored, ...s.body };
      return { status: 200, body: { ...stored, space_id: "s" } };
    });
    let [reqs, out] = await sent(["reason-settings", "set", "--model", "fast", ...AUTH()], { n: 2 });
    expect(reqs.map((r) => r.method)).toEqual(["GET", "PUT"]);
    expect(reqs[1]?.body).toEqual({ model: "fast", depth: "thorough" });
    expect(Object.keys(reqs[1]?.body).sort()).toEqual([...BODIES.reason_settings_fields].sort());
    expect(out).toContain("  depth: thorough");
    [reqs] = await sent(["reason-settings", "set", "--clear", "depth", ...AUTH()], { n: 2 });
    expect(reqs[1]?.body).toEqual({ model: "fast", depth: null });

    route("GET", "/v1/spaces/s/chat-settings", ok(BODIES.chat_settings));
    route("PUT", "/v1/spaces/s/chat-settings", (s) => ({ status: 200, body: { ...s.body, space_id: "s" } }));
    [reqs] = await sent(["chat-settings", "set", "--memory-limit", "3", "--no-auto-record", ...AUTH()], { n: 2 });
    const { space_id: _drop, ...want } = BODIES.chat_settings;
    expect(reqs[1]?.body).toEqual({ ...want, memory_limit: 3, auto_record: false });
  });

  it("settings get and reset (reset needs --yes); llm-models lists the catalog", async () => {
    for (const kind of ["chat-settings", "reason-settings", "extraction-settings"]) {
      route("GET", `/v1/spaces/s/${kind}`, ok(BODIES[kind.replace("-", "_")]));
      route("DELETE", `/v1/spaces/s/${kind}`, () => ({ status: 204 }));
      const [, out] = await sent([kind, "get", ...AUTH()]);
      expect(out.startsWith(`${kind} for space 's':\n`)).toBe(true);
      expect(await refused([kind, "reset", ...AUTH()])).toContain(kind);
      const [reqs] = await sent([kind, "reset", "--yes", ...AUTH()]);
      expect(reqs[0]?.method).toBe("DELETE");
    }
    route("GET", "/v1/models", ok(BODIES.llm_models));
    const [, out] = await sent(["llm-models", ...KEYED()]);
    for (const m of BODIES.llm_models.data) expect(out).toContain(m.id);
  });

  it("--retries bounds the re-sends of a 5xx read, and --quiet drops the notices but never the error", async () => {
    route("GET", "/v1/spaces/s/memories", () => ({ status: 503, body: { error: { code: "x", message: "m" } }, headers: { "retry-after": "0" } }));
    let [, , err] = await sent(["memories", "list", "--retries", "1", ...AUTH()], { rc: 7, n: 2 });
    expect(err).toBe("anona: server error 503, retrying in 0 s (1/1)\nanona: x: m\n");
    [, , err] = await sent(["memories", "list", "--retries", "0", ...AUTH()], { rc: 7, n: 1 });
    [, , err] = await sent(["memories", "list", "-q", ...AUTH()], { rc: 7, n: 3 });
    expect(err).toBe("anona: x: m\n");
    route("POST", "/v1/reason", ok({ insights: "An answer." }));
    const [, out, rerr] = await sent(["reason", "q", "--quiet", ...AUTH()]);
    expect([out, rerr]).toEqual(["An answer.\n", ""]);
  });

  it("--timeout gives up on a request that does not answer, exit 7", async () => {
    route("GET", "/v1/usage/me", () => ({ status: 200, hang: true }));
    const r = await run(["usage", "--timeout", "0.2", ...KEYED()]);
    expect(r.rc).toBe(7);
    expect(r.err).toContain("the request timed out");
  });

  it("--profile is a whole separate directory: credentials, config.env, and the lock beside them", async () => {
    route("POST", "/v1/record", ok({ memory_id: "m1", memory_ids: ["m1"], status: "stored" }, 201));
    writeMcpFile({ base: srv.base, tempHours: 5, profile: "work" });
    let [reqs] = await sent(["record", "x", "--profile", "work"]);
    expect(reqs[0]?.auth).toBe("Bearer anona_tmp_TEMPTOKEN");
    // The default profile has no credential, and the named one leaked nothing into it.
    expect((await run(["record", "x"])).rc).toBe(4);
    writeConfigEnv(`ANONA_API_KEY='anona_live_OTHERKEYOTHERKEY'\nANONA_SPACE_ID='other'\nANONA_BASE_URL='${srv.base}'\n`, 0o600, "other");
    [reqs] = await sent(["record", "x", "--profile", "other"]);
    expect([reqs[0]?.auth, reqs[0]?.body.space_id]).toEqual(["Bearer anona_live_OTHERKEYOTHERKEY", "other"]);
    process.env.ANONA_PROFILE = "work";
    try {
      [reqs] = await sent(["record", "x"]);
      expect(reqs[0]?.auth).toBe("Bearer anona_tmp_TEMPTOKEN");
      // logout takes the lock beside the profile's own file, not ~/.anona's.
      const r = await run(["logout", "--force"]);
      expect(r.rc).toBe(0);
      expect(existsSync(join(home.dir(), ".anona", "profiles", "work", "credentials.json"))).toBe(false);
    } finally {
      delete process.env.ANONA_PROFILE;
    }
    expect((await run(["record", "x", "--profile", "../up"])).err).toContain("argument --profile: profile names are letters");
    process.env.ANONA_PROFILE = "a/b";
    try {
      const r = await run(["status"]);
      expect([r.rc, r.err]).toEqual([2, "anona: ANONA_PROFILE: profile names are letters, digits, '-' and '_' (at most 64), not 'a/b'\n"]);
    } finally {
      delete process.env.ANONA_PROFILE;
    }
  });

  it("doctor names the winning credential without printing it, and its exit code names the first problem", async () => {
    route("GET", "/health", ok({ status: "ok" }));
    route("GET", "/v1/spaces/s", ok(BODIES.space));
    route("GET", "/v1/spaces/gone", () => ({ status: 404, body: { error: { code: "space_not_found", message: "m" } } }));
    let r = await run(["doctor", ...AUTH()]);
    expect(r.rc).toBe(0);
    // Too short a key to show even four characters of it.
    expect(r.out).toContain("Credential: anona_live_… from --api-key.");
    expect(r.out).toContain("Space: s (from --space): accessible.");
    expect(r.out).toMatch(/Health: ok \(HTTP 200 in [0-9.]+ s\)\./);
    expect(r.out.endsWith("All good.\n")).toBe(true);
    r = await run(["doctor", "--space", "gone", ...KEYED()]);
    expect(r.rc).toBe(5);
    expect(r.out).toContain("NOT accessible (space_not_found)");
    r = await run(["doctor", "--json"]);
    expect(r.rc).toBe(4);
    const doc = oneJson(r.out);
    expect(Object.keys(doc)).toEqual(["anona_version", "node", "executable", "package_path", "profile", "profile_dir", "credential", "health", "space", "credits", "ok"]);
    expect([doc.credential.kind, doc.ok]).toEqual([null, false]);
  });

  it("doctor reports credits, names an empty balance as a problem, and an unreadable one as unknown", async () => {
    route("GET", "/health", ok({ status: "ok" }));
    route("GET", "/v1/spaces/s", ok(BODIES.space));
    // No usage route yet: the balance is unknown, which is not a problem.
    let r = await run(["doctor", ...AUTH()]);
    expect(r.rc).toBe(0);
    expect(r.out).toContain("Credits: unknown (not_found).");
    expect(r.out.endsWith("All good.\n")).toBe(true);
    route("GET", "/v1/usage/me", ok({ credits_remaining: 0, credits_limit: 1000, credits_used: 1000, rate_limit_per_min: 60 }));
    r = await run(["doctor", ...AUTH()]);
    expect(r.rc).toBe(6);
    expect(r.out).toContain("Credits: 0 remaining of 1000.\n  Problem: credits exhausted; writes and reads will be refused with exit 6.");
    expect(r.out).not.toContain("All good.");
    r = await run(["doctor", "--json", ...AUTH()]);
    expect(r.rc).toBe(6);
    const doc = oneJson(r.out);
    expect(doc.credits).toEqual({ remaining: 0, limit: 1000, used: 1000, exhausted: true, error: null });
    expect(doc.ok).toBe(false);
  });
});
