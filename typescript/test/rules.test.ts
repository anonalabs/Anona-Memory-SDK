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

const RULE = {
  id: "rl_1",
  space_id: "s",
  name: "Never call revenue committed",
  content: "Never describe revenue as committed without a signed document.",
  priority: 100,
  is_active: true,
  tags: [],
};

function call(fetchImpl: unknown, index = 0) {
  const [url, init] = (fetchImpl as any).mock.calls[index]!;
  return { url: url as string, init: init as RequestInit };
}

describe("listRules", () => {
  it("reads the space and returns the items", async () => {
    const fetchImpl = stub({ items: [RULE], total: 1 });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const rules = await anona.listRules("s");

    expect(call(fetchImpl).url).toBe("https://api.anonalabs.com/v1/spaces/s/rules");
    expect(rules).toHaveLength(1);
    expect(rules[0]!.name).toBe("Never call revenue committed");
  });

  it("encodes a space id that is not URL-safe", async () => {
    const fetchImpl = stub({ items: [], total: 0 });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.listRules("my space");

    expect(call(fetchImpl).url).toBe(
      "https://api.anonalabs.com/v1/spaces/my%20space/rules",
    );
  });
});

describe("createRule", () => {
  it("posts every field, defaulting priority, active and tags", async () => {
    const fetchImpl = stub(RULE, 201);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.createRule({
      spaceId: "s",
      name: "Never call revenue committed",
      content: "Never describe revenue as committed without a signed document.",
    });

    const { url, init } = call(fetchImpl);
    expect(url).toBe("https://api.anonalabs.com/v1/spaces/s/rules");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      name: "Never call revenue committed",
      content: "Never describe revenue as committed without a signed document.",
      priority: 0,
      is_active: true,
      tags: [],
    });
  });

  it("sends a rule that is stored switched off", async () => {
    const fetchImpl = stub({ ...RULE, is_active: false }, 201);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.createRule({
      spaceId: "s",
      name: "Seasonal",
      content: "Quote Q4 pricing.",
      isActive: false,
      priority: 500,
    });

    expect(JSON.parse(call(fetchImpl).init.body as string)).toMatchObject({
      is_active: false,
      priority: 500,
    });
  });

  it("is not auto-retried on a 5xx", async () => {
    const fetchImpl = vi.fn(async () => new Response("boom", { status: 503 }));
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    // A 5xx can arrive after the rule was stored. A replay would add a second
    // copy of something applied to every answer, and spend one of the 25
    // active slots on the duplicate.
    await expect(
      anona.createRule({ spaceId: "s", name: "n", content: "c" }),
    ).rejects.toBeDefined();
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
});

describe("updateRule", () => {
  it("patches only what was passed", async () => {
    const fetchImpl = stub({ ...RULE, is_active: false });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.updateRule({ spaceId: "s", ruleId: "rl_1", isActive: false });

    const { url, init } = call(fetchImpl);
    expect(url).toBe("https://api.anonalabs.com/v1/spaces/s/rules/rl_1");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual({ is_active: false });
  });

  it("keeps priority 0, which is a real priority and not an absent one", async () => {
    const fetchImpl = stub({ ...RULE, priority: 0 });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.updateRule({ spaceId: "s", ruleId: "rl_1", priority: 0 });

    expect(JSON.parse(call(fetchImpl).init.body as string)).toEqual({ priority: 0 });
  });
});

describe("deleteRule", () => {
  it("deletes by id and expects no content", async () => {
    const fetchImpl = stub(null, 204);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.deleteRule({ spaceId: "my space", ruleId: "rl 1" });

    const { url, init } = call(fetchImpl);
    expect(url).toBe("https://api.anonalabs.com/v1/spaces/my%20space/rules/rl%201");
    expect(init.method).toBe("DELETE");
  });
});

describe("reason", () => {
  it("carries the rules that shaped the answer, named and not quoted", async () => {
    const fetchImpl = stub({
      job_id: null,
      status: "complete",
      insights: "The renewal is not committed.",
      rules_applied: [{ id: "rl_1", name: "Never call revenue committed" }],
    });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const result = await anona.reason({ spaceId: "s", query: "Is it a done deal?" });

    expect(result.rules_applied).toEqual([
      { id: "rl_1", name: "Never call revenue committed" },
    ]);
  });
});
