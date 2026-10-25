/**
 * `--wait`: poll background jobs until they finish. Mirrors anona/cli/jobs.py.
 *
 * Shared by `record --async`, `record --batch` and `documents upload`. Every
 * poll is a GET of the job, which costs no credits and is safe to retry.
 *
 * The submit and the polls are separate calls on purpose: `invoke` renews an
 * expired command-line login after a 401 and runs its function once more, and
 * were the submit and the polling one function, a token expiring mid-poll
 * would re-run the submit too, and a write would land twice.
 */
import { redact } from "./credential.js";
import * as exits from "./exits.js";
import { invoke, notice, type Bearer, type DataIO, type Invoked } from "./memory.js";
import { isObject, printable, pyStr, truthy } from "./py.js";

// What a job can still become: anything else is final.
const RUNNING = new Set(["pending", "processing", "running", "queued"]);

/**
 * What `jobs get` and a --wait say when the server reports a job it does not
 * hold. The API answers that with HTTP 200 and `"status": "not_found"`, so
 * nothing throws and the CLI turns it into the not-found outcome itself.
 */
export const NOT_FOUND_LINE = "anona: job_not_found: No job with that id in this space.";

/** The server answered, and the answer is that it holds no such job. */
export function isNotFound(doc: unknown): boolean {
  return isObject(doc) && status(doc as Doc) === "not_found";
}

/** Poll back-off, in seconds. Mutable so a test can shorten it. */
export const jobTuning = { firstInterval: 1.0, maxInterval: 5.0 };

type Doc = Record<string, unknown>;

const status = (d: Doc) => (truthy(d.status) ? pyStr(d.status) : "");

function p(value: unknown): string {
  return value === null || value === undefined || value === "" ? "-" : printable(redact(pyStr(value)));
}

/**
 * The exit code a set of finished jobs adds up to: a failed or cancelled job
 * fails the command; a job the server no longer knows is NOT_FOUND; anything
 * still running is STILL_RUNNING.
 */
export function outcome(docs: Doc[]): number {
  const statuses = docs.map(status);
  if (statuses.some((s) => RUNNING.has(s))) return exits.STILL_RUNNING;
  if (statuses.some((s) => s === "failed" || s === "cancelled")) return exits.FAILED;
  if (statuses.some((s) => s === "not_found")) return exits.NOT_FOUND;
  return exits.OK;
}

/**
 * Poll until every job is final or `timeout` seconds pass. Always polls at
 * least once, so `--wait-timeout 0` is a single status check.
 */
export async function wait(
  io: DataIO,
  baseUrl: string,
  bearer: Bearer,
  space: string | undefined,
  ids: string[],
  timeout: number,
): Promise<Invoked<Doc[]>> {
  const deadline = performance.now() + Math.max(0, timeout) * 1000;
  const docs = new Map<string, Doc>();
  let pending = [...ids];
  let interval = jobTuning.firstInterval;
  let announced = false;
  for (;;) {
    for (const id of pending) {
      const r = await invoke(io, baseUrl, bearer, (c) => c.getJob({ spaceId: pyStr(space), jobId: id }));
      if (!r.done) return r;
      docs.set(id, isObject(r.value) ? (r.value as Doc) : { job_id: id });
    }
    pending = ids.filter((id) => RUNNING.has(status(docs.get(id) as Doc)));
    if (!pending.length) break;
    // Announced before the deadline check: whether the first poll left any
    // time is a race, and the notice must not depend on it.
    if (!announced) {
      notice(io, `Waiting for ${pending.length} job(s) to finish...`);
      announced = true;
    }
    const remaining = (deadline - performance.now()) / 1000;
    if (remaining <= 0) break;
    await new Promise((r) => setTimeout(r, Math.min(interval, remaining) * 1000));
    interval = Math.min(interval * 1.5, jobTuning.maxInterval);
  }
  return { done: true, value: ids.map((id) => docs.get(id) as Doc) };
}

/** One job, as `anona jobs get` prints it. */
export function render(io: DataIO, doc: Doc, jobId?: string): void {
  let line = `Job ${p(truthy(doc.job_id) ? doc.job_id : jobId)}: ${p(doc.status)}`;
  if (doc.memory_count !== null && doc.memory_count !== undefined) line += `, ${p(doc.memory_count)} memories`;
  if (doc.items_submitted !== null && doc.items_submitted !== undefined) line += ` from ${p(doc.items_submitted)} items`;
  io.stdout.write(line + ".\n");
  if (truthy(doc.error)) io.stdout.write(`Error: ${p(doc.error)}\n`);
  const memoryIds = doc.memory_ids;
  if (Array.isArray(memoryIds) && memoryIds.length) io.stdout.write("Memory ids: " + memoryIds.map(p).join(", ") + "\n");
}

/**
 * Every job completed and the server says it stored no memory at all. Only
 * when every count is known: an unknown count is not evidence of nothing.
 */
export function storedNothing(docs: Doc[]): boolean {
  const counts = docs.map((d) => d.memory_count);
  return (
    docs.length > 0 &&
    docs.every((d) => d.status === "completed") &&
    counts.every((c) => typeof c === "number" && Number.isInteger(c)) &&
    counts.reduce((a: number, c) => a + (c as number), 0) === 0
  );
}
