/**
 * The resource verbs: spaces, memories, documents, jobs, profile, model,
 * user-profile, usage, llm-models, and the three space settings. Mirrors
 * anona/cli/verbs.py.
 *
 * Each is a thin wrapper over one client call, sharing everything else with
 * record/retrieve/reason (memory.ts): the same credential, the same `--json`
 * contract (one document on stdout, nothing there on failure), the same
 * one-line error rendering and exit codes, and the same sanitizers on every
 * string the server sent.
 *
 * Every verb that destroys something -- `memories delete`, `jobs cancel`,
 * `spaces delete`, `documents delete` and the three `... reset` -- refuses to
 * run without `--yes`. They never prompt: the caller is usually an agent with
 * no terminal to answer on, and a prompt it cannot see reads as a hung command.
 */
import { statSync } from "node:fs";
import { basename, extname } from "node:path";
import { readErrorName, SETTINGS_FIELDS } from "./args.js";
import { redact } from "./credential.js";
import * as exits from "./exits.js";
import { readFileSync } from "node:fs";
import * as jobs from "./jobs.js";
import { afterQueued, emitJson, given, invoke, jobIds, tokenAndSpace, USAGE_EXIT, type Bearer, type CliClient, type DataIO } from "./memory.js";
import { isObject, printable, printableBlock, pyJson, pyStr, truthy } from "./py.js";

export type Verb = [string, string | null];
type Values = Record<string, unknown>;

const key = (v: Verb) => `${v[0]} ${v[1] ?? ""}`.trim();

/** The verbs that address one space, and so need one named with an account credential. */
export const SPACE_VERBS = new Set([
  "memories list",
  "memories edit",
  "memories delete",
  "memories history",
  "jobs get",
  "jobs cancel",
  "profile",
  "model",
  "user-profile",
  "documents upload",
  "documents list",
  "documents get",
  "documents delete",
  ...Object.keys(SETTINGS_FIELDS).flatMap((k) => [`${k} get`, `${k} set`, `${k} reset`]),
]);

export const needsSpace = (v: Verb) => SPACE_VERBS.has(key(v));

export const DEFAULT_WAIT = 300;

/** What a destructive verb would do, or null when the verb destroys nothing. */
function destroyMessage(verb: Verb, args: Values, space: string | undefined): string | null {
  const where = `in space '${p(space || "default")}'`;
  const k = key(verb);
  if (k === "memories delete") return `delete memory ${p(args.memory_id)} ${where}`;
  if (k === "jobs cancel") return `cancel the queued parts of job ${p(args.job_id)} ${where}`;
  if (k === "spaces delete") return `delete space '${p(args.space_id)}' and every memory in it, permanently`;
  if (k === "documents delete") return `delete document ${p(args.document_id)} and the memories extracted from it ${where}`;
  if (verb[0] in SETTINGS_FIELDS && verb[1] === "reset") {
    return `reset the ${verb[0]} of space '${p(space || "default")}' to the platform defaults`;
  }
  return null;
}

function isFile(path: string): boolean {
  try {
    return statSync(path).isFile();
  } catch {
    return false;
  }
}

/**
 * Usage errors the server would certainly reject, caught before the
 * credential is looked up, so they exit 2 and send nothing.
 */
export function precheck(io: DataIO, verb: Verb, args: Values): number | null {
  const k = key(verb);
  if (k === "spaces create") return rejectBlank(io, "space name", args.name as string);
  if (k === "memories edit") return rejectBlank(io, "memory text", args.text as string);
  if (k === "documents upload") {
    for (const path of args.paths as string[]) {
      if (!isFile(path)) {
        io.stderr.write(`anona: ${p(path)} is not a readable file. Nothing was sent.\n`);
        return USAGE_EXIT;
      }
    }
    if (args.wait_timeout !== undefined && !args.wait) {
      io.stderr.write("anona: --wait-timeout needs --wait. Nothing was sent.\n");
      return USAGE_EXIT;
    }
    return null;
  }
  if (verb[0] in SETTINGS_FIELDS && verb[1] === "set") {
    const changes = settingsChanges(io, verb[0], args);
    if (typeof changes === "number") return changes;
    if (!Object.keys(changes).length) {
      io.stderr.write(`anona: name at least one field to change (see \`anona ${verb[0]} set --help\`). Nothing was sent.\n`);
      return USAGE_EXIT;
    }
  }
  return null;
}

/** {field: new value} from a `... set` command line; null means clear it. */
function settingsChanges(io: DataIO, kind: string, args: Values): Record<string, unknown> | number {
  const changes: Record<string, unknown> = {};
  for (const field of SETTINGS_FIELDS[kind] as readonly string[]) {
    const value = args[field];
    if (value !== undefined && value !== null) changes[field] = value;
  }
  for (const field of (args.clear as string[] | undefined) ?? []) {
    if (field in changes) {
      io.stderr.write(`anona: ${field} is both set and cleared. Nothing was sent.\n`);
      return USAGE_EXIT;
    }
    changes[field] = null;
  }
  return changes;
}

function rejectBlank(io: DataIO, what: string, value: string): number | null {
  if (value.trim()) return null;
  io.stderr.write(`anona: the ${what} is empty. Nothing was sent.\n`);
  return USAGE_EXIT;
}

export async function runVerb(
  io: DataIO,
  verb: Verb,
  args: Values,
  baseUrl: string,
  space: string | undefined,
  apiKey: string | undefined,
): Promise<number> {
  const what = destroyMessage(verb, args, space);
  if (what !== null && !args.yes) {
    io.stderr.write(`anona: this would ${what}. Pass --yes to do it. Nothing was sent.\n`);
    return USAGE_EXIT;
  }
  let bearer: Bearer;
  if (apiKey !== undefined) {
    bearer = { token: apiKey, space };
  } else {
    const got = await tokenAndSpace(io, space);
    if (typeof got === "number") return got;
    bearer = got;
    space = bearer.space;
  }
  if (key(verb) === "documents upload") return runUpload(io, args, baseUrl, bearer, space);
  const r = await invoke(io, baseUrl, bearer, (client) => call(io, client, verb, args, space));
  if (!r.done) return r.exit;
  if (key(verb) === "jobs get" && jobs.isNotFound(r.value)) {
    // HTTP 200 on the wire, but a not-found outcome: report it the way a 404
    // is reported, nothing on stdout, --json or not.
    io.stderr.write(jobs.NOT_FOUND_LINE + "\n");
    return exits.NOT_FOUND;
  }
  if (args.json) {
    emitJson(io, r.value);
    return 0;
  }
  (RENDER[key(verb)] as Render)(io, r.value, args, space);
  return 0;
}

/** The SDK's per-file ceilings, checked before anything is sent. */
const MAX_FILE_BYTES = 25 * 1024 * 1024;
const MAX_VIDEO_BYTES = 64 * 1024 * 1024;
const VIDEO_EXTENSIONS = new Set([".mp4", ".mov", ".webm", ".mkv"]);

/**
 * `documents upload`: every file in one multipart request. Never re-sent (a
 * multipart body is not), and a 413 or 415 comes back as its code with the
 * server's message saying the limit.
 */
async function runUpload(io: DataIO, args: Values, baseUrl: string, bearer: Bearer, space: string | undefined): Promise<number> {
  if (space === undefined) throw new Error("documents upload needs a space");
  const tags = args.tags
    ? (args.tags as string)
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean)
    : null;
  const files: Array<[string, Uint8Array]> = [];
  for (const path of args.paths as string[]) {
    let content: Uint8Array;
    try {
      content = readFileSync(path);
    } catch (e) {
      io.stderr.write(`anona: could not read a file (${readErrorName(e)}). Nothing was sent.\n`);
      return exits.USAGE;
    }
    const name = basename(path);
    const cap = VIDEO_EXTENSIONS.has(extname(name).toLowerCase()) ? MAX_VIDEO_BYTES : MAX_FILE_BYTES;
    if (content.byteLength > cap) {
      const mb = 1024 * 1024;
      const detail = `'${name}' is ${Math.floor(content.byteLength / mb)} MB, over the ${Math.floor(cap / mb)} MB per-file limit.`;
      io.stderr.write(`anona: ${p(detail)} Nothing was sent.\n`);
      return exits.USAGE;
    }
    files.push([name, content]);
  }
  const fields: Record<string, string> = {};
  if (args.strategy !== undefined) fields.strategy = args.strategy as string;
  if (tags && tags.length) fields.tags = tags.join(",");
  for (const k of ["user_id", "agent_id", "session_id"]) if (args[k] !== undefined) fields[k] = args[k] as string;
  const build = () => {
    const form = new FormData();
    for (const [k, v] of Object.entries(fields)) form.append(k, v);
    for (const [name, content] of files) form.append("files", new Blob([content.slice()]), name);
    return form;
  };
  const r = await invoke(io, baseUrl, bearer, (c) =>
    c.raw<unknown>({ method: "POST", path: `/v1/spaces/${encodeURIComponent(space)}/documents`, form: build(), idempotent: false }),
  );
  if (!r.done) return r.exit;
  const wait = args.wait ? ((args.wait_timeout as number | undefined) ?? DEFAULT_WAIT) : undefined;
  // An upload's job has no item count to judge "nothing stored" by.
  return afterQueued(io, baseUrl, bearer, space, r.value, jobIds(r.value), {
    asJson: Boolean(args.json),
    wait,
    nothingStoredIsAnOutcome: false,
  });
}

async function call(io: DataIO, client: CliClient, verb: Verb, args: Values, space: string | undefined): Promise<unknown> {
  const k = key(verb);
  const str = (name: string) => args[name] as string;
  const seg = encodeURIComponent;
  // The whole response: listSpaces() returns only the rows and drops `total`,
  // and --json promises the API's document.
  if (k === "spaces list") return client.raw({ method: "GET", path: "/v1/spaces/" });
  if (k === "spaces create") return client.createSpace({ name: str("name"), ...(args.description !== undefined ? { description: str("description") } : {}) });
  if (k === "spaces get") return client.getSpace(str("space_id"));
  if (k === "spaces delete") {
    await client.raw({ method: "DELETE", path: `/v1/spaces/${seg(str("space_id"))}`, expectNoContent: true });
    return { deleted: true, space_id: str("space_id") };
  }
  // /v1/usage/me, the key-scoped snapshot: /v1/usage is a dashboard session
  // route and refuses an API key.
  if (k === "usage") return client.getUsage();
  if (k === "llm-models") return client.raw({ method: "GET", path: "/v1/models" });
  if (space === undefined) throw new Error(`${k} needs a space`);
  if (k === "memories list") {
    return client.raw({
      method: "GET",
      path: `/v1/spaces/${seg(space)}/memories`,
      query: given({
        limit: args.limit ?? 50,
        offset: args.offset ?? 0,
        q: args.q,
        type: args.memory_type,
        prefer_observations: args.prefer_observations,
        user_id: args.user_id,
        agent_id: args.agent_id,
        session_id: args.session_id,
        member_id: args.member_id,
        // A GET, so the expression travels as a JSON string.
        tag_groups: args.tag_groups === undefined ? undefined : pyJson(args.tag_groups, { compact: true }),
      }),
    });
  }
  if (k === "documents list") {
    return client.raw({
      method: "GET",
      path: `/v1/spaces/${seg(space)}/documents`,
      query: given({ limit: args.limit ?? 100, offset: args.offset ?? 0, source: args.source }),
    });
  }
  if (k === "documents get") return client.raw({ method: "GET", path: `/v1/spaces/${seg(space)}/documents/${seg(str("document_id"))}` });
  if (k === "documents delete") {
    await client.raw({ method: "DELETE", path: `/v1/spaces/${seg(space)}/documents/${seg(str("document_id"))}`, expectNoContent: true });
    return { deleted: true, document_id: str("document_id"), space_id: space };
  }
  if (verb[0] in SETTINGS_FIELDS) {
    const path = `/v1/spaces/${seg(space)}/${verb[0]}`;
    if (verb[1] === "get") return client.raw({ method: "GET", path });
    if (verb[1] === "set") {
      // The PUT is a full replace, so read the stored record, apply the
      // changes and write every field back. Only the request model's fields
      // are sent: the GET carries `space_id`, and the API rejects unknown keys.
      const changes = settingsChanges(io, verb[0], args) as Record<string, unknown>;
      const got = await client.raw<unknown>({ method: "GET", path });
      const current = isObject(got) ? got : {};
      const body: Record<string, unknown> = {};
      for (const field of SETTINGS_FIELDS[verb[0]] as readonly string[]) body[field] = current[field] ?? null;
      Object.assign(body, changes);
      const doc = await client.raw<unknown>({ method: "PUT", path, body });
      return truthy(doc) ? doc : {};
    }
    await client.raw({ method: "DELETE", path, expectNoContent: true });
    return { reset: true, space_id: space, settings: verb[0] };
  }
  if (k === "memories edit") {
    return client.updateMemory({
      spaceId: space,
      memoryId: str("memory_id"),
      text: str("text"),
      ...(args.reason !== undefined ? { reason: str("reason") } : {}),
    });
  }
  if (k === "memories delete") {
    await client.deleteMemory({ spaceId: space, memoryId: str("memory_id") });
    // 204, no body: the document says what went.
    return { deleted: true, memory_id: str("memory_id"), space_id: space };
  }
  if (k === "memories history") return client.getMemoryHistory({ spaceId: space, memoryId: str("memory_id") });
  if (k === "jobs get") return client.getJob({ spaceId: space, jobId: str("job_id") });
  if (k === "jobs cancel") return client.cancelJob({ spaceId: space, jobId: str("job_id") });
  if (k === "profile") return client.getSpaceProfile(space);
  if (k === "model") return client.getMemoryModel({ spaceId: space, modelId: str("model_id") });
  if (k === "user-profile") {
    return client.getUserProfile({
      spaceId: space,
      userId: str("user_id"),
      ...(given({ limit: args.limit, offset: args.offset }) as { limit?: number; offset?: number }),
    });
  }
  throw new Error(`unknown verb ${k}`);
}

// --- plain-text output ------------------------------------------------------------
//
// One line per item, ids in full (an agent copies them into the next command),
// every server string through printable. Fields are read defensively, so one
// the server stops sending prints as missing rather than throwing.

type Render = (io: DataIO, doc: unknown, args: Values, space: string | undefined) => void;

const d = (doc: unknown): Record<string, unknown> => (isObject(doc) ? doc : {});

function rows(doc: unknown, field: string): Record<string, unknown>[] {
  const r = d(doc)[field];
  return Array.isArray(r) ? r.filter(isObject) : [];
}

/** Redacted as well: an id argument echoed back may be a key pasted into the wrong place. */
function p(value: unknown): string {
  return value === null || value === undefined || value === "" ? "-" : printable(redact(pyStr(value)));
}

const or = (...xs: unknown[]) => xs.find(truthy) ?? xs[xs.length - 1];

/** A non-negative integer from the server, or 0. Never a bool. */
const count = (v: unknown) => (typeof v === "number" && Number.isInteger(v) && v >= 0 ? v : 0);

function spaceLine(s: Record<string, unknown>): string {
  let line = p(or(s.qualified_id, s.space_id));
  if (truthy(s.shared_by)) line += `  (shared by ${p(s.shared_by)})`;
  if (truthy(s.description)) line += `  ${p(s.description)}`;
  return line;
}

function memoryLine(m: Record<string, unknown>): string {
  const content = or(m.content, m.text, "");
  return `${p(or(m.id, m.memory_id))}  ${p(m.type)}  ${printable(content)}`;
}

const out = (io: DataIO, line: string) => void io.stdout.write(line + "\n");

function renderSettings(io: DataIO, doc: unknown, args: Values, space: string | undefined, kind: string): void {
  const x = d(doc);
  out(io, `${printable(kind)} for space '${p(or(x.space_id, space))}':`);
  for (const field of SETTINGS_FIELDS[kind] as readonly string[]) {
    const value = x[field];
    let shown: string;
    if (value === null || value === undefined) shown = "(platform default)";
    else if (Array.isArray(value) || isObject(value)) shown = printable(pyJson(value));
    else shown = p(value);
    out(io, `  ${field}: ${shown}`);
  }
}

const RENDER: Record<string, Render> = {
  "spaces list": (io, doc) => {
    const r = rows(doc, "spaces");
    if (!r.length) out(io, "No spaces.");
    for (const s of r) out(io, spaceLine(s));
  },
  "spaces create": (io, doc, args) => {
    const x = d(doc);
    let line = `Created space '${p(or(x.space_id, args.name))}'.`;
    if (truthy(x.space_type)) line += ` Type: ${p(x.space_type)}.`;
    out(io, line);
  },
  "spaces get": (io, doc) => {
    const x = d(doc);
    for (const k of ["space_id", "qualified_id", "name", "description", "shared_by", "created_at"]) {
      if (x[k] !== null && x[k] !== undefined) out(io, `${k}: ${p(x[k])}`);
    }
  },
  "spaces delete": (io, _doc, args) => out(io, `Deleted space '${p(args.space_id)}'.`),
  "memories list": (io, doc, _args, space) => {
    const x = d(doc);
    const r = rows(x, "items");
    if (!r.length) {
      out(io, `No memories in space '${p(space)}'.`);
      return;
    }
    for (const m of r) out(io, memoryLine(m));
    const off = count(x.offset);
    out(io, `Showing ${off + 1}-${off + r.length} of ${p(x.total)}.`);
  },
  "memories edit": (io, doc, args) => out(io, `Updated memory ${p(or(d(doc).id, args.memory_id))}.`),
  "memories delete": (io, _doc, args, space) => out(io, `Deleted memory ${p(args.memory_id)} from space '${p(space)}'.`),
  "memories history": (io, doc, args) => {
    const r = rows(doc, "history");
    if (!r.length) out(io, `No earlier versions of memory ${p(args.memory_id)}.`);
    for (const h of r) {
      let line = `${p(h.changed_at)}  was: ${p(h.previous_content)}`;
      if (truthy(h.reason)) line += `  (reason: ${p(h.reason)})`;
      out(io, line);
    }
  },
  "documents list": (io, doc, _args, space) => {
    const x = d(doc);
    const r = rows(x, "documents");
    if (!r.length) {
      out(io, `No documents in space '${p(space)}'.`);
      return;
    }
    for (const row of r) {
      let line = `${p(row.document_id)}  ${p(row.source)}`;
      if (row.memory_count !== null && row.memory_count !== undefined) line += `  ${p(row.memory_count)} memories`;
      if (truthy(row.created_at)) line += `  ${p(row.created_at)}`;
      out(io, line);
    }
    const off = count(x.offset);
    out(io, `Showing ${off + 1}-${off + r.length} of ${p(x.total)}.`);
  },
  "documents get": (io, doc, args) => {
    const x = d(doc);
    out(io, `Document ${p(or(x.id, x.document_id, args.document_id))}`);
    for (const [k, v] of Object.entries(x)) {
      if (k === "id" || k === "document_id" || Array.isArray(v) || isObject(v)) continue;
      out(io, `${printable(k)}: ${p(v)}`);
    }
  },
  "documents delete": (io, _doc, args, space) => out(io, `Deleted document ${p(args.document_id)} from space '${p(space)}'.`),
  "jobs get": (io, doc, args) => jobs.render(io, d(doc), args.job_id as string),
  "jobs cancel": (io, doc, args) => {
    const x = d(doc);
    out(
      io,
      `Cancelled ${p(x.cancelled_parts)} queued part(s) of job ${p(or(x.job_id, args.job_id))}; ` +
        `${p(x.running_parts)} already running will finish.`,
    );
  },
  profile: (io, doc, _args, space) => {
    const x = d(doc);
    out(io, `Space: ${p(or(x.space_id, space))}`);
    out(io, `Mission: ${p(x.mission)}`);
    const disp = x.disposition;
    if (isObject(disp) && Object.keys(disp).length) {
      out(io, "Disposition: " + Object.entries(disp).map(([k, v]) => `${printable(k)}=${p(v)}`).join(", "));
    }
  },
  model: (io, doc, args) => {
    const x = d(doc);
    out(io, `${p(x.name)} (${p(or(x.id, args.model_id))})`);
    if (truthy(x.query)) out(io, `Query: ${p(x.query)}`);
    const content = x.content;
    if (typeof content === "string" && content.trim()) {
      // Markdown: paragraph breaks kept, as `reason` keeps them.
      out(io, "");
      out(io, printableBlock(content));
    } else {
      out(io, "No content yet: the first refresh has not finished, or it was cleared.");
    }
  },
  "user-profile": (io, doc, args, space) => {
    const x = d(doc);
    out(
      io,
      `User ${p(or(x.user_id, args.user_id))} in space '${p(or(x.space_id, space))}': ` +
        `${p(x.memory_count)} memories, first seen ${p(x.first_seen)}, last active ${p(x.last_active)}.`,
    );
    for (const m of rows(x, "memories")) out(io, memoryLine(m));
  },
  usage: (io, doc) => {
    const x = d(doc);
    let line = `Credits: ${p(x.credits_remaining)} remaining of ${p(x.credits_limit)} (${p(x.credits_used)} used`;
    if (truthy(x.credits_banked)) line += `, ${p(x.credits_banked)} banked`;
    out(io, line + ").");
    out(io, `Rate limit: ${p(x.rate_limit_per_min)} requests a minute.`);
  },
  "llm-models": (io, doc) => {
    const r = rows(doc, "data");
    if (!r.length) out(io, "No models listed.");
    for (const m of r) {
      let line = `${p(m.id)}  ${p(m.display_name)}`;
      line += `  ${p(m.credits_per_1k_input)}/${p(m.credits_per_1k_output)} credits per 1K in/out`;
      const flags: string[] = [];
      if (truthy(m.default)) flags.push("default");
      if (truthy(m.alias)) flags.push(`alias ${p(m.alias)}`);
      if (m.supports_memory_operations === false) flags.push("chat proxy only");
      if (flags.length) line += `  (${flags.join(", ")})`;
      out(io, line);
    }
  },
};
for (const kind of Object.keys(SETTINGS_FIELDS)) {
  RENDER[`${kind} get`] = (io, doc, args, space) => renderSettings(io, doc, args, space, kind);
  RENDER[`${kind} set`] = (io, doc, args, space) => renderSettings(io, doc, args, space, kind);
  RENDER[`${kind} reset`] = (io, _doc, _args, space) =>
    out(io, `Reset the ${printable(kind)} of space '${p(space)}' to the platform defaults.`);
}
