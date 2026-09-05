import { describe, expect, it, vi } from "vitest";
import { HttpClient, seg, backoffMs, serverRetryHint } from "../src/http.js";
import { AnonaError } from "../src/errors.js";

function jsonResponse(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
    ...init,
  });
}

function makeClient(fetchImpl: typeof fetch, overrides = {}) {
  return new HttpClient({
    apiKey: "anona_test_key",
    baseUrl: "https://api.example.com",
    timeoutMs: 1000,
    maxRetries: 2,
    fetchImpl,
    ...overrides,
  });
}

describe("seg", () => {
  it("encodes characters that are structural in a URL", () => {
    expect(seg("a/b")).toBe("a%2Fb");
    expect(seg("x?y")).toBe("x%3Fy");
    expect(seg("a#b")).toBe("a%23b");
    expect(seg("my space")).toBe("my%20space");
  });
});

describe("HttpClient.request", () => {
  it("sends the bearer token and parses JSON", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ ok: true }));
    const result = await makeClient(fetchImpl as never).request<{ ok: boolean }>({
      method: "GET",
      path: "/v1/spaces/",
    });

    expect(result).toEqual({ ok: true });
    const call = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(call[0]).toBe("https://api.example.com/v1/spaces/");
    expect(call[1].headers).toMatchObject({
      authorization: "Bearer anona_test_key",
    });
  });

  it("appends only defined query parameters", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({}));
    await makeClient(fetchImpl as never).request({
      method: "GET",
      path: "/v1/spaces/x/entities",
      query: { limit: 50, offset: undefined },
    });

    const call = fetchImpl.mock.calls[0] as unknown as [string];
    expect(call[0]).toBe(
      "https://api.example.com/v1/spaces/x/entities?limit=50",
    );
  });

  it("maps the API error envelope onto AnonaError", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(
        { error: { code: "space_not_found", message: "No such space" } },
        { status: 404, headers: { "content-type": "application/json", "x-request-id": "req_123" } },
      ),
    );

    const err = await makeClient(fetchImpl as never)
      .request({ method: "GET", path: "/v1/spaces/nope" })
      .catch((e: unknown) => e);

    expect(err).toBeInstanceOf(AnonaError);
    const anonaErr = err as AnonaError;
    expect(anonaErr.statusCode).toBe(404);
    expect(anonaErr.code).toBe("space_not_found");
    expect(anonaErr.requestId).toBe("req_123");
    expect(anonaErr.message).toContain("No such space");
  });

  it("falls back to raw text when the error body is not JSON", async () => {
    const fetchImpl = vi.fn(
      async () => new Response("error code: 502", { status: 502 }),
    );

    const err = (await makeClient(fetchImpl as never, { maxRetries: 0 })
      .request({ method: "GET", path: "/v1/spaces/" })
      .catch((e: unknown) => e)) as AnonaError;

    expect(err.statusCode).toBe(502);
    expect(err.detail).toBe("error code: 502");
    expect(err.code).toBeUndefined();
  });

  it("retries 5xx and succeeds on a later attempt", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(new Response("boom", { status: 503 }))
      .mockResolvedValueOnce(jsonResponse({ ok: true }));

    const result = await makeClient(fetchImpl as never).request<{ ok: boolean }>({
      method: "GET",
      path: "/v1/spaces/",
    });

    expect(result).toEqual({ ok: true });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });

  it("does not retry 4xx", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ error: { code: "bad", message: "nope" } }, { status: 422 }),
    );

    await expect(
      makeClient(fetchImpl as never).request({ method: "POST", path: "/v1/record" }),
    ).rejects.toBeInstanceOf(AnonaError);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("gives up after maxRetries and throws the last error", async () => {
    const fetchImpl = vi.fn(async () => new Response("down", { status: 500 }));

    const err = (await makeClient(fetchImpl as never, { maxRetries: 1 })
      .request({ method: "GET", path: "/v1/spaces/" })
      .catch((e: unknown) => e)) as AnonaError;

    expect(err.statusCode).toBe(500);
    expect(fetchImpl).toHaveBeenCalledTimes(2); // initial + 1 retry
  });

  it("returns undefined for a 204 response", async () => {
    const fetchImpl = vi.fn(async () => new Response(null, { status: 204 }));
    const result = await makeClient(fetchImpl as never).request({
      method: "DELETE",
      path: "/v1/spaces/x",
      expectNoContent: true,
    });
    expect(result).toBeUndefined();
  });

  it("aborts when the request outlives timeoutMs", async () => {
    const fetchImpl = vi.fn(
      (_url: string, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(Object.assign(new Error("aborted"), { name: "AbortError" })),
          );
        }),
    );

    const err = (await makeClient(fetchImpl as never, { timeoutMs: 10, maxRetries: 0 })
      .request({ method: "GET", path: "/v1/spaces/" })
      .catch((e: unknown) => e)) as AnonaError;

    expect(err).toBeInstanceOf(AnonaError);
    expect(err.statusCode).toBe(408);
  });

  it("does not retry a non-idempotent request on 5xx", async () => {
    const fetchImpl = vi.fn(async () => new Response("boom", { status: 503 }));

    const err = (await makeClient(fetchImpl as never)
      .request({ method: "POST", path: "/v1/record", idempotent: false })
      .catch((e: unknown) => e)) as AnonaError;

    expect(err.statusCode).toBe(503);
    // A 5xx can land after the write was applied, so replaying it would store
    // the memory twice — the request is sent exactly once.
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("does not retry a non-idempotent request on a timeout", async () => {
    const fetchImpl = vi.fn(
      (_url: string, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(Object.assign(new Error("aborted"), { name: "AbortError" })),
          );
        }),
    );

    const err = (await makeClient(fetchImpl as never, { timeoutMs: 10 })
      .request({ method: "POST", path: "/v1/record", idempotent: false })
      .catch((e: unknown) => e)) as AnonaError;

    expect(err.statusCode).toBe(408);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("still retries a non-idempotent request on 429 (nothing was stored)", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(new Response("slow down", { status: 429 }))
      .mockResolvedValueOnce(jsonResponse({ memory_id: "m" }, { status: 201 }));

    const result = await makeClient(fetchImpl as never).request<{ memory_id: string }>({
      method: "POST",
      path: "/v1/record",
      idempotent: false,
    });

    expect(result).toEqual({ memory_id: "m" });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });

  it("stops retrying when the caller aborts during a backoff sleep", async () => {
    const controller = new AbortController();
    const fetchImpl = vi.fn(async () => new Response("boom", { status: 503 }));

    const promise = makeClient(fetchImpl as never, { maxRetries: 3 })
      .request({ method: "GET", path: "/v1/spaces/", signal: controller.signal })
      .catch((e: unknown) => e);

    // Attempt 1 returns 503 immediately; the client is now in its backoff
    // sleep (>=250ms). Cancel well inside that window.
    await new Promise((r) => setTimeout(r, 20));
    controller.abort();

    const err = (await promise) as AnonaError;
    expect(err).toBeInstanceOf(AnonaError);
    // The abort is observed by the sleep, so no second network attempt fires —
    // without that, one more full request goes out after the caller cancelled.
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("honours a per-request timeout override over the client default", async () => {
    const fetchImpl = vi.fn(
      (_url: string, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(Object.assign(new Error("aborted"), { name: "AbortError" })),
          );
        }),
    );

    // Client default is 1000ms, but this request caps itself at 10ms — so it
    // aborts fast rather than waiting out the client budget (reason inverts
    // this: a long override so a slow `reason` is not cut off at the default).
    const start = Date.now();
    const err = (await makeClient(fetchImpl as never, { timeoutMs: 1000, maxRetries: 0 })
      .request({ method: "GET", path: "/v1/reason", timeoutMs: 10 })
      .catch((e: unknown) => e)) as AnonaError;

    expect(err.statusCode).toBe(408);
    expect(Date.now() - start).toBeLessThan(500);
  });
});

describe("backoffMs", () => {
  it("caps an absurd Retry-After at 60s instead of honouring it verbatim", () => {
    // A server naming a one-hour wait must not park the client for an hour.
    expect(backoffMs(0, 3600)).toBe(60_000);
    expect(backoffMs(0, 999999)).toBe(60_000);
  });

  it("honours a sane Retry-After exactly", () => {
    expect(backoffMs(5, 12)).toBe(12_000);
    expect(backoffMs(5, 0)).toBe(0);
  });

  it("falls back to jittered exponential backoff without a Retry-After", () => {
    for (let attempt = 0; attempt < 4; attempt++) {
      const base = 250 * 2 ** attempt;
      const ms = backoffMs(attempt);
      expect(ms).toBeGreaterThanOrEqual(base);
      expect(ms).toBeLessThan(base * 2);
    }
  });
});

describe("serverRetryHint", () => {
  it("reads the Retry-After header", () => {
    expect(serverRetryHint("17", null)).toBe(17);
    expect(serverRetryHint("0", null)).toBe(0);
  });

  it("ignores a non-numeric Retry-After", () => {
    // An HTTP-date is legal but the API never emits one.
    expect(serverRetryHint("Wed, 21 Oct 2026 07:28:00 GMT", null)).toBeUndefined();
  });

  it("falls back to the rate-limit body when there is no header", () => {
    // The deployed API carried window_seconds before it grew the header, so a
    // header-only client backs off ~1.5s against a 60s window and fails.
    expect(serverRetryHint(null, { error: { window_seconds: 60 } })).toBe(60);
    expect(serverRetryHint(null, { error: { retry_after: 12, window_seconds: 60 } })).toBe(12);
  });

  it("prefers the header over the body", () => {
    expect(serverRetryHint("5", { error: { window_seconds: 60 } })).toBe(5);
  });

  it("returns undefined for a body that is not the error envelope", () => {
    expect(serverRetryHint(null, "error code: 502")).toBeUndefined();
    expect(serverRetryHint(null, null)).toBeUndefined();
    expect(serverRetryHint(null, { error: { code: "rate_limited" } })).toBeUndefined();
  });
});
