import { describe, expect, it, vi } from "vitest";
import { HttpClient } from "../src/http.js";
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

function calledUrl(fetchImpl: ReturnType<typeof vi.fn>): string {
  return (fetchImpl.mock.calls[0] as unknown as [string])[0];
}

describe("base URL with a path prefix", () => {
  it("keeps the prefix instead of resolving the request path over it", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({}));
    await makeClient(fetchImpl as never, {
      baseUrl: "https://proxy.example.com/anona",
    }).request({ method: "GET", path: "/v1/spaces/" });
    expect(calledUrl(fetchImpl)).toBe("https://proxy.example.com/anona/v1/spaces/");
  });

  it("does not double a trailing slash on the base", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({}));
    await makeClient(fetchImpl as never, {
      baseUrl: "https://proxy.example.com/anona/",
    }).request({ method: "GET", path: "/v1/spaces/" });
    expect(calledUrl(fetchImpl)).toBe("https://proxy.example.com/anona/v1/spaces/");
  });

  it("is unchanged for a bare origin", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({}));
    await makeClient(fetchImpl as never).request({ method: "GET", path: "/v1/spaces/" });
    expect(calledUrl(fetchImpl)).toBe("https://api.example.com/v1/spaces/");
  });
});

describe("array query parameters", () => {
  it("sends each element as a repeated key, not one comma-joined value", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({}));
    await makeClient(fetchImpl as never).request({
      method: "GET",
      path: "/v1/spaces/s/models",
      query: { tags: ["a", "b,c"], limit: 5 },
    });
    const url = new URL(calledUrl(fetchImpl));
    expect(url.searchParams.getAll("tags")).toEqual(["a", "b,c"]);
    expect(url.searchParams.get("limit")).toBe("5");
  });

  it("sends nothing for an empty array", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({}));
    await makeClient(fetchImpl as never).request({
      method: "GET",
      path: "/v1/spaces/s/models",
      query: { tags: [] },
    });
    expect(calledUrl(fetchImpl)).toBe("https://api.example.com/v1/spaces/s/models");
  });
});

describe("credits_exhausted", () => {
  it("is not retried, because no wait can make it succeed", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(
        { error: { code: "credits_exhausted", message: "out of credits" } },
        { status: 429 },
      ),
    );
    const err = await makeClient(fetchImpl as never)
      .request({ method: "GET", path: "/v1/spaces/" })
      .catch((e) => e);
    expect(err).toBeInstanceOf(AnonaError);
    expect((err as AnonaError).code).toBe("credits_exhausted");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("leaves an ordinary rate-limit 429 retried (positive control)", async () => {
    let n = 0;
    const fetchImpl = vi.fn(async () =>
      ++n === 1
        ? jsonResponse(
            { error: { code: "rate_limited" } },
            { status: 429, headers: { "retry-after": "0", "content-type": "application/json" } },
          )
        : jsonResponse({ ok: true }),
    );
    await makeClient(fetchImpl as never).request({ method: "GET", path: "/v1/spaces/" });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });
});
