/**
 * Per-user profiles: `getUserProfile` and `askAboutUser`.
 *
 * Both endpoints shipped on the API, MCP and the dashboard and were reachable
 * from neither SDK — the same drift the per-space configuration methods had.
 * These pin the request each method makes so it cannot recur silently.
 */
import { describe, expect, it, vi } from "vitest";
import { Anona } from "../src/client.js";

const PROFILE = {
  space_id: "s1",
  user_id: "alice",
  memory_count: 2,
  first_seen: "2026-05-02T09:12:00Z",
  last_active: "2026-08-15T18:40:11Z",
  memories: [{ id: "m1", text: "prefers email", user_id: "alice" }],
};

const ANSWER = {
  space_id: "s1",
  user_id: "alice",
  insights: "Alice prefers email.",
  usage: { input_tokens: 210, output_tokens: 42 },
  model: "us.amazon.nova-pro-v1:0",
};

function stub(body: unknown, status = 200) {
  return vi.fn(
    async () =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      }),
  );
}

function call(fetchImpl: unknown, index = 0) {
  const [url, init] = (fetchImpl as any).mock.calls[index]!;
  return {
    url: url as string,
    method: (init as RequestInit).method,
    body: (init as RequestInit).body
      ? JSON.parse((init as RequestInit).body as string)
      : undefined,
  };
}

describe("getUserProfile", () => {
  it("reads one user's profile", async () => {
    const fetchImpl = stub(PROFILE);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const profile = await anona.getUserProfile({ spaceId: "s1", userId: "alice" });

    expect(call(fetchImpl).method).toBe("GET");
    expect(call(fetchImpl).url).toBe(
      "https://api.anonalabs.com/v1/spaces/s1/users/alice/profile",
    );
    expect(profile.memory_count).toBe(2);
    expect(profile.last_active).toBe("2026-08-15T18:40:11Z");
    expect(profile.memories[0]?.user_id).toBe("alice");
  });

  it("sends no query string when the caller asked for no options", async () => {
    // Every parameter has a server-side default, so sending an unasked-for one
    // pins the caller to today's value of a default that may move.
    const fetchImpl = stub(PROFILE);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.getUserProfile({ spaceId: "s1", userId: "alice" });

    expect(call(fetchImpl).url).not.toContain("?");
  });

  it("forwards every documented parameter", async () => {
    const fetchImpl = stub(PROFILE);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.getUserProfile({
      spaceId: "s1",
      userId: "alice",
      limit: 20,
      offset: 40,
      memoryType: "note",
      format: "block",
      contextMaxTokens: 500,
    });

    const query = new URL(call(fetchImpl).url).searchParams;
    expect(Object.fromEntries(query)).toEqual({
      limit: "20",
      offset: "40",
      memory_type: "note",
      format: "block",
      context_max_tokens: "500",
    });
  });

  it("still sends an explicit offset of 0", async () => {
    // 0 is falsy and is also a legal explicit offset.
    const fetchImpl = stub(PROFILE);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.getUserProfile({ spaceId: "s1", userId: "alice", offset: 0 });

    expect(new URL(call(fetchImpl).url).searchParams.get("offset")).toBe("0");
  });

  it("encodes both path segments", async () => {
    // A space's id is its name, and a userId is whatever the caller scopes
    // writes with — either can carry a character that is structural in a URL.
    const fetchImpl = stub(PROFILE);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.getUserProfile({ spaceId: "my space", userId: "user/42" });

    expect(call(fetchImpl).url).toBe(
      "https://api.anonalabs.com/v1/spaces/my%20space/users/user%2F42/profile",
    );
  });

  it("returns memory_count 0 rather than throwing for an unknown user", async () => {
    // A user is a tag created by the first write naming it; there is no
    // registry, so an unknown user is a 200 and must not read as an error.
    const fetchImpl = stub({
      space_id: "s1",
      user_id: "nobody",
      memory_count: 0,
      first_seen: null,
      last_active: null,
      memories: [],
    });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const profile = await anona.getUserProfile({ spaceId: "s1", userId: "nobody" });

    expect(profile.memory_count).toBe(0);
    expect(profile.memories).toEqual([]);
  });
});

describe("askAboutUser", () => {
  it("posts the query", async () => {
    const fetchImpl = stub(ANSWER);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const answer = await anona.askAboutUser({
      spaceId: "s1",
      userId: "alice",
      query: "What does she prefer?",
    });

    expect(call(fetchImpl).method).toBe("POST");
    expect(call(fetchImpl).url).toBe(
      "https://api.anonalabs.com/v1/spaces/s1/users/alice/ask",
    );
    expect(call(fetchImpl).body).toEqual({ query: "What does she prefer?" });
    expect(answer.insights).toBe("Alice prefers email.");
  });

  it("returns the model that answered", async () => {
    // The bill is reconciled against the model that ran, not the one asked for.
    const fetchImpl = stub(ANSWER);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const answer = await anona.askAboutUser({
      spaceId: "s1",
      userId: "alice",
      query: "q",
      model: "balanced",
    });

    expect(call(fetchImpl).body).toEqual({ query: "q", model: "balanced" });
    expect(answer.model).toBe("us.amazon.nova-pro-v1:0");
    expect(answer.usage?.input_tokens).toBe(210);
  });

  it("omits an unset model", async () => {
    // Omitted means "fall through to the space's reason default"; sending null
    // is a different request from sending nothing.
    const fetchImpl = stub(ANSWER);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.askAboutUser({ spaceId: "s1", userId: "alice", query: "q" });

    expect(call(fetchImpl).body).toEqual({ query: "q" });
  });

  it("is never auto-replayed, and waits out the synthesis", async () => {
    // A synthesis pass sits behind this, exactly as it does behind `reason` —
    // so it must not be replayed into overlapping runs, and must not be cut off
    // at the 30s default while the server is still working.
    const fetchImpl = vi.fn(async () => new Response("", { status: 500 }));
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never, maxRetries: 2 });

    await expect(
      anona.askAboutUser({ spaceId: "s1", userId: "alice", query: "q" }),
    ).rejects.toThrow();
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
});
