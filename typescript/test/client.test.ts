import { describe, expect, it, vi } from "vitest";
import { Anona } from "../src/client.js";

describe("Anona constructor", () => {
  it("requires an API key", () => {
    expect(() => new Anona({ apiKey: "" })).toThrow(/apiKey/i);
  });

  it("defaults to the data-plane host and strips a trailing slash", async () => {
    const fetchImpl = vi.fn(
      async () =>
        new Response(JSON.stringify({ spaces: [], total: 0 }), {
          headers: { "content-type": "application/json" },
        }),
    );

    const withDefault = new Anona({ apiKey: "k", fetch: fetchImpl as never });
    await withDefault.listSpaces();
    const call0 = fetchImpl.mock.calls[0] as unknown as [string];
    expect(call0[0]).toBe("https://api.anonalabs.com/v1/spaces/");

    const withOverride = new Anona({
      apiKey: "k",
      baseUrl: "http://localhost:3001/",
      fetch: fetchImpl as never,
    });
    await withOverride.listSpaces();
    const call1 = fetchImpl.mock.calls[1] as unknown as [string];
    expect(call1[0]).toBe("http://localhost:3001/v1/spaces/");
  });
});
