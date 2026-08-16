/**
 * An error returned by the Anona API.
 *
 * The API wraps failures as `{"error": {"code", "message"}}` — not
 * FastAPI's `{"detail": ...}` — so `code` is the stable machine-readable
 * handle and `message` the human one.
 *
 * A 503 carrying no `requestId` may be a Cloudflare-mangled 502 or 504: the
 * edge strips the body of those two statuses, so the API rewrites them to
 * 503 before they leave. Report such a failure with a timestamp rather than
 * treating it as a malformed response.
 */
export class AnonaError extends Error {
  readonly statusCode: number;
  readonly code?: string;
  readonly requestId?: string;
  readonly detail: unknown;

  constructor(args: {
    statusCode: number;
    message: string;
    code?: string;
    requestId?: string;
    detail?: unknown;
  }) {
    super(`Anona API error ${args.statusCode}: ${args.message}`);
    this.name = "AnonaError";
    this.statusCode = args.statusCode;
    this.code = args.code;
    this.requestId = args.requestId;
    this.detail = args.detail;
  }
}
