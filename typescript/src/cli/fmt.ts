/**
 * Small pieces the plain-text renderers share (verbs.ts, resources.ts): ids and
 * server strings made safe for a terminal, defensive field reads, and the two
 * renderers that more than one command prints. Mirrors the helpers at the
 * bottom of anona/cli/verbs.py.
 */
import { redact } from "./credential.js";
import type { DataIO } from "./memory.js";
import { isObject, printable, printableBlock, pyStr, truthy } from "./py.js";

export type Writer = DataIO;

export type Values = Record<string, unknown>;
export type Render = (io: Writer, doc: unknown, args: Values, space: string | undefined) => void;

export const d = (doc: unknown): Record<string, unknown> => (isObject(doc) ? doc : {});

export function rows(doc: unknown, field: string): Record<string, unknown>[] {
  const r = d(doc)[field];
  return Array.isArray(r) ? r.filter(isObject) : [];
}

/** Redacted as well: an id argument echoed back may be a key pasted into the wrong place. */
export function p(value: unknown): string {
  return value === null || value === undefined || value === "" ? "-" : printable(redact(pyStr(value)));
}

export const or = (...xs: unknown[]) => xs.find(truthy) ?? xs[xs.length - 1];

/** A non-negative integer from the server, or 0. Never a bool. */
export const count = (v: unknown) => (typeof v === "number" && Number.isInteger(v) && v >= 0 ? v : 0);

export const out = (io: Writer, line: string) => void io.stdout.write(line + "\n");

/** Only what the caller named: an unset flag is omitted, never sent as a default. */
export function given(values: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(values).filter(([, v]) => v !== undefined && v !== null));
}

export const renderProfile: Render = (io, doc, _args, space) => {
  const x = d(doc);
  out(io, `Space: ${p(or(x.space_id, space))}`);
  out(io, `Mission: ${p(x.mission)}`);
  const disp = x.disposition;
  if (isObject(disp) && Object.keys(disp).length) {
    out(io, "Disposition: " + Object.entries(disp).map(([k, v]) => `${printable(k)}=${p(v)}`).join(", "));
  }
};

export const renderModel: Render = (io, doc, args) => {
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
};

/**
 * The code a presigned download link's non-2xx answer is carried under. It is
 * reported as a transport failure (exit 7) with the advice to ask for a fresh
 * link, never mapped as if the API had answered that status.
 */
export const LINK_REFUSED = "download_link_refused";
