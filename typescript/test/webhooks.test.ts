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

// A fetch that never resolves until its request signal aborts, then rejects as
// an AbortError — exactly what a hung request past the per-attempt timeout does.
function hangUntilAbort() {
  return vi.fn(
    (_url: string, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () =>
          reject(Object.assign(new Error("aborted"), { name: "AbortError" })),
        );
      }),
  );
}

describe("createWebhook", () => {
  it("posts url, event types and enabled, defaulting the events", async () => {
    const fetchImpl = stub(
      { id: "wh_1", url: "https://x.test/hook", event_types: ["memory.created"], enabled: true, secret: "sh_abc" },
      201,
    );
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const wh = await anona.createWebhook({ spaceId: "s", url: "https://x.test/hook" });

    expect((fetchImpl as any).mock.calls[0]![0]).toBe(
      "https://api.anonalabs.com/v1/spaces/s/webhooks",
    );
    expect(JSON.parse(((fetchImpl as any).mock.calls[0]![1] as RequestInit).body as string)).toEqual({
      url: "https://x.test/hook",
      event_types: ["memory.created"],
      enabled: true,
    });
    expect(wh.secret).toBe("sh_abc");
  });

  it("is not auto-retried on a timeout", async () => {
    const fetchImpl = hangUntilAbort();
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never, timeoutMs: 10 });

    const err = await anona
      .createWebhook({ spaceId: "s", url: "https://x.test/hook" })
      .catch((e: unknown) => e);

    expect(err).toBeDefined();
    // A timeout can arrive after the webhook was registered. Replaying it would
    // register a SECOND webhook with a fresh secret the caller never sees, and
    // every event would then be delivered twice. Sent exactly once.
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("is not auto-retried on a 5xx", async () => {
    const fetchImpl = vi.fn(async () => new Response("boom", { status: 503 }));
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await expect(
      anona.createWebhook({ spaceId: "s", url: "https://x.test/hook" }),
    ).rejects.toBeDefined();
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
});
