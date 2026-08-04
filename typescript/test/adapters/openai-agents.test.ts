import { describe, expect, it, vi } from "vitest";
import { Anona } from "../../src/index.js";
import { anonaTools } from "../../src/adapters/openai-agents.js";

function client() {
  const anona = new Anona({ apiKey: "k" });
  vi.spyOn(anona, "retrieve").mockResolvedValue([
    { content: "Alice prefers email", relevance_score: 0.9 },
  ] as never);
  vi.spyOn(anona, "record").mockResolvedValue({
    memory_id: "m1",
    job_id: null,
    status: "stored",
  } as never);
  return anona;
}

describe("anonaTools", () => {
  it("exposes exactly the record and retrieve tools", () => {
    const tools = anonaTools({ client: client(), spaceId: "s" });
    expect(tools.map((t) => t.name)).toEqual(["remember", "recall"]);
  });

  it("declares JSON-Schema parameters so no zod dependency is needed", () => {
    const [remember] = anonaTools({ client: client(), spaceId: "s" });
    expect(remember!.parameters).toMatchObject({
      type: "object",
      properties: { content: { type: "string" } },
      required: ["content"],
    });
  });

  it("recall returns formatted memory text", async () => {
    const anona = client();
    const tools = anonaTools({ client: anona, spaceId: "s" });
    const recall = tools.find((t) => t.name === "recall")!;

    const output = await recall.execute({ query: "who is Alice" });

    expect(anona.retrieve).toHaveBeenCalledWith(
      expect.objectContaining({ spaceId: "s", query: "who is Alice" }),
    );
    expect(output).toContain("Alice prefers email");
  });

  it("recall reports an empty result rather than returning nothing", async () => {
    const anona = new Anona({ apiKey: "k" });
    vi.spyOn(anona, "retrieve").mockResolvedValue([]);
    const recall = anonaTools({ client: anona, spaceId: "s" }).find((t) => t.name === "recall")!;

    await expect(recall.execute({ query: "q" })).resolves.toMatch(/no relevant memories/i);
  });

  it("remember stores content and confirms", async () => {
    const anona = client();
    const remember = anonaTools({ client: anona, spaceId: "s" }).find(
      (t) => t.name === "remember",
    )!;

    const output = await remember.execute({ content: "Alice moved to Berlin" });

    expect(anona.record).toHaveBeenCalledWith(
      expect.objectContaining({ spaceId: "s", content: "Alice moved to Berlin" }),
    );
    expect(output).toMatch(/stored/i);
  });

  it("returns the error text instead of throwing into the agent loop", async () => {
    const anona = new Anona({ apiKey: "k" });
    vi.spyOn(anona, "retrieve").mockRejectedValue(new Error("memory down"));
    const recall = anonaTools({ client: anona, spaceId: "s" }).find((t) => t.name === "recall")!;

    await expect(recall.execute({ query: "q" })).resolves.toMatch(/memory down/);
  });
});
