import { connect } from "node:net";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { challengeFor } from "../../src/cli/oauth.js";
import { browser, makeServer, openBrowser, runLogin } from "../../src/cli/login.js";
import { launched, opened } from "./setup.js";
import { credentialsPath, loadCredentials } from "../../src/cli/store.js";
import { json, startAs, tokenOk, useTempHome, type FakeAs } from "./helpers.js";

function sink() {
  const chunks: string[] = [];
  return { write: (s: string) => void chunks.push(s), text: () => chunks.join("") };
}

describe("runLogin", () => {
  useTempHome();
  let as: FakeAs;
  beforeEach(async () => {
    as = await startAs();
    as.on("/oauth/register", (_q, r) => json(r, 201, { client_id: "cid-new" }));
    as.on("/oauth/token", tokenOk("ACCESS-SECRET", "REFRESH-SECRET"));
  });
  afterEach(() => as.close());

  /** Plays the browser: hands the callback whatever `respond` builds. */
  const pending: Promise<void>[] = [];
  // An assertion inside the fake browser must fail the TEST, not vanish into a
  // floating promise: afterEach awaits every one and rethrows.
  afterEach(async () => void (await Promise.all(pending.splice(0))));
  function playBrowser(respond: (cb: URL, state: string) => Promise<void> | void) {
    return (authUrl: string) => {
      const u = new URL(authUrl);
      const cb = new URL(u.searchParams.get("redirect_uri")!);
      pending.push(Promise.resolve(respond(cb, u.searchParams.get("state")!)));
    };
  }
  const ok = playBrowser(async (cb, state) => {
    cb.searchParams.set("state", state);
    cb.searchParams.set("code", "CODE-SECRET");
    await fetch(cb);
  });

  it("completes the flow, proves the verifier, saves 0600, and prints no secret", async () => {
    const out = sink(), err = sink();
    let authUrl = "";
    const rc = await runLogin(as.base, { opener: (u) => ((authUrl = u), ok(u)), stdout: out, stderr: err });
    expect(rc).toBe(0);
    const c = loadCredentials()!;
    expect(c).toMatchObject({ accessToken: "ACCESS-SECRET", refreshToken: "REFRESH-SECRET", clientId: "cid-new", baseUrl: as.base });
    // The token request carries the verifier whose S256 is the challenge we sent.
    const form = new URLSearchParams(as.hits.find((h) => h.path === "/oauth/token")!.body);
    const q = new URL(authUrl).searchParams;
    expect(form.get("grant_type")).toBe("authorization_code");
    expect(form.get("code")).toBe("CODE-SECRET");
    expect(challengeFor(form.get("code_verifier")!)).toBe(q.get("code_challenge"));
    expect(form.get("redirect_uri")).toBe(q.get("redirect_uri"));
    // Registered redirect_uri is the loopback one we then listened on.
    expect(JSON.parse(as.hits.find((h) => h.path === "/oauth/register")!.body).redirect_uris).toEqual([q.get("redirect_uri")]);
    expect(q.get("redirect_uri")).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/cb$/);
    // The URL is printed; no secret is.
    expect(out.text()).toContain(authUrl);
    for (const secret of ["ACCESS-SECRET", "REFRESH-SECRET", "CODE-SECRET", form.get("code_verifier")!]) {
      expect(out.text() + err.text()).not.toContain(secret);
    }
  });

  it("waits for the refresh lock before writing, so a rotating proxy cannot overwrite the approval", async () => {
    const lock = credentialsPath() + ".nodelock";
    mkdirSync(dirname(lock), { recursive: true });
    writeFileSync(lock, "99999");
    const out = sink(), err = sink();
    let done = false;
    const p = runLogin(as.base, { opener: ok, stdout: out, stderr: err }).then((rc) => ((done = true), rc));
    await new Promise((r) => setTimeout(r, 600));
    expect(done).toBe(false);
    expect(loadCredentials()).toBeNull();
    rmSync(lock);
    expect(await p).toBe(0);
    expect(loadCredentials()!.accessToken).toBe("ACCESS-SECRET");
  });

  it("opens the browser through the replaceable binding by default, and not when told not to", async () => {
    // Default path: browser.open is the (faked) global one; play the user from it.
    vi.mocked(browser.open).mockImplementation((u) => (opened.push(u), ok(u)));
    expect(await runLogin(as.base, { stdout: sink(), stderr: sink() })).toBe(0);
    expect(opened).toHaveLength(1);
    expect(opened[0]).toContain("/oauth/authorize");
    opened.length = 0;
    await runLogin(as.base, { openBrowser: false, timeoutMs: 100, stdout: sink(), stderr: sink() });
    expect(opened).toEqual([]);
  });

  it("the real launcher picks a platform command and never throws, and the spawn guard sees it", () => {
    // Positive control for the global guard: calling the REAL function must be
    // recorded (and refused) at the child_process boundary.
    openBrowser("http://127.0.0.1:1/x?a=1&b=2");
    expect(launched).toHaveLength(1);
    const { cmd, args } = launched[0]!;
    expect(["xdg-open", "open", "rundll32"]).toContain(cmd);
    expect(args.at(-1)).toBe("http://127.0.0.1:1/x?a=1&b=2"); // one argv entry, & intact
    launched.length = 0; // expected here; the afterEach guard must see none
  });

  it("ignores a callback with the wrong state, then accepts the right one", async () => {
    const rc = await runLogin(as.base, {
      opener: playBrowser(async (cb, state) => {
        const bad = new URL(cb);
        bad.searchParams.set("state", "x".repeat(state.length)); // same length: only the comparison can refuse it
        bad.searchParams.set("code", "FORGED");
        expect((await fetch(bad)).status).toBe(400);
        cb.searchParams.set("state", state);
        cb.searchParams.set("code", "REAL");
        await fetch(cb);
      }),
      stdout: sink(), stderr: sink(),
    });
    expect(rc).toBe(0);
    expect(new URLSearchParams(as.hits.find((h) => h.path === "/oauth/token")!.body).get("code")).toBe("REAL");
  });

  it("survives a state of the wrong LENGTH (timingSafeEqual throws on that)", async () => {
    const rc = await runLogin(as.base, {
      opener: playBrowser(async (cb, state) => {
        for (const s of ["", "x", state + "extra"]) {
          const bad = new URL(cb);
          bad.searchParams.set("state", s);
          bad.searchParams.set("code", "FORGED");
          expect((await fetch(bad)).status).toBe(400);
        }
        cb.searchParams.set("state", state);
        cb.searchParams.set("code", "REAL");
        await fetch(cb);
      }),
      stdout: sink(), stderr: sink(),
    });
    expect(rc).toBe(0);
  });

  it("a forged denial with the wrong state cannot abort the login", async () => {
    const rc = await runLogin(as.base, {
      opener: playBrowser(async (cb, state) => {
        const bad = new URL(cb);
        bad.searchParams.set("state", "y".repeat(state.length));
        bad.searchParams.set("error", "access_denied");
        await fetch(bad);
        cb.searchParams.set("state", state);
        cb.searchParams.set("code", "REAL");
        await fetch(cb);
      }),
      stdout: sink(), stderr: sink(),
    });
    expect(rc).toBe(0);
  });

  it("surfaces access_denied and saves nothing", async () => {
    const err = sink();
    const rc = await runLogin(as.base, {
      opener: playBrowser(async (cb, state) => {
        cb.searchParams.set("state", state);
        cb.searchParams.set("error", "access_denied");
        await fetch(cb);
      }),
      stdout: sink(), stderr: err,
    });
    expect(rc).toBe(1);
    expect(err.text()).toMatch(/denied/);
    expect(existsSync(credentialsPath())).toBe(false);
    expect(as.count("/oauth/token")).toBe(0);
  });

  it("neutralises an arbitrary error string before printing it", async () => {
    const err = sink();
    await runLogin(as.base, {
      opener: playBrowser(async (cb, state) => {
        cb.searchParams.set("state", state);
        cb.searchParams.set("error", "bad\u001b[31m thing" + "z".repeat(200));
        await fetch(cb);
      }),
      stdout: sink(), stderr: err,
    });
    expect(err.text()).not.toContain("\u001b");
    expect(err.text()).toContain("error: bad??31m?thing");
    expect(err.text().length).toBeLessThan(200);
  });

  it("a valid-state callback with neither code nor error is refused and login keeps waiting", async () => {
    const rc = await runLogin(as.base, {
      opener: playBrowser(async (cb, state) => {
        const empty = new URL(cb);
        empty.searchParams.set("state", state);
        expect((await fetch(empty)).status).toBe(400);
        cb.searchParams.set("state", state);
        cb.searchParams.set("code", "REAL");
        await fetch(cb);
      }),
      stdout: sink(), stderr: sink(),
    });
    expect(rc).toBe(0);
  });

  it("times out with a message and saves nothing", async () => {
    const err = sink();
    const rc = await runLogin(as.base, { opener: () => {}, timeoutMs: 150, stdout: sink(), stderr: err });
    expect(rc).toBe(1);
    expect(err.text()).toMatch(/Timed out/);
    expect(existsSync(credentialsPath())).toBe(false);
  });

  it("a refused code exchange saves nothing", async () => {
    as.on("/oauth/token", (_q, r) => json(r, 400, {}));
    const err = sink();
    expect(await runLogin(as.base, { opener: ok, stdout: sink(), stderr: err })).toBe(1);
    expect(err.text()).toMatch(/refused the code \(400\)/);
    expect(existsSync(credentialsPath())).toBe(false);
  });

  it.each([
    ["no refresh_token", { access_token: "a", expires_in: 3600 }],
    ["no access_token", { refresh_token: "r", expires_in: 3600 }],
    ["no expires_in", { access_token: "a", refresh_token: "r" }],
    ["negative expires_in", { access_token: "a", refresh_token: "r", expires_in: -1 }],
  ])("an incomplete token response (%s) saves nothing", async (_n, body) => {
    as.on("/oauth/token", (_q, r) => json(r, 200, body));
    expect(await runLogin(as.base, { opener: ok, stdout: sink(), stderr: sink() })).toBe(1);
    expect(existsSync(credentialsPath())).toBe(false);
  });

  it("a non-object token response saves nothing", async () => {
    as.on("/oauth/token", (_q, r) => json(r, 200, "nope"));
    const err = sink();
    expect(await runLogin(as.base, { opener: ok, stdout: sink(), stderr: err })).toBe(1);
    expect(err.text()).toMatch(/not a JSON object/);
  });

  it("reports an unwritable credential location by path, with the reason", async () => {
    mkdirSync(credentialsPath(), { recursive: true }); // a directory where the file goes
    const err = sink();
    expect(await runLogin(as.base, { opener: ok, stdout: sink(), stderr: err })).toBe(1);
    expect(err.text()).toContain(credentialsPath());
    expect(err.text()).toMatch(/could not be saved/);
  });

  it("stops at discovery when the issuer is wrong, before registering or listening", async () => {
    as.meta.issuer = "http://localhost:1";
    const err = sink();
    expect(await runLogin(as.base, { opener: ok, stdout: sink(), stderr: err })).toBe(1);
    expect(as.count("/oauth/register")).toBe(0);
  });
});

describe("loopback server", () => {
  it("binds 127.0.0.1 only", async () => {
    const lb = await makeServer();
    try {
      const a = lb.server.address() as { address: string; family: string };
      expect(a.address).toBe("127.0.0.1");
    } finally {
      lb.close();
    }
  });

  it("answers only GET /cb", async () => {
    const lb = await makeServer();
    lb.setState("S");
    try {
      const base = `http://127.0.0.1:${lb.port}`;
      expect((await fetch(base + "/other?state=S&code=c")).status).toBe(404);
      expect((await fetch(base + "/cb?state=S&code=c", { method: "POST" })).status).toBe(405);
      // Neither of those may have resolved the login.
      const settled = await Promise.race([lb.result.then(() => true), new Promise((r) => setTimeout(() => r(false), 100))]);
      expect(settled).toBe(false);
      expect((await fetch(base + "/cb?state=S&code=c")).status).toBe(200);
      expect(await lb.result).toEqual({ code: "c" });
    } finally {
      lb.close();
    }
  });

  it("refuses everything until a state is set", async () => {
    const lb = await makeServer();
    try {
      expect((await fetch(`http://127.0.0.1:${lb.port}/cb?state=&code=c`)).status).toBe(400);
    } finally {
      lb.close();
    }
  });

  it("never reflects a query parameter into a page", async () => {
    const lb = await makeServer();
    lb.setState("S");
    try {
      const r = await fetch(`http://127.0.0.1:${lb.port}/cb?state=S&error=%3Cscript%3Ealert(1)%3C/script%3E`);
      expect(await r.text()).not.toContain("script");
    } finally {
      lb.close();
    }
  });

  it("drops an idle connection, and an idle socket does not block the real callback", async () => {
    const lb = await makeServer({ idleMs: 150 });
    lb.setState("S");
    try {
      const idle = connect(lb.port, "127.0.0.1"); // browsers preconnect and say nothing
      const closed = new Promise<number>((resolve) => {
        const t = Date.now();
        idle.on("close", () => resolve(Date.now() - t));
        idle.on("error", () => {});
      });
      await new Promise((r) => idle.once("connect", r));
      // The real callback gets through while the idle socket is still open...
      expect((await fetch(`http://127.0.0.1:${lb.port}/cb?state=S&code=c`)).status).toBe(200);
      // ...and the idle one is cut loose by the server, not by us.
      expect(await Promise.race([closed, new Promise<number>((r) => setTimeout(() => r(-1), 2000))])).toBeGreaterThan(0);
    } finally {
      lb.close();
    }
  });
});
