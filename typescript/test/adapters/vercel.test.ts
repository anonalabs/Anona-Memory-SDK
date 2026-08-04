import { describe, expect, it, vi } from "vitest";
import { Anona } from "../../src/index.js";
import { anonaMemory } from "../../src/adapters/vercel.js";

function clientWith(results: unknown[], recordSpy = vi.fn()) {
  const anona = new Anona({ apiKey: "k" });
  vi.spyOn(anona, "retrieve").mockResolvedValue(results as never);
  vi.spyOn(anona, "record").mockImplementation(recordSpy as never);
  return anona;
}

const prompt = [{ role: "user", content: [{ type: "text", text: "who is Alice?" }] }];

describe("anonaMemory.transformParams", () => {
  it("prepends a system message built from retrieved memories", async () => {
    const client = clientWith([{ content: "Alice prefers email", relevance_score: 1.1 }]);
    const middleware = anonaMemory({ client, spaceId: "support" });

    const params = await middleware.transformParams!({
      type: "generate",
      params: { prompt },
    } as never);

    const first = (params as { prompt: Array<{ role: string; content: unknown }> }).prompt[0]!;
    expect(first.role).toBe("system");
    expect(JSON.stringify(first.content)).toContain("Alice prefers email");
    expect(client.retrieve).toHaveBeenCalledWith(
      expect.objectContaining({ spaceId: "support", query: "who is Alice?" }),
    );
  });

  it("leaves the prompt untouched when nothing is recalled", async () => {
    const client = clientWith([]);
    const middleware = anonaMemory({ client, spaceId: "support" });

    const params = await middleware.transformParams!({
      type: "generate",
      params: { prompt },
    } as never);

    expect((params as { prompt: unknown[] }).prompt).toEqual(prompt);
  });

  it("never fails the request when retrieval throws", async () => {
    const client = new Anona({ apiKey: "k" });
    vi.spyOn(client, "retrieve").mockRejectedValue(new Error("memory down"));
    const middleware = anonaMemory({ client, spaceId: "support" });

    const params = await middleware.transformParams!({
      type: "generate",
      params: { prompt },
    } as never);

    expect((params as { prompt: unknown[] }).prompt).toEqual(prompt);
  });
});

describe("anonaMemory.wrapGenerate", () => {
  it("records the turn after the model responds", async () => {
    const recordSpy = vi.fn().mockResolvedValue({});
    const client = clientWith([], recordSpy);
    const middleware = anonaMemory({ client, spaceId: "support", await: true });

    const result = await middleware.wrapGenerate!({
      doGenerate: async () => ({ content: [{ type: "text", text: "Alice prefers email." }] }),
      params: { prompt },
    } as never);

    expect(result).toBeDefined();
    expect(recordSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        spaceId: "support",
        content: expect.stringContaining("Alice prefers email."),
      }),
    );
  });

  it("does not record when record:false", async () => {
    const recordSpy = vi.fn();
    const client = clientWith([], recordSpy);
    const middleware = anonaMemory({ client, spaceId: "s", record: false, await: true });

    await middleware.wrapGenerate!({
      doGenerate: async () => ({ content: [{ type: "text", text: "hi" }] }),
      params: { prompt },
    } as never);

    expect(recordSpy).not.toHaveBeenCalled();
  });
});

describe("anonaMemory.wrapStream", () => {
  it("records the full streamed answer after the stream closes when await:true", async () => {
    const recordSpy = vi.fn().mockResolvedValue({});
    const client = clientWith([], recordSpy);
    const middleware = anonaMemory({ client, spaceId: "support", await: true });

    // Create a ReadableStream that emits multiple text-delta chunks
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue({ type: "text-delta", delta: "Hello" });
        controller.enqueue({ type: "text-delta", delta: " " });
        controller.enqueue({ type: "text-delta", delta: "Alice" });
        controller.enqueue({ type: "text-delta", delta: "!" });
        controller.close();
      },
    });

    const { stream: resultStream } = await middleware.wrapStream!({
      doStream: async () => ({ stream }),
      params: { prompt },
    } as never);

    // Drain the stream to trigger flush and memory recording
    const reader = resultStream.getReader();
    try {
      while (true) {
        const { done } = await reader.read();
        if (done) break;
      }
    } finally {
      reader.releaseLock();
    }

    // Assert that the full concatenated answer was recorded
    expect(recordSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        spaceId: "support",
        content: expect.stringContaining("Hello Alice!"),
      }),
    );
  });
});
