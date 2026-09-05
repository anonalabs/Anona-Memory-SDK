import { AnonaError } from "./errors.js";

export interface ResolvedOptions {
  apiKey: string;
  baseUrl: string;
  timeoutMs: number;
  maxRetries: number;
  fetchImpl: typeof fetch;
}

export interface RequestOptions {
  method: string;
  path: string;
  query?: Record<string, unknown>;
  body?: unknown;
  form?: FormData;
  signal?: AbortSignal;
  expectNoContent?: boolean;
  /**
   * Whether this call is safe to retry automatically. Defaults to `true`.
   *
   * A retry only replays work; it must never duplicate it. Reads are safe. The
   * create endpoints — record, batch record, file upload, create-space,
   * create-webhook — are not: a 5xx or a timeout can arrive after the write has
   * already landed, so replaying it stores the memory twice, mints a second
   * webhook (whose secret the caller never sees), or trips the create-space
   * member row's unique constraint. `reason` opts out for a different reason —
   * a slow reflect must not be replayed into two or three overlapping runs at a
   * space already under load. Those pass `idempotent: false`, which limits their
   * retries to the one failure that proves nothing happened server-side (a 429,
   * rejected before the work is attempted).
   */
  idempotent?: boolean;
  /**
   * Per-attempt timeout override, in ms. Falls back to the client default.
   * `reason` raises it to the API's ~90s reflect budget so a normal
   * multi-iteration reflect is not cut off mid-flight — safe only because
   * `reason` is also non-idempotent, so the long attempt is never replayed.
   */
  timeoutMs?: number;
}

/**
 * Percent-encode one path segment.
 *
 * A space's id IS its name, so it can legally contain characters that are
 * structural in a URL. Without this, a space named `a/b` addresses
 * `/v1/spaces/a/b/graph` — a different route — and `x?y` truncates the path
 * into a query string. Every path-based method is affected, delete included,
 * so such a space could be created and then never reached again.
 */
export function seg(value: string): string {
  return encodeURIComponent(String(value));
}

// 429 is always safe to retry: the API rejects it before the write reaches
// the engine, so nothing was stored. A 5xx is only safe to retry when the call
// is idempotent — replaying a create (record / batch / upload) after a 5xx that
// landed post-write stores the memory twice, which is exactly what the API's
// `write_status_unknown` 503 warns against.
const RETRYABLE = (status: number, idempotent: boolean): boolean =>
  status === 429 || (status >= 500 && idempotent);

// A server can name any `Retry-After` it likes, including a value that would
// park the client for an hour. Honour the hint, but never sleep longer than
// this — an absurd value is far more likely a misconfiguration than an
// instruction we should obey to the letter.
const MAX_RETRY_AFTER_MS = 60_000;

/**
 * Seconds the server asked us to wait, or undefined if it did not say.
 *
 * `Retry-After` first. Then the error body, because the rate-limit payload
 * carries `retry_after` / `window_seconds` and predates the header — a client
 * that only reads the header falls back to its own backoff, which tops out
 * around a second and a half against a window of a whole minute. It then spends
 * every retry landing on the same full bucket, each one counted against it, and
 * fails anyway. Reading the body is what makes the retry able to succeed.
 */
export function serverRetryHint(
  header: string | null,
  detail: unknown,
): number | undefined {
  if (header) {
    const seconds = Number(header);
    // An HTTP-date is legal here but the API never emits one; treating it as
    // "no hint" beats parsing dates on the error path.
    if (Number.isFinite(seconds) && seconds >= 0) return seconds;
  }
  const error = (detail as { error?: Record<string, unknown> } | null)?.error;
  if (!error || typeof error !== "object") return undefined;
  for (const key of ["retry_after", "window_seconds"]) {
    const value = error[key];
    if (typeof value === "number" && Number.isFinite(value) && value >= 0) return value;
  }
  return undefined;
}

export function backoffMs(attempt: number, retryAfterSeconds?: number): number {
  if (retryAfterSeconds !== undefined) {
    return Math.min(retryAfterSeconds * 1000, MAX_RETRY_AFTER_MS);
  }
  const base = 250 * 2 ** attempt;
  return base + Math.random() * base; // full jitter, so retries don't sync up
}

/**
 * The budget the API reported on the most recent response.
 *
 * Every metered response carries `X-RateLimit-*` and `X-Credits-Remaining`.
 * They were read by nobody, so a caller had no way to pace itself and could
 * only discover the ceiling by hitting it. Fields stay `undefined` until a
 * metered response has been seen — the unmetered routes (spaces, settings,
 * webhooks) report no budget, and a call to one must not blank out what the
 * last metered call said.
 */
export interface RateLimitSnapshot {
  limit?: number;
  remaining?: number;
  windowSeconds?: number;
  creditsRemaining?: number;
  retryAfter?: number;
}

/**
 * Sleep that a caller-supplied abort ends early.
 *
 * A backoff sleep sits between two network attempts, so a signal that fires
 * during it must be observed here — otherwise the client wakes and fires one
 * more full request after the caller has already cancelled. Resolves on the
 * timer or the abort, whichever comes first; the caller checks `signal.aborted`
 * afterwards to decide whether to retry or stop.
 */
const sleep = (ms: number, signal?: AbortSignal): Promise<void> =>
  new Promise((resolve) => {
    if (signal?.aborted) return resolve();
    let timer: ReturnType<typeof setTimeout>;
    const onAbort = () => {
      clearTimeout(timer);
      resolve();
    };
    timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });

export class HttpClient {
  /** Budget from the most recent response. See {@link RateLimitSnapshot}. */
  readonly rateLimit: RateLimitSnapshot = {};

  constructor(private readonly opts: ResolvedOptions) {}

  private observeRateLimit(response: Response, retryAfter?: number): void {
    const num = (name: string): number | undefined => {
      const raw = response.headers.get(name);
      if (raw === null) return undefined;
      const parsed = Number(raw);
      return Number.isFinite(parsed) ? parsed : undefined;
    };
    const limit = num("x-ratelimit-limit");
    if (limit !== undefined) {
      this.rateLimit.limit = limit;
      this.rateLimit.remaining = num("x-ratelimit-remaining");
      this.rateLimit.windowSeconds = num("x-ratelimit-window");
    }
    const credits = num("x-credits-remaining");
    if (credits !== undefined) this.rateLimit.creditsRemaining = credits;
    this.rateLimit.retryAfter = response.status === 429 ? retryAfter : undefined;
  }

  private url(path: string, query?: Record<string, unknown>): string {
    const url = new URL(path, this.opts.baseUrl);
    for (const [key, value] of Object.entries(query ?? {})) {
      if (value !== undefined && value !== null) url.searchParams.set(key, String(value));
    }
    return url.toString();
  }

  private async parseError(response: Response): Promise<AnonaError> {
    const requestId = response.headers.get("x-request-id") ?? undefined;
    const text = await response.text();

    let detail: unknown = text;
    let code: string | undefined;
    let message = text || response.statusText;

    try {
      const parsed = JSON.parse(text) as { error?: { code?: string; message?: string } };
      detail = parsed;
      if (parsed?.error) {
        code = parsed.error.code;
        message = parsed.error.message ?? message;
      }
    } catch {
      // Not JSON. Keep the raw text as the detail — this is what a stripped
      // edge response looks like.
    }

    const retryAfter = serverRetryHint(response.headers.get("retry-after"), detail);
    return new AnonaError({
      statusCode: response.status,
      message,
      code,
      requestId,
      detail,
      retryAfter,
    });
  }

  async request<T = unknown>(options: RequestOptions): Promise<T> {
    const url = this.url(options.path, options.query);
    const headers: Record<string, string> = {
      authorization: `Bearer ${this.opts.apiKey}`,
    };

    let body: BodyInit | undefined;
    if (options.form) {
      body = options.form; // fetch sets the multipart boundary itself
    } else if (options.body !== undefined) {
      headers["content-type"] = "application/json";
      body = JSON.stringify(options.body);
    }

    // Absent means safe: only the create endpoints opt out (see RequestOptions).
    const idempotent = options.idempotent !== false;

    let lastError: AnonaError | undefined;

    const timeoutMs = options.timeoutMs ?? this.opts.timeoutMs;

    for (let attempt = 0; attempt <= this.opts.maxRetries; attempt++) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      const onExternalAbort = () => controller.abort();
      options.signal?.addEventListener("abort", onExternalAbort);

      let response: Response;
      try {
        response = await this.opts.fetchImpl(url, {
          method: options.method,
          headers,
          body,
          signal: controller.signal,
        });
      } catch (cause) {
        // A caller-supplied abort is intentional; surface it unchanged rather
        // than retrying work the caller just cancelled.
        if (options.signal?.aborted) throw cause;
        lastError = new AnonaError({
          statusCode: 408,
          message: cause instanceof Error ? cause.message : "request failed",
          detail: cause,
        });
        // A network failure or timeout is retried only when the call is
        // idempotent. On a create the request may have reached the server and
        // been applied before the connection dropped, so replaying it risks a
        // duplicate — surface the error and let the caller decide.
        if (idempotent && attempt < this.opts.maxRetries) {
          await sleep(backoffMs(attempt), options.signal);
          // The caller cancelled while we were backing off — surface the last
          // failure rather than starting another attempt they don't want.
          if (options.signal?.aborted) throw lastError;
          continue;
        }
        throw lastError;
      } finally {
        clearTimeout(timer);
        options.signal?.removeEventListener("abort", onExternalAbort);
      }

      if (response.ok) {
        this.observeRateLimit(response);
        if (options.expectNoContent || response.status === 204) return undefined as T;
        return (await response.json()) as T;
      }

      lastError = await this.parseError(response);
      this.observeRateLimit(response, lastError.retryAfter);
      if (
        !RETRYABLE(response.status, idempotent) ||
        attempt === this.opts.maxRetries
      )
        throw lastError;
      await sleep(backoffMs(attempt, lastError.retryAfter), options.signal);
      // Abort observed mid-backoff: stop here instead of firing one more attempt.
      if (options.signal?.aborted) throw lastError;
    }

    throw lastError ?? new AnonaError({ statusCode: 500, message: "request failed" });
  }
}
