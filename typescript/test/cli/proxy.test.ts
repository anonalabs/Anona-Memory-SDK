import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { json, drive, seed, startAs, tokenOk, useTempHome, type FakeAs } from "./helpers.js";

const call = (id: number | string, method = "tools/list") => JSON.stringify({ jsonrpc: "2.0", id, method });
const reply = (id: unknown, result: unknown = {}) => ({ jsonrpc: "2.0", id, result });

describe("anona mcp proxy", () => {
  useTempHome();
  let as: FakeAs;
  beforeEach(async () => {
    as = await startAs();
    as.on("/mcp", (_q, r, hit) => json(r, 200, reply(JSON.parse(hit.body).id)));
    as.on("/oauth/token", tokenOk("tok-2", "rt-2"));
  });
  afterEach(() => as.close());

  it("forwards a request with the bearer token and relays the answer", async () => {
    seed(as.base);
    const { out, rc } = await drive(as.base, [call(1)]);
    expect(rc).toBe(0);
    expect(out).toEqual([reply(1)]);
    const hit = as.hits.find((h) => h.path === "/mcp")!;
    expect(hit.headers.authorization).toBe("Bearer tok-1");
    expect(JSON.parse(hit.body)).toMatchObject({ method: "tools/list", id: 1 });
  });

  it("answers in order and skips blank lines", async () => {
    seed(as.base);
    const { out } = await drive(as.base, [call(1), "", "   ", call(2), call(3)]);
    expect(out.map((o) => o.id)).toEqual([1, 2, 3]);
  });

  it("re-serialises a pretty-printed upstream body onto one line", async () => {
    seed(as.base);
    as.on("/mcp", (_q, r) => (r.writeHead(200), r.end(JSON.stringify(reply(1), null, 4))));
    const { out } = await drive(as.base, [call(1)]);
    expect(out).toEqual([reply(1)]);
  });

  it("writes nothing for a notification the server accepts with an empty 202", async () => {
    seed(as.base);
    as.on("/mcp", (_q, r) => (r.writeHead(202), r.end()));
    const { out } = await drive(as.base, [JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" })]);
    expect(out).toEqual([]);
  });

  it("turns an empty reply to a REQUEST into an error, so the client does not wait forever", async () => {
    seed(as.base);
    as.on("/mcp", (_q, r) => (r.writeHead(202), r.end()));
    const { out } = await drive(as.base, [call(7)]);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ id: 7, error: { code: -32000 } });
  });

  it("refreshes once on a 401 and retries once", async () => {
    seed(as.base);
    let first = true;
    as.on("/mcp", (_q, r, hit) => {
      if (first) return ((first = false), json(r, 401, {}));
      json(r, 200, reply(JSON.parse(hit.body).id));
    });
    const { out } = await drive(as.base, [call(1)]);
    expect(out).toEqual([reply(1)]);
    expect(as.count("/mcp")).toBe(2);
    expect(as.count("/oauth/token")).toBe(1);
    expect(as.hits.filter((h) => h.path === "/mcp").map((h) => h.headers.authorization)).toEqual(["Bearer tok-1", "Bearer tok-2"]);
  });

  it("NEVER loops: a server that always 401s sees exactly two POSTs per message", async () => {
    seed(as.base);
    as.on("/mcp", (_q, r) => json(r, 401, {}));
    const { out, err } = await drive(as.base, [call(1), call(2)]);
    expect(as.count("/mcp")).toBe(4); // 2 messages x (try + one retry)
    expect(out.map((o) => [o.id, o.error.message])).toEqual([
      [1, expect.stringContaining("anona login")],
      [2, expect.stringContaining("anona login")],
    ]);
    expect(err).toContain("anona login");
  });

  it("a token endpoint that rejects the refresh ends the call with a login hint, not a retry", async () => {
    seed(as.base, { offset: -10 });
    as.on("/oauth/token", (_q, r) => json(r, 400, {}));
    const { out } = await drive(as.base, [call(1)]);
    expect(out[0]).toMatchObject({ id: 1, error: { message: expect.stringContaining("anona login") } });
    expect(as.count("/mcp")).toBe(0);
  });

  it("reports not-logged-in as a JSON-RPC error and keeps serving", async () => {
    const { out, err, rc } = await drive(as.base, [call(1), call(2)]);
    expect(rc).toBe(0);
    expect(out.map((o) => o.id)).toEqual([1, 2]);
    expect(out[0].error.message).toMatch(/Not logged in/);
    expect(err).toMatch(/Not logged in/);
  });

  it("maps an HTTP 5xx to an error carrying the request id", async () => {
    seed(as.base);
    as.on("/mcp", (_q, r) => json(r, 500, {}));
    const { out } = await drive(as.base, [call("abc")]);
    expect(out[0]).toMatchObject({ id: "abc", error: { message: expect.stringContaining("HTTP 500") } });
  });

  it("maps a non-JSON 200 to an error rather than relaying it to stdout", async () => {
    seed(as.base);
    as.on("/mcp", (_q, r) => (r.writeHead(200), r.end("<html>proxy error</html>")));
    const { out } = await drive(as.base, [call(1)]);
    expect(out[0]).toMatchObject({ id: 1, error: { message: expect.stringContaining("not JSON") } });
  });

  it("maps a network failure to an error, not a crash", async () => {
    seed(as.base);
    await as.close();
    const { out, rc } = await drive(as.base, [call(1)]);
    expect(rc).toBe(0);
    expect(out[0]).toMatchObject({ id: 1, error: { message: expect.stringContaining("Request failed") } });
    as = await startAs();
  });

  it("a failing NOTIFICATION produces no stdout line (it would be unsolicited) but is logged", async () => {
    seed(as.base);
    as.on("/mcp", (_q, r) => json(r, 500, {}));
    const { out, err } = await drive(as.base, [JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" })]);
    expect(out).toEqual([]);
    expect(err).toContain("HTTP 500");
  });

  it("answers invalid JSON with a parse error with a null id", async () => {
    const { out } = await drive(as.base, ["{nope"]);
    expect(out).toEqual([{ jsonrpc: "2.0", id: null, error: { code: -32700, message: expect.any(String) } }]);
  });

  it.each(["5", '"hi"', "null", "true"])("answers a non-object line (%s) with invalid request", async (line) => {
    const { out } = await drive(as.base, [line]);
    expect(out[0]).toMatchObject({ id: null, error: { code: -32600 } });
    expect(as.hits).toHaveLength(0);
  });

  it("forwards a JSON-RPC batch and relays the array", async () => {
    seed(as.base);
    as.on("/mcp", (_q, r, hit) => json(r, 200, JSON.parse(hit.body).map((m: any) => reply(m.id))));
    const { out } = await drive(as.base, [`[${call(1)},${call(2)}]`]);
    expect(out).toEqual([[reply(1), reply(2)]]);
  });

  it("a failed batch returns an array of errors, one per REQUEST, none for notifications", async () => {
    seed(as.base);
    as.on("/mcp", (_q, r) => json(r, 500, {}));
    const note = JSON.stringify({ jsonrpc: "2.0", method: "n" });
    const { out } = await drive(as.base, [`[${call(1)},${note},${call(2)}]`]);
    expect(out).toHaveLength(1);
    expect(out[0].map((e: any) => e.id)).toEqual([1, 2]);
  });

  it("a batch of only notifications fails silently", async () => {
    seed(as.base);
    as.on("/mcp", (_q, r) => json(r, 500, {}));
    const note = JSON.stringify({ jsonrpc: "2.0", method: "n" });
    const { out } = await drive(as.base, [`[${note}]`]);
    expect(out).toEqual([]);
  });

  it("an id of null still counts as a request and gets its error", async () => {
    seed(as.base);
    as.on("/mcp", (_q, r) => json(r, 500, {}));
    const { out } = await drive(as.base, [JSON.stringify({ jsonrpc: "2.0", id: null, method: "x" })]);
    expect(out).toHaveLength(1);
    expect(out[0].id).toBeNull();
  });

  it("never writes a token to stdout or stderr", async () => {
    seed(as.base, { access: "SECRET-ACCESS", refresh: "SECRET-REFRESH", offset: -10 });
    as.on("/oauth/token", tokenOk("SECRET-NEW", "SECRET-NEWR"));
    as.on("/mcp", (_q, r) => json(r, 500, {}));
    const { out, err } = await drive(as.base, [call(1)]);
    expect(JSON.stringify(out) + err).not.toMatch(/SECRET/);
  });

  it("honours a base URL with a trailing slash", async () => {
    seed(as.base);
    const { out } = await drive(as.base + "/", [call(1)]);
    expect(out).toEqual([reply(1)]);
  });
});
