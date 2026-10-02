/**
 * Read-side space routing on `retrieve`, and the write fields on `recordBatch`.
 *
 * `route: "auto"` shipped on `POST /v1/retrieve` and `POST /v1/record/batch`
 * but reached neither client. These tests pin the request shape, the two local
 * pre-flight refusals (both selectors, neither selector — the server's
 * `422 route_conflict`, reproduced client-side so a doomed call costs no round
 * trip), that both are *compile* errors as well, and that an addressed call
 * still serialises exactly as it did before routing existed.
 */
import { describe, expect, it, vi } from "vitest";
import { Anona } from "../src/client.js";
import type { SearchedSpace } from "../src/types.js";

function stub(body: unknown, status = 200) {
  return vi.fn(
    async () =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      }),
  );
}

function bodyOf(fetchImpl: ReturnType<typeof stub>, call = 0): unknown {
  const calls = (fetchImpl as any).mock.calls as Array<unknown[]>;
  return JSON.parse((calls[call]![1] as RequestInit).body as string);
}

describe("retrieve routing", () => {
  it("sends route and omits space_id on a routed read", async () => {
    const fetchImpl = stub({ results: [] });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.retrieve({ route: "auto", query: "acme payment terms" });

    expect(bodyOf(fetchImpl)).toEqual({
      query: "acme payment terms",
      route: "auto",
    });
  });

  it("sends fallbackSpaceId when given", async () => {
    const fetchImpl = stub({ results: [] });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.retrieve({ route: "auto", query: "q", fallbackSpaceId: "inbox" });

    expect(bodyOf(fetchImpl)).toEqual({
      query: "q",
      route: "auto",
      fallback_space_id: "inbox",
    });
  });

  it("leaves an addressed read byte-identical to what it sent before routing existed", async () => {
    const fetchImpl = stub({ results: [] });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.retrieve({
      spaceId: "support",
      query: "q",
      limit: 5,
      mode: "fast",
      userId: "alice",
      tags: ["a"],
      minScore: 0.2,
    });

    // Keys and their order both, which is what "the same bytes" means for a
    // payload JSON.stringify builds for us.
    const expected = {
      space_id: "support",
      query: "q",
      limit: 5,
      mode: "fast",
      user_id: "alice",
      tags: ["a"],
      min_score: 0.2,
    };
    const sent = bodyOf(fetchImpl) as Record<string, unknown>;
    expect(sent).toEqual(expected);
    expect(Object.keys(sent)).toEqual(Object.keys(expected));
  });

  it("rejects both selectors before making a request", async () => {
    const fetchImpl = stub({ results: [] });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await expect(
      // @ts-expect-error — both selectors is a compile error as well as a runtime one.
      anona.retrieve({ spaceId: "s", route: "auto", query: "q" }),
    ).rejects.toMatchObject({ statusCode: 422, code: "route_conflict" });
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("rejects neither selector before making a request", async () => {
    const fetchImpl = stub({ results: [] });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await expect(
      // @ts-expect-error — one of the two is required.
      anona.retrieve({ query: "q" }),
    ).rejects.toMatchObject({ statusCode: 422, code: "route_conflict" });
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("reads `searched` off a routed response", async () => {
    const searched: SearchedSpace[] = [
      { space_id: "billing", confidence: 0.87, stage: "model", reason: "invoice terms" },
    ];
    const fetchImpl = stub({ results: [{ memory_id: "m1" }], searched });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const memories = await anona.retrieve({ route: "auto", query: "q" });

    // Still an ordinary array, which is what keeps every existing call site
    // working.
    expect(Array.isArray(memories)).toBe(true);
    expect(memories).toHaveLength(1);
    expect(memories.searched).toEqual(searched);
  });

  it("parses an abstention: one entry with a null space_id, and no results", async () => {
    const fetchImpl = stub({
      results: [],
      searched: [
        {
          space_id: null,
          confidence: 0.21,
          stage: "model",
          reason: "no_space_fits",
        },
      ],
    });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const memories = await anona.retrieve({ route: "auto", query: "q" });

    expect(memories).toHaveLength(0);
    // One entry, not an empty list: an empty list has nowhere to carry the
    // reason. And null is not the default space — nothing was searched.
    expect(memories.searched).toHaveLength(1);
    expect(memories.searched![0]!.space_id).toBeNull();
    expect(memories.searched![0]!.reason).toBe("no_space_fits");
  });

  it("leaves `searched` undefined on an addressed read, where the key is absent", async () => {
    const fetchImpl = stub({ results: [{ memory_id: "m1" }] });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const memories = await anona.retrieve({ spaceId: "s", query: "q" });

    expect(memories.searched).toBeUndefined();
    expect("searched" in memories).toBe(false);
  });
});

describe("recordBatch routing", () => {
  it("sends route, fallback and maxTargets, and omits space_id", async () => {
    const fetchImpl = stub({ job_id: "j1", job_ids: null, status: "queued", accepted: 1 });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.recordBatch({
      route: "auto",
      items: [{ content: "c" }],
      fallbackSpaceId: "inbox",
      maxTargets: 1,
    });

    expect(bodyOf(fetchImpl)).toEqual({
      items: [{ content: "c" }],
      route: "auto",
      fallback_space_id: "inbox",
      max_targets: 1,
    });
  });

  it("does not expect routed_to back — the batch is routed as one unit", async () => {
    // The whole batch is filed together, so the response carries job ids only.
    // Asserted so nobody adds a field the API does not return.
    const fetchImpl = stub({
      job_id: "j1",
      job_ids: ["j1", "j2"],
      status: "queued",
      accepted: 2,
    });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const result = await anona.recordBatch({
      route: "auto",
      items: [{ content: "a" }, { content: "b" }],
    });

    expect(result.job_ids).toEqual(["j1", "j2"]);
    expect("routed_to" in result).toBe(false);
  });

  it("leaves an addressed batch byte-identical", async () => {
    const fetchImpl = stub({ job_id: "j", job_ids: null, status: "queued", accepted: 1 });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.recordBatch({ spaceId: "s", items: [{ content: "c" }] });

    const sent = bodyOf(fetchImpl) as Record<string, unknown>;
    expect(sent).toEqual({ space_id: "s", items: [{ content: "c" }] });
    expect(Object.keys(sent)).toEqual(["space_id", "items"]);
  });

  it("rejects both selectors and neither, before making a request", async () => {
    const fetchImpl = stub({});
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await expect(
      // @ts-expect-error — both selectors is a compile error as well as a runtime one.
      anona.recordBatch({ spaceId: "s", route: "auto", items: [{ content: "c" }] }),
    ).rejects.toMatchObject({ statusCode: 422, code: "route_conflict" });
    await expect(
      // @ts-expect-error — one of the two is required.
      anona.recordBatch({ items: [{ content: "c" }] }),
    ).rejects.toMatchObject({ statusCode: 422, code: "route_conflict" });
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("still rejects an empty batch before the routing check", async () => {
    const fetchImpl = stub({});
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await expect(anona.recordBatch({ spaceId: "s", items: [] })).rejects.toThrow(
      /at least one item/,
    );
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

/**
 * `maxTargets` — the cap on how many spaces a routed read may search.
 *
 * A ceiling, not a count: the API searches only the spaces that look likely, so
 * these pin the wire shape and the two ways the field can be wrong, not a
 * promise about how many spaces get searched.
 */
describe("retrieve maxTargets", () => {
  it("sends max_targets on a routed read", async () => {
    const fetchImpl = stub({ results: [] });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.retrieve({ route: "auto", query: "was acme charged?", maxTargets: 3 });

    expect(bodyOf(fetchImpl)).toEqual({
      query: "was acme charged?",
      route: "auto",
      max_targets: 3,
    });
  });

  it("omits it entirely when unset, so the organization's setting applies", async () => {
    const fetchImpl = stub({ results: [] });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.retrieve({ route: "auto", query: "was acme charged?" });

    expect(bodyOf(fetchImpl)).not.toHaveProperty("max_targets");
  });

  it("refuses it on an addressed read before the request goes out", async () => {
    // The types already forbid this; the runtime check is for a JavaScript
    // caller, who would otherwise pay a round trip to be told the same thing.
    const fetchImpl = stub({ results: [] });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await expect(
      anona.retrieve({
        spaceId: "billing",
        query: "invoices?",
        maxTargets: 2,
      } as never),
    ).rejects.toMatchObject({ code: "route_conflict" });
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("reports every space searched, best first", async () => {
    const searched: SearchedSpace[] = [
      { space_id: "billing", confidence: 0.78, stage: "model", reason: null },
      { space_id: "acme", confidence: 0.73, stage: "model", reason: null },
    ];
    const fetchImpl = stub({ results: [{ memory_id: "m1" }], searched });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const results = await anona.retrieve({
      route: "auto",
      query: "was acme charged?",
      maxTargets: 2,
    });

    expect(results.searched).toHaveLength(2);
    expect(results.searched?.[0]?.space_id).toBe("billing");
  });
});
