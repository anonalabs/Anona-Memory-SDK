import { describe, expect, it } from "vitest";
import { checkTransport, resolveTarget, TargetError } from "../../src/cli/target.js";
import { DEFAULT_BASE_URL } from "../../src/cli/store.js";

describe("target", () => {
  it("allows https anywhere and plain http only to loopback", () => {
    for (const url of ["https://memory.anonalabs.com", "https://x.example/", "http://127.0.0.1:3001", "http://localhost:3001", "http://[::1]:3001"]) {
      expect(() => checkTransport(url), url).not.toThrow();
    }
    for (const url of ["http://memory.anonalabs.com", "http://10.0.0.5:3001", "http://127.0.0.1.evil.example", "http://localhost@evil.example", "ftp://memory.anonalabs.com", "memory.anonalabs.com", "https://"]) {
      expect(() => checkTransport(url), url).toThrow(TargetError);
    }
  });
  it("pins to the stored deployment and refuses another", () => {
    expect(resolveTarget(undefined, "https://a.example")).toBe("https://a.example");
    expect(resolveTarget("https://a.example/", "https://a.example")).toBe("https://a.example");
    expect(() => resolveTarget("https://b.example", "https://a.example")).toThrow(/stored credential is for https:\/\/a\.example, not https:\/\/b\.example/);
  });
  it("follows the request only when nothing is stored", () => {
    expect(resolveTarget("https://b.example", undefined)).toBe("https://b.example");
    expect(resolveTarget(undefined, undefined)).toBe(DEFAULT_BASE_URL);
  });
});
