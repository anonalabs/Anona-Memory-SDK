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

function bodyOf(fetchImpl: ReturnType<typeof stub>): any {
  const calls = (fetchImpl as any).mock.calls as Array<unknown[]>;
  return JSON.parse((calls[0]![1] as RequestInit).body as string);
}

function urlOf(fetchImpl: ReturnType<typeof stub>): string {
  return (fetchImpl as any).mock.calls[0]![0] as string;
}

describe("retrieveReceipt", () => {
  it("asks for a receipt and returns the id alongside the results", async () => {
    const fetchImpl = stub({
      results: [{ memory_id: "m1", content: "hi", relevance_score: 0.9, entities: [] }],
      receipt_id: "req-123",
    });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const res = await anona.retrieveReceipt({ spaceId: "s", query: "q" });

    expect(bodyOf(fetchImpl).receipt).toBe(true);
    expect(res.receipt_id).toBe("req-123");
    // The memories are the same shape `retrieve` hands back, which is the
    // whole point of the variant: swapping one for the other should not mean
    // rewriting what consumes it.
    expect(res.memories[0]!.memory_id).toBe("m1");
  });

  it("omits receipt_detail unless full is asked for", async () => {
    const fetchImpl = stub({ results: [], receipt_id: "r" });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.retrieveReceipt({ spaceId: "s", query: "q" });

    // Positive control: the body was really built and sent.
    expect(bodyOf(fetchImpl).space_id).toBe("s");
    expect("receipt_detail" in bodyOf(fetchImpl)).toBe(false);
  });

  it("sends receipt_detail when full is asked for", async () => {
    const fetchImpl = stub({ results: [], receipt_id: "r" });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.retrieveReceipt({ spaceId: "s", query: "q", receiptDetail: "full" });

    expect(bodyOf(fetchImpl).receipt_detail).toBe("full");
  });

  it("reports a null id rather than throwing when no receipt was stored", async () => {
    // A receipt is a debugging aid, never load-bearing: if it could not be
    // built the search itself still succeeded and the results are complete.
    const fetchImpl = stub({ results: [{ memory_id: "m1" }] });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const res = await anona.retrieveReceipt({ spaceId: "s", query: "q" });

    expect(res.receipt_id).toBeNull();
    expect(res.memories).toHaveLength(1);
  });
});

describe("getReceipt", () => {
  it("fetches by request id and percent-encodes it", async () => {
    const fetchImpl = stub({
      request_id: "a/b",
      space_id: "s",
      included: [],
      excluded: [],
      excluded_truncated: {},
      token_accounting: {},
      engine_stages: true,
    });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const receipt = await anona.getReceipt("a/b");

    // A slash left raw would address a different route entirely.
    expect(urlOf(fetchImpl)).toBe("https://api.anonalabs.com/v1/receipts/a%2Fb");
    expect(receipt.engine_stages).toBe(true);
  });
});

describe("explain", () => {
  it("sends the memory id as a query parameter", async () => {
    const fetchImpl = stub({
      memory_id: "mem_abc",
      outcome: "excluded",
      stage: "fusion",
      detail: "fusion rank 812, cap 300",
      arms: { semantic: 812, keyword: null },
      scores: { fusion: 0.031 },
    });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const out = await anona.explain("req-1", "mem_abc");

    expect(urlOf(fetchImpl)).toBe(
      "https://api.anonalabs.com/v1/receipts/req-1/explain?memory_id=mem_abc",
    );
    expect(out.outcome).toBe("excluded");
    expect(out.arms.semantic).toBe(812);
    // Public arm names only. The keyword arm is not BM25 in production and the
    // backend is swappable, so the engine's own names must never surface.
    expect("bm25" in out.arms).toBe(false);
  });

  it("passes not_retrieved straight through", async () => {
    // The load-bearing outcome: nothing matched the memory, so a bigger limit
    // will not bring it back. Collapsing it into `excluded` would send people
    // to tune the wrong knob.
    const fetchImpl = stub({
      memory_id: "mem_x",
      outcome: "not_retrieved",
      stage: null,
      detail: null,
      arms: {},
      scores: {},
    });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const out = await anona.explain("req-1", "mem_x");

    expect(out.outcome).toBe("not_retrieved");
    expect(out.stage).toBeNull();
  });
});
