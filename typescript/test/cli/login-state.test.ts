import { describe, expect, it, vi } from "vitest";

// Timing cannot be observed from a test, so pin the mechanism instead: the state
// comparison must go through crypto.timingSafeEqual.
const calls = vi.hoisted(() => ({ n: 0 }));
vi.mock("node:crypto", async (orig) => {
  const real = await orig<typeof import("node:crypto")>();
  return {
    ...real,
    timingSafeEqual: (a: NodeJS.ArrayBufferView, b: NodeJS.ArrayBufferView) => (calls.n++, real.timingSafeEqual(a, b)),
  };
});

import { makeServer } from "../../src/cli/login.js";

describe("callback state comparison", () => {
  it("uses timingSafeEqual", async () => {
    const lb = await makeServer();
    lb.setState("S".repeat(43));
    try {
      await fetch(`http://127.0.0.1:${lb.port}/cb?state=${"x".repeat(43)}&code=c`);
      expect(calls.n).toBe(1);
    } finally {
      lb.close();
    }
  });
});
