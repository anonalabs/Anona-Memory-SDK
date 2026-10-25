/**
 * Small pieces of Python behaviour the data commands have to reproduce, so
 * that `anona` from npm and `anona` from pip print the same thing for the same
 * response: the terminal sanitizers, Python's truthiness and `str()`, `%.3g`,
 * and `json.dumps(ensure_ascii=True)`.
 *
 * One limit no code here can lift: `JSON.parse` does not keep the difference
 * between `1` and `1.0`, and reorders integer-like object keys, so a document
 * holding either prints slightly differently from the Python CLI's.
 */

// Memory content and server error text are attacker-influenceable (an agent
// records what it read on a web page; a shared-space member writes the rest; a
// space's id is a user-chosen name). Raw, an ESC sequence could erase and
// rewrite earlier terminal output. Every control character becomes a space --
// newlines and tabs included, so one memory stays one line -- plus C1, the
// Unicode line separators and the bidi overrides.
const UNSAFE = /[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]/g;

// `reason` answers in prose, often several paragraphs of markdown, so the
// one-line rule above is wrong for it. This keeps \n and \t and strips
// everything else the line form strips -- carriage return included: a bare \r
// returns the cursor to column 0 and lets later text overwrite the line
// already printed, which is most of what an escape sequence would buy.
const UNSAFE_BLOCK = /[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]/g;

export function printable(text: unknown): string {
  return pyStr(text).replace(UNSAFE, " ");
}

export function printableBlock(text: unknown): string {
  return pyStr(text).replace(UNSAFE_BLOCK, " ");
}

/** Python's `bool(x)` for a decoded JSON value. */
export function truthy(x: unknown): boolean {
  if (x === null || x === undefined || x === false || x === 0 || x === "") return false;
  if (typeof x === "number" && Number.isNaN(x)) return false;
  if (Array.isArray(x)) return x.length > 0;
  if (typeof x === "object") return Object.keys(x as object).length > 0;
  return true;
}

export function isObject(x: unknown): x is Record<string, unknown> {
  return x !== null && typeof x === "object" && !Array.isArray(x);
}

/** Python's `float.__repr__`: shortest round-trip digits, exponent outside [1e-4, 1e16). */
function floatRepr(n: number): string {
  if (Number.isNaN(n)) return "nan";
  if (!Number.isFinite(n)) return n > 0 ? "inf" : "-inf";
  if (Number.isInteger(n) && Math.abs(n) < 1e16) return `${n < 0 || Object.is(n, -0) ? "-" : ""}${Math.abs(n)}.0`;
  const [mant, expText] = n.toExponential().split("e") as [string, string];
  const exp = Number(expText);
  const neg = mant.startsWith("-");
  const digits = mant.replace("-", "").replace(".", "");
  if (exp < -4 || exp >= 16) {
    const m = digits.length > 1 ? `${digits[0]}.${digits.slice(1)}` : digits;
    return `${neg ? "-" : ""}${m}e${exp < 0 ? "-" : "+"}${String(Math.abs(exp)).padStart(2, "0")}`;
  }
  let body: string;
  if (exp < 0) body = "0." + "0".repeat(-exp - 1) + digits;
  else if (digits.length > exp + 1) body = digits.slice(0, exp + 1) + "." + digits.slice(exp + 1);
  else body = digits + "0".repeat(exp + 1 - digits.length) + ".0";
  return (neg ? "-" : "") + body;
}

/** A JSON number as Python holds it: integral values were ints on the wire. */
function numberStr(n: number): string {
  if (Number.isInteger(n)) return Math.abs(n) < 1e16 ? String(n) : BigInt(n).toString();
  return floatRepr(n);
}

function reprStr(s: string): string {
  const quote = s.includes("'") && !s.includes('"') ? '"' : "'";
  let out = "";
  for (const ch of s) {
    const c = ch.codePointAt(0) as number;
    if (ch === "\\") out += "\\\\";
    else if (ch === quote) out += "\\" + ch;
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else if (c < 0x20 || (c >= 0x7f && c <= 0xa0)) out += "\\x" + c.toString(16).padStart(2, "0");
    else out += ch;
  }
  return quote + out + quote;
}

/** Python's `repr()` of a decoded JSON value. */
export function pyRepr(x: unknown): string {
  if (typeof x === "string") return reprStr(x);
  if (Array.isArray(x)) return "[" + x.map(pyRepr).join(", ") + "]";
  if (isObject(x)) return "{" + Object.entries(x).map(([k, v]) => `${reprStr(k)}: ${pyRepr(v)}`).join(", ") + "}";
  return pyStr(x);
}

/** Python's `str()` of a decoded JSON value. */
export function pyStr(x: unknown): string {
  if (x === null || x === undefined) return "None";
  if (x === true) return "True";
  if (x === false) return "False";
  if (typeof x === "number") return numberStr(x);
  if (typeof x === "string") return x;
  if (x instanceof Error) return x.message;
  if (Array.isArray(x) || isObject(x)) return pyRepr(x);
  return String(x);
}

/** `format(x, ".3g")`. */
export function g3(x: number): string {
  if (Number.isNaN(x)) return "nan";
  if (!Number.isFinite(x)) return x > 0 ? "inf" : "-inf";
  if (x === 0) return Object.is(x, -0) ? "-0" : "0";
  const [mant, expText] = x.toExponential(2).split("e") as [string, string];
  const exp = Number(expText);
  const strip = (s: string) => (s.includes(".") ? s.replace(/0+$/, "").replace(/\.$/, "") : s);
  if (exp < -4 || exp >= 3) {
    return `${strip(mant)}e${exp < 0 ? "-" : "+"}${String(Math.abs(exp)).padStart(2, "0")}`;
  }
  return strip(x.toFixed(Math.max(0, 2 - exp)));
}

function jsonStr(s: string): string {
  let out = '"';
  for (let i = 0; i < s.length; i++) {
    const ch = s[i] as string;
    const c = s.charCodeAt(i);
    if (ch === '"') out += '\\"';
    else if (ch === "\\") out += "\\\\";
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else if (ch === "\b") out += "\\b";
    else if (ch === "\f") out += "\\f";
    // Python's ESCAPE_ASCII: anything outside printable ASCII, DEL included.
    // Astral characters are already surrogate pairs in a JS string.
    else if (c < 0x20 || c > 0x7e) out += "\\u" + c.toString(16).padStart(4, "0");
    else out += ch;
  }
  return out + '"';
}

/**
 * `json.dumps(doc, ensure_ascii=True)`: Python's default separators, and every
 * control and non-ASCII character escaped, so the document is terminal-safe
 * without a sanitizer and still round-trips exactly. `sortKeys` and `compact`
 * are `sort_keys=True` and `separators=(",", ":")`.
 */
export function pyJson(x: unknown, opts: { sortKeys?: boolean; compact?: boolean } = {}): string {
  const item = opts.compact ? "," : ", ";
  const kv = opts.compact ? ":" : ": ";
  const dump = (v: unknown): string => {
    if (v === null || v === undefined) return "null";
    if (v === true) return "true";
    if (v === false) return "false";
    if (typeof v === "number") {
      if (Number.isNaN(v)) return "NaN";
      if (!Number.isFinite(v)) return v > 0 ? "Infinity" : "-Infinity";
      return numberStr(v);
    }
    if (typeof v === "string") return jsonStr(v);
    if (Array.isArray(v)) return "[" + v.map(dump).join(item) + "]";
    if (isObject(v)) {
      const entries = Object.entries(v);
      if (opts.sortKeys) entries.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
      return "{" + entries.map(([k, val]) => `${jsonStr(k)}${kv}${dump(val)}`).join(item) + "}";
    }
    return jsonStr(String(v));
  };
  return dump(x);
}
