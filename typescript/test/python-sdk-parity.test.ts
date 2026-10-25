/**
 * Where the TypeScript SDK used to differ from the Python SDK on the wire, and
 * now does not: which failures are re-sent, which fields are always sent, and
 * how long `reason` may take.
 */
import { describe, expect, it, vi } from "vitest";
import { Anona } from "../src/client.js";
import { AnonaError } from "../src/errors.js";
import { HttpClient } from "../src/http.js";

function json(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });
}

function http(fetchImpl: unknown) {
  return new HttpClient({ apiKey: "k", baseUrl: "https://api.example.com", timeoutMs: 1000, maxRetries: 2, fetchImpl: fetchImpl as typeof fetch });
}

const down = () => vi.fn(async () => json({ error: { code: "internal_error", message: "x" } }, 503, { "retry-after": "0" }));

function calls(fetchImpl: ReturnType<typeof vi.fn>): Array<{ url: string; body: any }> {
  return (fetchImpl.mock.calls as Array<[string, RequestInit]>).map(([url, init]) => ({
    url,
    body: init.body ? JSON.parse(init.body as string) : null,
  }));
}

describe("retry policy, as the Python SDK's", () => {
  it("a POST is never re-sent after a 5xx, even one that is not a write", async () => {
    for (const path of ["/v1/retrieve", "/v1/reason"]) {
      const f = down();
      await expect(http(f).request({ method: "POST", path, body: {} })).rejects.toBeInstanceOf(AnonaError);
      expect(f, path).toHaveBeenCalledTimes(1);
    }
    const f = down();
    await expect(new Anona({ apiKey: "k", fetch: f as never }).retrieve({ spaceId: "s", query: "q" })).rejects.toBeInstanceOf(AnonaError);
    expect(f).toHaveBeenCalledTimes(1);
  });

  it("GET, PUT and DELETE are re-sent after a 5xx", async () => {
    for (const method of ["GET", "PUT", "DELETE"]) {
      const f = down();
      await expect(http(f).request({ method, path: "/v1/x" })).rejects.toBeInstanceOf(AnonaError);
      expect(f, method).toHaveBeenCalledTimes(3);
    }
  });

  it("a 429 is re-sent on any method", async () => {
    const f = vi
      .fn()
      .mockResolvedValueOnce(json({ error: { code: "rate_limited", message: "x" } }, 429, { "retry-after": "0" }))
      .mockResolvedValueOnce(json({ results: [] }));
    await new Anona({ apiKey: "k", fetch: f as never }).retrieve({ spaceId: "s", query: "q" });
    expect(f).toHaveBeenCalledTimes(2);
  });

  it("a connection failure is never re-sent, on a read either", async () => {
    const f = vi.fn(async () => {
      throw new TypeError("fetch failed");
    });
    const err = (await http(f).request({ method: "GET", path: "/v1/spaces/" }).catch((e: unknown) => e)) as AnonaError;
    expect(err.statusCode).toBe(408);
    expect(f).toHaveBeenCalledTimes(1);
  });
});

describe("timeout, as the Python SDK's", () => {
  it("defaults to 120 s per attempt, and `reason` no longer caps itself at 90 s", async () => {
    const seen: number[] = [];
    const realSetTimeout = globalThis.setTimeout;
    const spy = vi.spyOn(globalThis, "setTimeout").mockImplementation(((fn: () => void, ms?: number) => {
      seen.push(ms ?? 0);
      return realSetTimeout(fn, ms);
    }) as never);
    try {
      const f = vi.fn(async () => json({ insights: "a" }));
      const anona = new Anona({ apiKey: "k", fetch: f as never });
      await anona.reason({ spaceId: "s", query: "q" });
      await anona.askAboutUser({ spaceId: "s", userId: "u", query: "q" }).catch(() => undefined);
    } finally {
      spy.mockRestore();
    }
    expect(seen.slice(0, 2)).toEqual([120_000, 120_000]);
  });

  it("an explicit timeout applies to `reason` too", async () => {
    const f = vi.fn(
      (_url: string, init?: RequestInit) =>
        new Promise<Response>((_r, reject) => init?.signal?.addEventListener("abort", () => reject(Object.assign(new Error("aborted"), { name: "AbortError" })))),
    );
    const err = (await new Anona({ apiKey: "k", fetch: f as never, timeoutMs: 10 })
      .reason({ spaceId: "s", query: "q" })
      .catch((e: unknown) => e)) as AnonaError;
    expect(err.statusCode).toBe(408);
    expect(f).toHaveBeenCalledTimes(1);
  });
});

describe("fields the Python SDK always sends", () => {
  const ok = (body: unknown) => vi.fn(async () => json(body));

  it("record: metadata, `{}` when unset", async () => {
    const f = ok({ memory_id: "m", memory_ids: ["m"], status: "stored" });
    await new Anona({ apiKey: "k", fetch: f as never }).record({ spaceId: "s", content: "c" });
    expect(calls(f)[0]?.body).toEqual({ space_id: "s", content: "c", metadata: {} });
  });

  it("retrieve and retrieveReceipt: limit 10 and mode accurate; getContext: limit 10", async () => {
    const f = ok({ results: [], context: "" });
    const anona = new Anona({ apiKey: "k", fetch: f as never });
    await anona.retrieve({ spaceId: "s", query: "q" });
    await anona.retrieveReceipt({ spaceId: "s", query: "q" });
    await anona.getContext({ spaceId: "s", query: "q" });
    const [a, b, c] = calls(f).map((x) => x.body);
    expect(a).toEqual({ space_id: "s", query: "q", limit: 10, mode: "accurate" });
    expect(b).toMatchObject({ space_id: "s", query: "q", limit: 10, mode: "accurate", receipt: true });
    expect(c).toEqual({ space_id: "s", query: "q", limit: 10, format: "block" });
    await anona.retrieve({ spaceId: "s", query: "q", limit: 3, mode: "fast" });
    expect(calls(f)[3]?.body).toMatchObject({ limit: 3, mode: "fast" });
  });

  it("createSpace: description, null when unset", async () => {
    const f = ok({ space_id: "d", name: "d" });
    await new Anona({ apiKey: "k", fetch: f as never }).createSpace({ name: "d" });
    expect(calls(f)[0]?.body).toEqual({ name: "d", description: null });
  });

  it("listMemories: limit 50 and offset 0, and prefer_observations either way", async () => {
    const f = ok({ items: [], total: 0, limit: 50, offset: 0 });
    const anona = new Anona({ apiKey: "k", fetch: f as never });
    await anona.listMemories({ spaceId: "s" });
    await anona.listMemories({ spaceId: "s", preferObservations: true });
    await anona.listMemories({ spaceId: "s", preferObservations: false });
    await anona.listMemories({ spaceId: "s", includeSources: true });
    const q = calls(f).map((x) => Object.fromEntries(new URL(x.url).searchParams));
    expect(q).toEqual([
      { limit: "50", offset: "0" },
      { limit: "50", offset: "0", prefer_observations: "true" },
      { limit: "50", offset: "0", prefer_observations: "false" },
      { limit: "50", offset: "0", prefer_observations: "false" },
    ]);
  });

  it("listDocuments: limit 100 and offset 0", async () => {
    const f = ok({ documents: [], total: 0, limit: 100, offset: 0 });
    await new Anona({ apiKey: "k", fetch: f as never }).listDocuments({ spaceId: "s" });
    expect(Object.fromEntries(new URL(calls(f)[0]!.url).searchParams)).toEqual({ limit: "100", offset: "0" });
  });
});
