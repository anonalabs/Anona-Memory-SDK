import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { authorizeUrl, challengeFor, discover, newPkce, OAuthError, registerClient } from "../../src/cli/oauth.js";
import { json, startAs, type FakeAs } from "./helpers.js";

describe("PKCE", () => {
  it("matches the RFC 7636 appendix B vector", () => {
    // Frozen: if the S256 construction ever drifts, this is the one that says so.
    expect(challengeFor("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")).toBe(
      "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
    );
  });
  it("newPkce returns the S256 of its own verifier, in range, unpadded", () => {
    const { verifier, challenge } = newPkce();
    expect(challenge).toBe(challengeFor(verifier));
    expect(verifier.length).toBeGreaterThanOrEqual(43);
    expect(verifier.length).toBeLessThanOrEqual(128);
    expect(verifier).not.toContain("=");
    expect(newPkce().verifier).not.toBe(verifier);
  });
});

describe("authorizeUrl", () => {
  const meta = {
    issuer: "https://a.test",
    authorization_endpoint: "https://a.test/oauth/authorize",
    token_endpoint: "https://a.test/t",
    registration_endpoint: "https://a.test/r",
    code_challenge_methods_supported: ["S256"],
  };
  it("carries state, S256 and the challenge", () => {
    const u = new URL(authorizeUrl(meta, "cid", "http://127.0.0.1:1/cb", "CHAL", "ST"));
    expect(u.origin + u.pathname).toBe("https://a.test/oauth/authorize");
    expect(u.searchParams.get("state")).toBe("ST");
    expect(u.searchParams.get("code_challenge")).toBe("CHAL");
    expect(u.searchParams.get("code_challenge_method")).toBe("S256");
    expect(u.searchParams.get("client_id")).toBe("cid");
    expect(u.searchParams.get("redirect_uri")).toBe("http://127.0.0.1:1/cb");
  });
  it("refuses a server advertising only plain", () => {
    expect(() => authorizeUrl({ ...meta, code_challenge_methods_supported: ["plain"] }, "c", "r", "x", "s")).toThrow(OAuthError);
  });
  it("refuses a string where a list belongs (substring trap)", () => {
    // "S256" is a substring of this, and not an element of anything.
    expect(() => authorizeUrl({ ...meta, code_challenge_methods_supported: "S256,plain" }, "c", "r", "x", "s")).toThrow(OAuthError);
  });
  it("refuses a missing list", () => {
    const { code_challenge_methods_supported: _, ...rest } = meta;
    expect(() => authorizeUrl(rest as never, "c", "r", "x", "s")).toThrow(OAuthError);
  });
});

describe("discover and registerClient", () => {
  let as: FakeAs;
  beforeEach(async () => void (as = await startAs()));
  afterEach(() => as.close());

  it("returns the metadata when everything is on the asked origin", async () => {
    expect((await discover(as.base)).token_endpoint).toBe(as.base + "/oauth/token");
  });
  it("accepts a trailing slash on the base url", async () => {
    await expect(discover(as.base + "/")).resolves.toBeTruthy();
  });
  it("rejects an issuer whose host is not the one asked", async () => {
    as.meta.issuer = "http://localhost:" + new URL(as.base).port; // other host, same port
    await expect(discover(as.base)).rejects.toThrow(/issuer/);
  });
  it("rejects a self-consistent document for a different issuer (every endpoint on the wrong host)", async () => {
    // The endpoint-origin check passes here, so only the issuer check can refuse it.
    const evil = "http://evil.test";
    Object.assign(as.meta, {
      issuer: evil,
      authorization_endpoint: evil + "/a",
      token_endpoint: evil + "/t",
      registration_endpoint: evil + "/r",
    });
    await expect(discover(as.base)).rejects.toThrow(/issuer/);
  });
  it("rejects an issuer on a different scheme", async () => {
    as.meta.issuer = as.base.replace("http:", "https:");
    await expect(discover(as.base)).rejects.toThrow(/issuer/);
  });
  it("rejects an issuer that is not a URL at all", async () => {
    as.meta.issuer = "not a url";
    await expect(discover(as.base)).rejects.toThrow(/issuer/);
  });
  it("rejects an endpoint off the issuer's origin even when the issuer is right", async () => {
    as.meta.authorization_endpoint = "http://evil.test/oauth/authorize";
    await expect(discover(as.base)).rejects.toThrow(/authorization_endpoint/);
  });
  it("rejects a token endpoint off the issuer's origin", async () => {
    as.meta.token_endpoint = "http://evil.test/t";
    await expect(discover(as.base)).rejects.toThrow(/token_endpoint/);
  });
  it("rejects a missing endpoint", async () => {
    delete as.meta.registration_endpoint;
    await expect(discover(as.base)).rejects.toThrow(/missing registration_endpoint/);
  });
  it("rejects userinfo in an endpoint", async () => {
    const u = new URL(as.base + "/oauth/authorize");
    u.username = "evil";
    as.meta.authorization_endpoint = u.toString();
    await expect(discover(as.base)).rejects.toThrow(/authorization_endpoint/);
  });
  it("rejects non-200, non-JSON and non-object documents", async () => {
    as.on("/.well-known/oauth-authorization-server", (_q, r) => json(r, 500, {}));
    await expect(discover(as.base)).rejects.toThrow(/500/);
    as.on("/.well-known/oauth-authorization-server", (_q, r) => (r.writeHead(200), r.end("<html>")));
    await expect(discover(as.base)).rejects.toThrow(/not JSON/);
    as.on("/.well-known/oauth-authorization-server", (_q, r) => json(r, 200, [1]));
    await expect(discover(as.base)).rejects.toThrow(/not an object/);
  });
  it("wraps a connection failure in OAuthError", async () => {
    const base = as.base;
    await as.close();
    await expect(discover(base)).rejects.toThrow(OAuthError);
    as = await startAs();
  });

  it("registers with the loopback redirect and no client auth", async () => {
    as.on("/oauth/register", (_q, r) => json(r, 201, { client_id: "abc" }));
    const meta = await discover(as.base);
    expect(await registerClient(meta, "http://127.0.0.1:9/cb")).toBe("abc");
    const sent = JSON.parse(as.hits.find((h) => h.path === "/oauth/register")!.body);
    expect(sent.redirect_uris).toEqual(["http://127.0.0.1:9/cb"]);
    expect(sent.token_endpoint_auth_method).toBe("none");
  });
  it("rejects a failed registration and one with no client_id", async () => {
    const meta = await discover(as.base);
    as.on("/oauth/register", (_q, r) => json(r, 400, {}));
    await expect(registerClient(meta, "x")).rejects.toThrow(/400/);
    as.on("/oauth/register", (_q, r) => json(r, 201, {}));
    await expect(registerClient(meta, "x")).rejects.toThrow(/no client_id/);
  });
});
