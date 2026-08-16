import { describe, expect, it, vi } from "vitest";
import { Anona } from "../src/client.js";

// Coverage for four parts of the public surface that had none: hierarchical
// scoping, point-in-time recall, the context-block helper, and `source_ids`.
//
// The absence was the real defect. All four shipped in the API and were
// documented, while this package went without them for several releases, and
// nothing failed - because nothing asserted they were sent. These tests pin
// the wire field names, which is the thing that gets silently missed.

function stub(body: unknown, status = 200) {
  return vi.fn(
    async () =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      }),
  );
}

function bodyOf(fetchImpl: ReturnType<typeof stub>): unknown {
  const calls = (fetchImpl as any).mock.calls as Array<unknown[]>;
  return JSON.parse((calls[0]![1] as RequestInit).body as string);
}

describe("hierarchical scoping", () => {
  it("sends the scope keys on record under their API field names", async () => {
    const fetchImpl = stub({ memory_id: "m", job_id: null, status: "stored" }, 201);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.record({
      spaceId: "s",
      content: "Alice prefers email",
      userId: "alice",
      agentId: "triage",
      sessionId: "sess-1",
    });

    // snake_case on the wire, camelCase in the SDK. A scope that arrives under
    // the wrong name is not an error, it is an unscoped write - the memory
    // lands in the space visible to everyone.
    expect(bodyOf(fetchImpl)).toEqual({
      space_id: "s",
      content: "Alice prefers email",
      user_id: "alice",
      agent_id: "triage",
      session_id: "sess-1",
    });
  });

  it("sends the scope keys on retrieve", async () => {
    const fetchImpl = stub({ results: [] });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.retrieve({
      spaceId: "s",
      query: "q",
      userId: "alice",
      agentId: "triage",
      sessionId: "sess-1",
    });

    expect(bodyOf(fetchImpl)).toEqual({
      space_id: "s",
      query: "q",
      user_id: "alice",
      agent_id: "triage",
      session_id: "sess-1",
    });
  });

  it("omits scope entirely when none is given", async () => {
    const fetchImpl = stub({ results: [] });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.retrieve({ spaceId: "s", query: "q" });

    // An unscoped call must stay byte-identical to what it always was: the API
    // builds different SQL when scope tags are present, so a stray null here
    // would change the query plan for every caller that never opted in.
    expect(bodyOf(fetchImpl)).toEqual({ space_id: "s", query: "q" });
  });
});

describe("point-in-time recall", () => {
  it("sends as_of and query_timestamp as separate fields", async () => {
    const fetchImpl = stub({ results: [] });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.retrieve({
      spaceId: "s",
      query: "q",
      asOf: "2026-06-01T00:00:00Z",
      queryTimestamp: "2026-01-01T00:00:00Z",
    });

    // They are not interchangeable: as_of filters on when a memory was
    // recorded, query_timestamp only re-ranks. Collapsing them would turn a
    // hard cutoff into a scoring hint and quietly return memories the caller
    // asked to exclude.
    expect(bodyOf(fetchImpl)).toEqual({
      space_id: "s",
      query: "q",
      as_of: "2026-06-01T00:00:00Z",
      query_timestamp: "2026-01-01T00:00:00Z",
    });
  });

  it("sends the event-time timestamp on record", async () => {
    const fetchImpl = stub({ memory_id: "m", job_id: null, status: "stored" }, 201);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.record({
      spaceId: "s",
      content: "shipped the beta",
      timestamp: "2025-06-14T10:00:00Z",
    });

    // When the event happened, not when it was recorded - the field a bulk
    // import needs so last June's memory is dated last June.
    expect(bodyOf(fetchImpl)).toEqual({
      space_id: "s",
      content: "shipped the beta",
      timestamp: "2025-06-14T10:00:00Z",
    });
  });
});

describe("getContext", () => {
  it("asks for block format and returns the string", async () => {
    const fetchImpl = stub({ context: "[memories] Alice prefers email" });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const context = await anona.getContext({
      spaceId: "s",
      query: "q",
      maxTokens: 500,
      userId: "alice",
    });

    expect((fetchImpl as any).mock.calls[0]![0]).toBe("https://api.anonalabs.com/v1/retrieve");
    expect(bodyOf(fetchImpl)).toEqual({
      space_id: "s",
      query: "q",
      format: "block",
      context_max_tokens: 500,
      user_id: "alice",
    });
    expect(context).toBe("[memories] Alice prefers email");
  });

  it("returns an empty string when nothing matched", async () => {
    const fetchImpl = stub({});
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    // Empty, not undefined: the return value goes straight into a prompt, and
    // "undefined" rendered into a system message is worse than nothing.
    await expect(anona.getContext({ spaceId: "s", query: "q" })).resolves.toBe("");
  });
});

describe("source_ids", () => {
  it("surfaces the memories a listed note was synthesized from", async () => {
    const fetchImpl = stub({
      items: [{ id: "obs_1", text: "Alice prefers email", source_ids: ["f1", "f2"] }],
      total: 1,
      limit: 20,
      offset: 0,
    });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const page = await anona.listMemories({ spaceId: "s" });

    // On `MemoryItem`, not `SearchResult` - the list is where a synthesized
    // note and its evidence are distinguishable, because the list is what
    // dedups them.
    expect(page.items[0]!.source_ids).toEqual(["f1", "f2"]);
  });

  it("pages the underlying evidence when asked for it", async () => {
    const fetchImpl = stub({ items: [], total: 0, limit: 20, offset: 0 });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.listMemories({ spaceId: "s", includeSources: true });

    // `includeSources` inverts into `prefer_observations=false`, and is absent
    // rather than "true" when not asked for - the default dedups.
    const url = (fetchImpl as any).mock.calls[0]![0] as string;
    expect(url).toContain("prefer_observations=false");
  });
});
