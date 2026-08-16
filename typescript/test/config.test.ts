/**
 * Per-space configuration: extraction settings, chat defaults and webhooks.
 *
 * All three existed on the API long before the SDK could reach them, which is
 * the drift that keeps recurring — a documented feature the published package
 * cannot call, and no test to notice. These pin the request each method makes.
 */
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

describe("getExtractionSettings", () => {
  it("reads the space's settings", async () => {
    const fetchImpl = stub({ space_id: "s1", mode: "concise", guidance: null, custom_prompt: null });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const settings = await anona.getExtractionSettings("s1");

    expect(call(fetchImpl).url).toBe(
      "https://api.anonalabs.com/v1/spaces/s1/extraction-settings",
    );
    expect(call(fetchImpl).method).toBe("GET");
    expect(settings.mode).toBe("concise");
  });

  it("encodes a space id containing a space", async () => {
    const fetchImpl = stub({ space_id: "my space" });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.getExtractionSettings("my space");

    expect(call(fetchImpl).url).toBe(
      "https://api.anonalabs.com/v1/spaces/my%20space/extraction-settings",
    );
  });
});

describe("setExtractionSettings", () => {
  it("sends every field, because the API replaces the record", async () => {
    const fetchImpl = stub({ space_id: "s1" });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.setExtractionSettings({ spaceId: "s1", guidance: "Capture service names." });

    expect(call(fetchImpl).method).toBe("PUT");
    expect(call(fetchImpl).body).toEqual({
      mode: null,
      guidance: "Capture service names.",
      custom_prompt: null,
    });
  });
});

describe("resetExtractionSettings", () => {
  it("deletes without parsing a body", async () => {
    const fetchImpl = stub(null, 204);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await expect(anona.resetExtractionSettings("s1")).resolves.toBeUndefined();
    expect(call(fetchImpl).method).toBe("DELETE");
  });
});

describe("chat settings", () => {
  it("reads the space's proxy defaults", async () => {
    const fetchImpl = stub({ space_id: "s1", memory_limit: 5 });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.getChatSettings("s1");

    expect(call(fetchImpl).url).toBe(
      "https://api.anonalabs.com/v1/spaces/s1/chat-settings",
    );
  });

  it("sends every field on a replace", async () => {
    const fetchImpl = stub({ space_id: "s1" });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.setChatSettings({ spaceId: "s1", memoryLimit: 3 });

    expect(call(fetchImpl).method).toBe("PUT");
    expect(call(fetchImpl).body).toEqual({
      memory_limit: 3,
      memory_token_budget: null,
      auto_record: null,
      memory: null,
    });
  });

  it("resets back to the platform defaults", async () => {
    const fetchImpl = stub(null, 204);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await expect(anona.resetChatSettings("s1")).resolves.toBeUndefined();
    expect(call(fetchImpl).method).toBe("DELETE");
  });
});

describe("createWebhook", () => {
  it("posts the url and events", async () => {
    const fetchImpl = stub({ id: "wh_1", url: "https://example.com/hook" }, 201);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const hook = await anona.createWebhook({
      spaceId: "s1",
      url: "https://example.com/hook",
      eventTypes: ["memory.created"],
    });

    expect(call(fetchImpl).url).toBe("https://api.anonalabs.com/v1/spaces/s1/webhooks");
    expect(call(fetchImpl).method).toBe("POST");
    expect(call(fetchImpl).body).toEqual({
      url: "https://example.com/hook",
      event_types: ["memory.created"],
      enabled: true,
    });
    expect(hook.id).toBe("wh_1");
  });

  it("defaults to the memory.created event", async () => {
    const fetchImpl = stub({ id: "wh_1" }, 201);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.createWebhook({ spaceId: "s1", url: "https://example.com/hook" });

    expect(call(fetchImpl).body.event_types).toEqual(["memory.created"]);
  });
});

describe("listWebhooks", () => {
  it("returns the items", async () => {
    const fetchImpl = stub({ items: [{ id: "wh_1" }] });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const hooks = await anona.listWebhooks("s1");

    expect(call(fetchImpl).method).toBe("GET");
    expect(hooks).toHaveLength(1);
  });

  it("returns an empty list when the response carries none", async () => {
    const fetchImpl = stub({});
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await expect(anona.listWebhooks("s1")).resolves.toEqual([]);
  });
});

describe("updateWebhook", () => {
  it("patches only what changed", async () => {
    const fetchImpl = stub({ id: "wh_1", enabled: false });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.updateWebhook({ spaceId: "s1", webhookId: "wh_1", enabled: false });

    expect(call(fetchImpl).url).toBe(
      "https://api.anonalabs.com/v1/spaces/s1/webhooks/wh_1",
    );
    expect(call(fetchImpl).method).toBe("PATCH");
    expect(call(fetchImpl).body).toEqual({ enabled: false });
  });
});

describe("deleteWebhook", () => {
  it("encodes both path segments", async () => {
    const fetchImpl = stub(null, 204);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await expect(
      anona.deleteWebhook({ spaceId: "my space", webhookId: "wh 1" }),
    ).resolves.toBeUndefined();
    expect(call(fetchImpl).url).toBe(
      "https://api.anonalabs.com/v1/spaces/my%20space/webhooks/wh%201",
    );
    expect(call(fetchImpl).method).toBe("DELETE");
  });
});

describe("listWebhookDeliveries", () => {
  it("passes paging through", async () => {
    const fetchImpl = stub({ items: [], next_cursor: null });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.listWebhookDeliveries({
      spaceId: "s1",
      webhookId: "wh_1",
      limit: 10,
      cursor: "abc",
    });

    const { url } = call(fetchImpl);
    expect(url).toContain("/v1/spaces/s1/webhooks/wh_1/deliveries");
    expect(url).toContain("limit=10");
    expect(url).toContain("cursor=abc");
  });

  it("omits an absent cursor", async () => {
    const fetchImpl = stub({ items: [] });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.listWebhookDeliveries({ spaceId: "s1", webhookId: "wh_1" });

    expect(call(fetchImpl).url).not.toContain("cursor");
  });
});
