import { describe, expect, it, vi } from "vitest";
import { Anona } from "../src/index.js";

function stub(body: unknown) {
  return vi.fn(
    async () =>
      new Response(JSON.stringify(body), {
        headers: { "content-type": "application/json" },
      }),
  );
}

describe("getGraph", () => {
  it("maps minCount onto the API's min_count parameter", async () => {
    const fetchImpl = stub({ nodes: [], edges: [], total_entities: 0, total_edges: 0 });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.getGraph({ spaceId: "s", limit: 100, minCount: 2 });

    expect((fetchImpl as any).mock.calls[0]![0]).toBe(
      "https://api.anonalabs.com/v1/spaces/s/graph?limit=100&min_count=2",
    );
  });
});

describe("listEntities", () => {
  it("returns the page envelope", async () => {
    const fetchImpl = stub({ items: [{ id: "e1", name: "Alice", mention_count: 3 }], total: 1, limit: 100, offset: 0 });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const page = await anona.listEntities({ spaceId: "s" });

    expect(page.items[0]!.name).toBe("Alice");
    expect(page.total).toBe(1);
  });
});

describe("getEntity", () => {
  it("returns the entity with its observations", async () => {
    const fetchImpl = stub({
      id: "e1",
      name: "Alice",
      mention_count: 3,
      observations: [{ text: "prefers email", mentioned_at: null }],
    });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const entity = await anona.getEntity({ spaceId: "s", entityId: "e1" });

    expect((fetchImpl as any).mock.calls[0]![0]).toBe("https://api.anonalabs.com/v1/spaces/s/entities/e1");
    expect(entity.observations[0]!.text).toBe("prefers email");
  });
});
