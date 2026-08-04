import { describe, expect, it, vi } from "vitest";
import { Anona } from "../src/client.js";

function stub(body: unknown, status = 200) {
  return vi.fn(
    async () =>
      new Response(status === 204 ? null : JSON.stringify(body), {
        status,
        headers: status === 204 ? {} : { "content-type": "application/json" },
      }),
  );
}

describe("createSpace", () => {
  it("posts name and description", async () => {
    const fetchImpl = stub({ space_id: "docs", name: "docs" }, 201);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const space = await anona.createSpace({ name: "docs", description: "product docs" });

    expect((fetchImpl as any).mock.calls[0]![0]).toBe("https://api.anonalabs.com/v1/spaces/");
    expect(JSON.parse(((fetchImpl as any).mock.calls[0]![1] as RequestInit).body as string)).toEqual({
      name: "docs",
      description: "product docs",
    });
    expect(space.space_id).toBe("docs");
  });
});

describe("deleteSpace", () => {
  it("resolves on 204 without parsing a body", async () => {
    const fetchImpl = stub(null, 204);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await expect(anona.deleteSpace("a b")).resolves.toBeUndefined();
    expect((fetchImpl as any).mock.calls[0]![0]).toBe("https://api.anonalabs.com/v1/spaces/a%20b");
    expect(((fetchImpl as any).mock.calls[0]![1] as RequestInit).method).toBe("DELETE");
  });
});

describe("listMemories", () => {
  it("passes pagination as query parameters", async () => {
    const fetchImpl = stub({ items: [], total: 0, limit: 25, offset: 50 });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const page = await anona.listMemories({ spaceId: "s", limit: 25, offset: 50 });

    expect((fetchImpl as any).mock.calls[0]![0]).toBe(
      "https://api.anonalabs.com/v1/spaces/s/memories?limit=25&offset=50",
    );
    expect(page.offset).toBe(50);
  });
});

describe("getUsage", () => {
  it("reads the key-scoped quota snapshot", async () => {
    const fetchImpl = stub({
      credits_remaining: 400,
      credits_limit: 500,
      credits_used: 100,
      rate_limit_per_min: 60,
    });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const usage = await anona.getUsage();

    expect((fetchImpl as any).mock.calls[0]![0]).toBe("https://api.anonalabs.com/v1/usage/me");
    expect(usage.credits_remaining).toBe(400);
  });
});
