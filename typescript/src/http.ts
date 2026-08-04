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

const RETRYABLE = (status: number): boolean => status === 429 || status >= 500;

function backoffMs(attempt: number, retryAfter: string | null): number {
  if (retryAfter) {
    const seconds = Number(retryAfter);
    if (Number.isFinite(seconds) && seconds >= 0) return seconds * 1000;
  }
  const base = 250 * 2 ** attempt;
  return base + Math.random() * base; // full jitter, so retries don't sync up
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export class HttpClient {
  constructor(private readonly opts: ResolvedOptions) {}

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

    return new AnonaError({ statusCode: response.status, message, code, requestId, detail });
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

    let lastError: AnonaError | undefined;

    for (let attempt = 0; attempt <= this.opts.maxRetries; attempt++) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), this.opts.timeoutMs);
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
        if (attempt < this.opts.maxRetries) {
          await sleep(backoffMs(attempt, null));
          continue;
        }
        throw lastError;
      } finally {
        clearTimeout(timer);
        options.signal?.removeEventListener("abort", onExternalAbort);
      }

      if (response.ok) {
        if (options.expectNoContent || response.status === 204) return undefined as T;
        return (await response.json()) as T;
      }

      lastError = await this.parseError(response);
      if (!RETRYABLE(response.status) || attempt === this.opts.maxRetries) throw lastError;
      await sleep(backoffMs(attempt, response.headers.get("retry-after")));
    }

    throw lastError ?? new AnonaError({ statusCode: 500, message: "request failed" });
  }
}
