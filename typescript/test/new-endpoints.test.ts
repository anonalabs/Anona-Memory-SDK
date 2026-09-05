import { describe, expect, it, vi } from "vitest";
import { Anona } from "../src/client.js";
import { AnonaError } from "../src/errors.js";

function jsonResponse(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
    ...init,
  });
}

/** A client whose fetch records every call and answers 200 with `body`. */
function spyClient(body: unknown = { ok: true }, init: ResponseInit = {}) {
  const fetchImpl = vi.fn(async () => jsonResponse(body, init));
  const anona = new Anona({
    apiKey: "anona_test_key",
    baseUrl: "https://api.example.com",
    fetch: fetchImpl as never,
  });
  const last = () => {
    const calls = fetchImpl.mock.calls as unknown as [string, RequestInit][];
    const [url, init] = calls[calls.length - 1] as [string, RequestInit];
    return {
      url,
      method: init.method,
      body: init.body ? JSON.parse(init.body as string) : undefined,
    };
  };
  return { anona, fetchImpl, last };
}

describe("reason settings", () => {
  it("gets, sets and resets", async () => {
    const { anona, last } = spyClient({ space_id: "s1", model: null });

    await anona.getReasonSettings("s1");
    expect(last().method).toBe("GET");
    expect(last().url).toBe("https://api.example.com/v1/spaces/s1/reason-settings");

    await anona.setReasonSettings({ spaceId: "s1", model: "balanced" });
    expect(last().method).toBe("PUT");
    expect(last().body).toEqual({ model: "balanced" });

    // Clearing is an explicit null, not an omission: PUT is a full replace.
    await anona.setReasonSettings({ spaceId: "s1", model: null });
    expect(last().body).toEqual({ model: null });

    await anona.resetReasonSettings("s1");
    expect(last().method).toBe("DELETE");
  });
});

describe("memory models", () => {
  it("covers the whole lifecycle on the right paths", async () => {
    const { anona, last } = spyClient({ id: "m1" });
    const spaceId = "s1";

    await anona.listMemoryModels({ spaceId, limit: 5, offset: 10 });
    expect(last().url).toBe(
      "https://api.example.com/v1/spaces/s1/models?limit=5&offset=10",
    );

    await anona.createMemoryModel({ spaceId, name: "n", query: "q", maxTokens: 512 });
    expect(last().method).toBe("POST");
    expect(last().body).toEqual({ name: "n", query: "q", max_tokens: 512 });

    await anona.getMemoryModel({ spaceId, modelId: "m1" });
    expect(last().url).toBe("https://api.example.com/v1/spaces/s1/models/m1");

    await anona.updateMemoryModel({ spaceId, modelId: "m1", name: "n2" });
    expect(last().method).toBe("PATCH");
    expect(last().body).toEqual({ name: "n2" });

    await anona.refreshMemoryModel({ spaceId, modelId: "m1" });
    expect(last().url).toBe("https://api.example.com/v1/spaces/s1/models/m1/refresh");

    await anona.clearMemoryModel({ spaceId, modelId: "m1" });
    expect(last().url).toBe("https://api.example.com/v1/spaces/s1/models/m1/clear");

    await anona.getMemoryModelHistory({ spaceId, modelId: "m1" });
    expect(last().url).toBe("https://api.example.com/v1/spaces/s1/models/m1/history");
  });

  it("does not auto-retry a create or a refresh, which are billed at submit", async () => {
    const fetchImpl = vi.fn(async () => new Response("boom", { status: 503 }));
    const anona = new Anona({
      apiKey: "k",
      baseUrl: "https://api.example.com",
      fetch: fetchImpl as never,
    });

    await expect(
      anona.createMemoryModel({ spaceId: "s1", name: "n", query: "q" }),
    ).rejects.toBeInstanceOf(AnonaError);
    expect(fetchImpl).toHaveBeenCalledTimes(1);

    fetchImpl.mockClear();
    await expect(
      anona.refreshMemoryModel({ spaceId: "s1", modelId: "m1" }),
    ).rejects.toBeInstanceOf(AnonaError);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
});

describe("space profile and the LLM catalog", () => {
  it("hits the right paths", async () => {
    const { anona, last } = spyClient({ object: "list", data: [] });

    await anona.getSpaceProfile("s1");
    expect(last().url).toBe("https://api.example.com/v1/spaces/s1/profile");

    await anona.listCatalogModels();
    // /v1/models is the LLM catalog; /v1/spaces/{id}/models is memory models.
    expect(last().url).toBe("https://api.example.com/v1/models");
  });
});

describe("cancelJob", () => {
  it("DELETEs the job and returns what it actually stopped", async () => {
    const { anona, last } = spyClient({
      job_id: "j1",
      status: "cancelled",
      cancelled: 3,
      running: 1,
    });
    const result = await anona.cancelJob({ spaceId: "s1", jobId: "j1" });
    expect(last().method).toBe("DELETE");
    expect(last().url).toBe("https://api.example.com/v1/spaces/s1/jobs/j1");
    // A part already picked up runs to completion; the cancel is not all-or-nothing.
    expect(result.running).toBe(1);
  });
});

describe("listMemories filters", () => {
  it("sends every filter, with the API's own parameter names", async () => {
    const { anona, last } = spyClient({ items: [], total: 0, limit: 50, offset: 0 });

    await anona.listMemories({
      spaceId: "s1",
      limit: 5,
      query: "kestrel",
      memoryType: "world",
      state: "invalidated",
      userId: "u1",
      memberId: "me",
      includeSources: true,
    });

    const url = new URL(last().url);
    expect(url.pathname).toBe("/v1/spaces/s1/memories");
    expect(url.searchParams.get("q")).toBe("kestrel");
    // The API's query name is `type`, not `memory_type`.
    expect(url.searchParams.get("type")).toBe("world");
    expect(url.searchParams.get("state")).toBe("invalidated");
    expect(url.searchParams.get("user_id")).toBe("u1");
    expect(url.searchParams.get("member_id")).toBe("me");
    expect(url.searchParams.get("prefer_observations")).toBe("false");
  });

  it("omits prefer_observations unless sources were asked for", async () => {
    const { anona, last } = spyClient({ items: [], total: 0, limit: 50, offset: 0 });
    await anona.listMemories({ spaceId: "s1" });
    expect(new URL(last().url).searchParams.has("prefer_observations")).toBe(false);
  });
});

describe("search arguments", () => {
  it("retrieve sends member_id", async () => {
    const { anona, last } = spyClient({ results: [] });
    await anona.retrieve({ spaceId: "s1", query: "q", memberId: "me" });
    expect(last().body).toMatchObject({ member_id: "me" });
  });

  it("getContext sends the block knobs", async () => {
    const { anona, last } = spyClient({ context: "" });
    await anona.getContext({
      spaceId: "s1",
      query: "q",
      maxTokens: 500,
      blockOrder: "stable",
      memberId: "me",
    });
    expect(last().body).toMatchObject({
      format: "block",
      context_max_tokens: 500,
      block_order: "stable",
      member_id: "me",
    });
  });

  it("an unfiltered retrieve still sends only the four base fields", async () => {
    // The no-regression guarantee: adding knobs must not add keys to a plain
    // search, or the engine builds different SQL than it always has.
    const { anona, last } = spyClient({ results: [] });
    await anona.retrieve({ spaceId: "s1", query: "q" });
    expect(last().body).toEqual({ space_id: "s1", query: "q" });
  });
});

describe("rate-limit snapshot", () => {
  it("is populated from the budget headers", async () => {
    const { anona } = spyClient(
      { results: [] },
      {
        headers: {
          "content-type": "application/json",
          "x-ratelimit-limit": "60",
          "x-ratelimit-remaining": "7",
          "x-ratelimit-window": "60",
          "x-credits-remaining": "4321",
        },
      },
    );

    expect(anona.rateLimit.limit).toBeUndefined();
    await anona.retrieve({ spaceId: "s1", query: "q" });
    expect(anona.rateLimit.limit).toBe(60);
    expect(anona.rateLimit.remaining).toBe(7);
    expect(anona.rateLimit.windowSeconds).toBe(60);
    expect(anona.rateLimit.creditsRemaining).toBe(4321);
    expect(anona.rateLimit.retryAfter).toBeUndefined();
  });

  it("an unmetered call leaves the last metered reading in place", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse(
          { results: [] },
          {
            headers: {
              "content-type": "application/json",
              "x-ratelimit-limit": "60",
              "x-ratelimit-remaining": "5",
            },
          },
        ),
      )
      // Config routes report no budget at all.
      .mockResolvedValueOnce(jsonResponse({ space_id: "s1" }));

    const anona = new Anona({
      apiKey: "k",
      baseUrl: "https://api.example.com",
      fetch: fetchImpl as never,
    });
    await anona.retrieve({ spaceId: "s1", query: "q" });
    await anona.getChatSettings("s1");
    expect(anona.rateLimit.limit).toBe(60);
    expect(anona.rateLimit.remaining).toBe(5);
  });
});

describe("429 handling", () => {
  it("waits out the window the rate-limit body names, not its own backoff", async () => {
    // Without the body fallback the client backs off ~250ms against a 60s
    // window, burns both retries on the same full bucket, and fails anyway.
    const sleeps: number[] = [];
    vi.spyOn(globalThis, "setTimeout").mockImplementation(((
      fn: () => void,
      ms?: number,
    ) => {
      sleeps.push(ms ?? 0);
      fn();
      return 0 as unknown as ReturnType<typeof setTimeout>;
    }) as never);

    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse(
          { error: { code: "rate_limited", limit: 60, window_seconds: 60 } },
          { status: 429, headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(jsonResponse({ results: [] }));

    const anona = new Anona({
      apiKey: "k",
      baseUrl: "https://api.example.com",
      fetch: fetchImpl as never,
    });
    await anona.retrieve({ spaceId: "s1", query: "q" });

    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(sleeps).toContain(60_000);
    vi.restoreAllMocks();
  });

  it("puts the wait on the error when it gives up", async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse(
        { error: { code: "rate_limited", window_seconds: 60 } },
        {
          status: 429,
          headers: { "content-type": "application/json", "retry-after": "12" },
        },
      ),
    );
    const anona = new Anona({
      apiKey: "k",
      baseUrl: "https://api.example.com",
      maxRetries: 0,
      fetch: fetchImpl as never,
    });

    const err = (await anona
      .retrieve({ spaceId: "s1", query: "q" })
      .catch((e: unknown) => e)) as AnonaError;

    // The header wins over the body.
    expect(err.retryAfter).toBe(12);
    expect(err.code).toBe("rate_limited");
    expect(anona.rateLimit.retryAfter).toBe(12);
  });
});
