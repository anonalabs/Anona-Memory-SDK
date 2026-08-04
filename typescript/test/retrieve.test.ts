import { describe, expect, it, vi } from "vitest";
import { Anona } from "../src/client.js";

function stub(body: unknown) {
  return vi.fn(
    async () =>
      new Response(JSON.stringify(body), {
        headers: { "content-type": "application/json" },
      }),
  );
}

function bodyOf(fetchImpl: ReturnType<typeof stub>): unknown {
  const calls = (fetchImpl as any).mock.calls as Array<unknown[]>;
  return JSON.parse((calls[0]![1] as RequestInit).body as string);
}

describe("retrieve", () => {
  it("sends only space_id, query and the defaults the API needs", async () => {
    const fetchImpl = stub({ results: [] });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.retrieve({ spaceId: "support", query: "contact Alice" });

    expect((fetchImpl as any).mock.calls[0]![0]).toBe("https://api.anonalabs.com/v1/retrieve");
    expect(bodyOf(fetchImpl)).toEqual({ space_id: "support", query: "contact Alice" });
  });

  it("returns the results array, not the envelope", async () => {
    const fetchImpl = stub({
      results: [{ memory_id: "m1", content: "hi", relevance_score: 1.4, entities: [] }],
      usage: { input_tokens: 10, output_tokens: 0 },
    });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const results = await anona.retrieve({ spaceId: "s", query: "q" });

    expect(Array.isArray(results)).toBe(true);
    expect(results[0]!.relevance_score).toBe(1.4);
  });

  it("passes every filter through with its API field name", async () => {
    const fetchImpl = stub({ results: [] });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.retrieve({
      spaceId: "s",
      query: "q",
      limit: 5,
      topK: 7,
      mode: "fast",
      memoryType: ["fact"],
      tags: ["agent:triage"],
      tagsMatch: "all",
      preferObservations: false,
      minScore: 0.4,
      queryTimestamp: "2026-01-01T00:00:00Z",
    });

    expect(bodyOf(fetchImpl)).toEqual({
      space_id: "s",
      query: "q",
      limit: 5,
      top_k: 7,
      mode: "fast",
      memory_type: ["fact"],
      tags: ["agent:triage"],
      tags_match: "all",
      prefer_observations: false,
      min_score: 0.4,
      query_timestamp: "2026-01-01T00:00:00Z",
    });
  });

  it("tolerates a response with no results key", async () => {
    const fetchImpl = stub({});
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await expect(anona.retrieve({ spaceId: "s", query: "q" })).resolves.toEqual([]);
  });
});

describe("reason", () => {
  it("returns the whole envelope, including usage", async () => {
    const fetchImpl = stub({ insights: "Alice prefers email.", status: "completed" });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const result = await anona.reason({ spaceId: "s", query: "how to contact Alice" });

    expect((fetchImpl as any).mock.calls[0]![0]).toBe("https://api.anonalabs.com/v1/reason");
    expect(result.insights).toBe("Alice prefers email.");
  });
});
