"""`anona login`: one approval for the command line and `anona mcp`.

`anona login` runs ONE browser approval, scope `cli`, stored in
`cli-credentials.json` (`typ=cli_access`). The gateway accepts that token on
the REST data plane and on `/mcp`, so the data commands and `anona mcp` both
present it. `anona login --mcp` still mints a separate MCP-only token
(`credentials.json`, `typ=mcp_access`, refused on REST), which `anona mcp`
uses only when there is no command-line login for its deployment. A
deployment that will not grant `cli` gets the MCP approval instead.

Every case runs a real authorization server and data plane on loopback inside
the subprocess (see _cli.py), and plays the user with a fake
`webbrowser.open` that follows the real authorize URL to the real listener.
What is asserted is what crossed the wire and what landed on disk.
"""
from __future__ import annotations

import textwrap

from _cli import ok, run_cli

PRELUDE = r"""
import base64, io, json, os, stat, sys, threading, time, urllib.request
from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from anona.cli import login, main as cli, proxy, store

os.environ.pop("ANONA_BASE_URL", None)
os.environ.pop("ANONA_SPACE_ID", None)

MODE = {"cli_scope": "grant", "claim": False}
REG, AUTHZ, GRANTS, REVOKED, DATA, OPENED, MCPS = [], [], [], [], [], [], []
CODES, LIVE, REFRESH, DEAD = {}, set(), {}, set()
N = {"n": 0}


def _b64(d):
    return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")


def jwt(typ):
    N["n"] += 1
    return _b64({"alg": "HS256"}) + "." + _b64({"typ": typ, "n": N["n"]}) + ".sig"


def mint(typ, claimed=False):
    at = jwt(typ)
    N["n"] += 1
    rt = "RT-%s-%d" % (typ, N["n"])
    LIVE.add(at)
    REFRESH[rt] = typ
    body = {"access_token": at, "refresh_token": rt, "expires_in": 3600, "token_type": "Bearer"}
    if claimed:
        body["temp_claimed"] = True
    return body


class H(BaseHTTPRequestHandler):
    def _send(self, status, body=None, headers=None):
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path == "/.well-known/oauth-authorization-server":
            return self._send(200, META)
        if parts.path == "/oauth/authorize":
            q = {k: v[0] for k, v in parse_qs(parts.query).items()}
            AUTHZ.append(q)
            if q.get("scope") == "cli" and MODE["cli_scope"] == "refuse":
                return self._send(400, {"error": {"error": "invalid_scope",
                                                  "error_description": "Scope 'cli' is not granted here."}})
            return self._send(302, None, {"location": BASE + "/consent?req=x"})
        if parts.path == "/v1/spaces/":
            return self._data({})
        return self._send(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        path = urlsplit(self.path).path
        if path == "/oauth/register":
            body = json.loads(raw)
            REG.append(body)
            return self._send(201, {"client_id": "cid-%d" % len(REG)})
        if path == "/oauth/token":
            form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
            GRANTS.append(form)
            if form["grant_type"] == "authorization_code":
                scope = CODES.pop(form["code"])
                old = MODE["cli_scope"] == "old"
                typ = "cli_access" if scope == "cli" and not old else "mcp_access"
                claimed = MODE["claim"] and len([g for g in GRANTS if g["grant_type"] == "authorization_code"]) == 1
                return self._send(200, mint(typ, claimed))
            typ = REFRESH.pop(form["refresh_token"], None)
            if typ is None or MODE.get("refresh") == "refuse":
                return self._send(400, {"error": "invalid_grant"})
            return self._send(200, mint(typ))
        if path == "/oauth/revoke":
            REVOKED.append(parse_qs(raw.decode())["token"][0])
            return self._send(200, {"ok": True})
        if path == "/mcp":
            return self._mcp(json.loads(raw))
        return self._data(json.loads(raw or b"{}"))

    def _data(self, body):
        auth = self.headers.get("authorization") or ""
        token = auth.removeprefix("Bearer ")
        DATA.append((self.path, token, body))
        if token.startswith("anona_"):
            pass  # an API key or a temp token: this server does not judge them
        elif token not in LIVE or token in DEAD:
            return self._send(401, {"error": {"code": "invalid_token", "message": "Token refused."}})
        elif json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==")).get("typ") != "cli_access":
            return self._send(401, {"error": {"code": "invalid_token", "message": "MCP token on REST."}})
        if self.path.endswith("/record"):
            return self._send(200, {"memory_id": "m1", "memory_ids": ["m1"], "status": "stored"})
        if self.path.endswith("/retrieve"):
            return self._send(200, {"results": [{"content": "x"}]})
        return self._send(200, {"spaces": [], "total": 0})

    def _mcp(self, msg):
        # The gateway's /mcp: either OAuth typ, or a temp token.
        token = (self.headers.get("authorization") or "").removeprefix("Bearer ")
        MCPS.append(token)
        if not token.startswith("anona_") and (token not in LIVE or token in DEAD):
            return self._send(401, {"error": "invalid_token"})
        return self._send(200, {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"tools": []}})

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % srv.server_address[1]
META = {
    "issuer": BASE,
    "authorization_endpoint": BASE + "/oauth/authorize",
    "token_endpoint": BASE + "/oauth/token",
    "registration_endpoint": BASE + "/oauth/register",
    "revocation_endpoint": BASE + "/oauth/revoke",
    "code_challenge_methods_supported": ["S256"],
}


def approve(url):
    # Plays the person: reads the real authorize URL, "clicks Allow" (the
    # code remembers the scope it was asked for), and follows the redirect to
    # the real loopback listener.
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    OPENED.append(q["scope"])
    code = "code-%d" % len(OPENED)
    CODES[code] = q["scope"]
    target = q["redirect_uri"] + "?code=" + code + "&state=" + q["state"]
    threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=5).read(), daemon=True).start()
    return True


login.webbrowser.open = approve


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = cli.main(argv)
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


CLI_FILE = Path.home() / ".anona" / "cli-credentials.json"
MCP_FILE = Path.home() / ".anona" / "credentials.json"


def doc(p):
    return json.loads(p.read_text())


def mode(p):
    return stat.S_IMODE(p.stat().st_mode)


def seed_cli(offset=3600, base=None):
    body = mint("cli_access")
    store.save(store.Credentials(body["access_token"], body["refresh_token"], time.time() + offset,
                                 "cid-cli", base or BASE), store.cli_path())
    return body


def seed_mcp(temp=False, signed_in=True, base=None):
    body = mint("mcp_access")
    store.save(store.Credentials(
        body["access_token"] if signed_in else "", body["refresh_token"] if signed_in else "",
        time.time() + 3600 if signed_in else 0.0, "cid-mcp" if signed_in else "", base or BASE,
        temp_token="anona_tmp_TEMPSECRET" if temp else None,
        temp_expires_at=time.time() + 3600 if temp else None))
    return body


def typ_of(t):
    return json.loads(base64.urlsafe_b64decode(t.split(".")[1] + "=="))["typ"]


def mcp_call(base=None):
    out, err = io.StringIO(), io.StringIO()
    line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
    rc = proxy.run_proxy(base or BASE, io.StringIO(line), out, err)
    return rc, out.getvalue(), err.getvalue()


def secrets_in(text):
    toks = [t for t in list(LIVE) + list(REFRESH) + REVOKED if t]
    return [t for t in toks if t in text] + (["temp"] if "TEMPSECRET" in text else [])
"""


def _run(body, tmp_path, env=None, timeout=60):
    return run_cli(
        PRELUDE + textwrap.dedent(body),
        env={"HOME": str(tmp_path), **(env or {})},
        timeout=timeout,
    )


# --- login: one approval ------------------------------------------------------


def test_login_is_one_approval_into_the_command_line_file(tmp_path):
    ok(_run("""
        rc, out, err = run_main(["login", "--base-url", BASE])
        assert rc == 0, (rc, out, err)
        assert OPENED == ["cli"] and len(REG) == 1, (OPENED, REG)
        # The CLI's own preflight of the cli scope.
        assert [a["scope"] for a in AUTHZ] == ["cli"], AUTHZ
        assert REG[0]["redirect_uris"][0].startswith("http://127.0.0.1:"), REG
        c = doc(CLI_FILE)
        assert typ_of(c["access_token"]) == "cli_access" and c["base_url"] == BASE, c
        assert sorted(c) == ["access_token", "base_url", "client_id", "expires_at", "refresh_token"], c
        assert mode(CLI_FILE) == 0o600 and not MCP_FILE.exists()
        assert "`anona mcp`" in out, out
        assert not secrets_in(out + err), secrets_in(out + err)
        print("OK")
    """, tmp_path))


def test_login_cli_does_only_the_command_line_approval(tmp_path):
    ok(_run("""
        rc, out, err = run_main(["login", "--cli", "--base-url", BASE])
        assert rc == 0, (rc, out, err)
        assert OPENED == ["cli"] and len(REG) == 1, (OPENED, REG)
        assert CLI_FILE.exists() and not MCP_FILE.exists()
        print("OK")
    """, tmp_path))


def test_login_mcp_does_only_the_mcp_approval_and_leaves_the_cli_file(tmp_path):
    ok(_run("""
        seed_cli()
        before = CLI_FILE.read_bytes()
        rc, out, err = run_main(["login", "--mcp", "--base-url", BASE])
        assert rc == 0, (rc, out, err)
        assert OPENED == ["mcp"] and len(REG) == 1, (OPENED, REG)
        assert AUTHZ == [], AUTHZ  # no preflight for mcp
        assert MCP_FILE.exists() and CLI_FILE.read_bytes() == before
        print("OK")
    """, tmp_path))


def test_login_cli_and_mcp_together_is_a_usage_error(tmp_path):
    ok(_run("""
        rc, out, err = run_main(["login", "--cli", "--mcp", "--base-url", BASE])
        assert rc == 2 and REG == [], (rc, err)
        print("OK")
    """, tmp_path))


# --- a deployment that will not grant `cli` ----------------------------------


def test_invalid_scope_falls_back_to_the_mcp_approval(tmp_path):
    ok(_run("""
        MODE["cli_scope"] = "refuse"
        rc, out, err = run_main(["login", "--base-url", BASE])
        assert rc == 0, (rc, out, err)
        # Refused before any browser: the only window opened is MCP's.
        assert OPENED == ["mcp"], OPENED
        assert "does not issue command-line credentials" in err and "not granted here" in err, err
        assert "Signing in for MCP only" in err, err
        assert MCP_FILE.exists() and not CLI_FILE.exists()
        print("OK")
    """, tmp_path))


def test_invalid_scope_on_login_cli_alone_is_a_failure(tmp_path):
    ok(_run("""
        MODE["cli_scope"] = "refuse"
        rc, out, err = run_main(["login", "--cli", "--base-url", BASE])
        assert rc == 1 and OPENED == [] and not CLI_FILE.exists(), (rc, OPENED, err)
        print("OK")
    """, tmp_path))


def test_an_older_server_minting_mcp_for_cli_saves_nothing_and_revokes_it(tmp_path):
    # A gateway that predates cli tokens takes scope=cli and mints mcp_access.
    # Stored, it would leave the CLI "signed in" to a token REST refuses.
    ok(_run("""
        MODE["cli_scope"] = "old"
        rc, out, err = run_main(["login", "--base-url", BASE])
        assert rc == 0, (rc, out, err)
        assert OPENED == ["cli", "mcp"], OPENED
        assert not CLI_FILE.exists()
        first_rt = [g for g in GRANTS if g["grant_type"] == "authorization_code"]
        assert len(REVOKED) == 1 and REVOKED[0].startswith("RT-mcp_access"), REVOKED
        assert doc(MCP_FILE)["refresh_token"] not in REVOKED
        assert "issued an MCP credential instead" in err, err
        print("OK")
    """, tmp_path))


def test_everything_failing_is_a_non_zero_exit(tmp_path):
    ok(_run("""
        MODE["cli_scope"] = "refuse"
        def deny(url):
            q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
            target = q["redirect_uri"] + "?error=access_denied&state=" + q["state"]
            threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=5).read(), daemon=True).start()
            return True
        login.webbrowser.open = deny
        rc, out, err = run_main(["login", "--base-url", BASE])
        assert rc == 1 and not CLI_FILE.exists() and not MCP_FILE.exists(), (rc, err)
        print("OK")
    """, tmp_path))


def test_a_temp_profile_is_claimed_by_the_one_approval_and_forgotten(tmp_path):
    ok(_run("""
        seed_mcp(temp=True, signed_in=False)
        MODE["claim"] = True
        rc, out, err = run_main(["login", "--base-url", BASE])
        assert rc == 0, (rc, out, err)
        assert len(REG) == 1 and REG[0].get("temp_token") == "anona_tmp_TEMPSECRET", REG
        # Spent by the claim: a file that held only the profile is gone.
        assert not MCP_FILE.exists() and "temp_token" not in doc(CLI_FILE)
        assert not secrets_in(out + err)
        print("OK")
    """, tmp_path))


def test_an_unclaimed_temp_profile_stays_in_the_mcp_file_on_login_cli(tmp_path):
    ok(_run("""
        seed_mcp(temp=True, signed_in=False)
        before = MCP_FILE.read_bytes()
        rc, out, err = run_main(["login", "--cli", "--base-url", BASE])
        assert rc == 0, (rc, out, err)
        assert MCP_FILE.read_bytes() == before
        assert "temp_token" not in doc(CLI_FILE)
        print("OK")
    """, tmp_path))


# --- which credential a data command presents --------------------------------


def test_the_cli_login_beats_the_temp_profile_and_the_mcp_login_is_never_sent(tmp_path):
    ok(_run("""
        mcp = seed_mcp(temp=True)
        c = seed_cli()
        rc, out, err = run_main(["record", "a fact", "--space", "work"])
        assert rc == 0, (rc, err)
        assert [d[1] for d in DATA] == [c["access_token"]], DATA
        assert DATA[0][2]["space_id"] == "work"
        print("OK")
    """, tmp_path))


def test_an_api_key_from_config_env_beats_the_cli_login(tmp_path):
    ok(_run("""
        seed_cli()
        (Path.home() / ".anona" / "config.env").write_text(
            "ANONA_API_KEY=anona_live_FROMFILE\\nANONA_BASE_URL=" + BASE + "\\n")
        rc, out, err = run_main(["record", "a fact", "--space", "work"])
        assert rc == 0, (rc, err)
        assert [d[1] for d in DATA] == ["anona_live_FROMFILE"], DATA
        print("OK")
    """, tmp_path))


def test_an_api_key_in_the_environment_beats_the_cli_login(tmp_path):
    ok(_run("""
        seed_cli()
        os.environ["ANONA_API_KEY"] = "anona_live_FROMENV"
        os.environ["ANONA_BASE_URL"] = BASE
        rc, out, err = run_main(["record", "a fact", "--space", "work"])
        assert rc == 0, (rc, err)
        assert [d[1] for d in DATA] == ["anona_live_FROMENV"], DATA
        print("OK")
    """, tmp_path))


def test_the_cli_login_beats_a_temp_profile_even_without_an_mcp_login(tmp_path):
    ok(_run("""
        seed_mcp(temp=True, signed_in=False)
        c = seed_cli()
        rc, out, err = run_main(["retrieve", "q", "--space", "work"])
        assert rc == 0, (rc, err)
        assert [d[1] for d in DATA] == [c["access_token"]], DATA
        print("OK")
    """, tmp_path))


def test_mcp_login_alone_is_refused_and_points_at_login_cli(tmp_path):
    ok(_run("""
        seed_mcp()
        rc, out, err = run_main(["record", "a fact", "--space", "work"])
        assert rc == 4 and DATA == [], (rc, DATA)
        assert "anona login --cli" in err, err
        print("OK")
    """, tmp_path))


def test_the_cli_login_needs_a_space_and_sends_nothing_without_one(tmp_path):
    ok(_run("""
        seed_cli()
        rc, out, err = run_main(["record", "a fact"])
        assert rc == 2 and DATA == [], (rc, DATA)
        assert "--space" in err and "ANONA_SPACE_ID" in err, err
        os.environ["ANONA_SPACE_ID"] = "fromenv"
        rc, out, err = run_main(["record", "a fact"])
        assert rc == 0 and DATA[-1][2]["space_id"] == "fromenv", (rc, err, DATA)
        print("OK")
    """, tmp_path))


def test_a_verb_with_no_space_works_on_the_cli_login(tmp_path):
    ok(_run("""
        c = seed_cli()
        rc, out, err = run_main(["spaces", "list"])
        assert rc == 0, (rc, err)
        assert DATA[-1][1] == c["access_token"]
        print("OK")
    """, tmp_path))


# --- refresh, rotation and a refused token -----------------------------------


def test_an_expired_cli_token_is_refreshed_and_rotated_on_disk(tmp_path):
    ok(_run("""
        mcp = seed_mcp()
        mcp_before = MCP_FILE.read_bytes()
        old = seed_cli(offset=-10)
        rc, out, err = run_main(["record", "a fact", "--space", "work"])
        assert rc == 0, (rc, err)
        refreshes = [g for g in GRANTS if g["grant_type"] == "refresh_token"]
        assert [g["refresh_token"] for g in refreshes] == [old["refresh_token"]], refreshes
        assert refreshes[0]["client_id"] == "cid-cli"
        new = doc(CLI_FILE)
        assert new["refresh_token"] != old["refresh_token"] and new["access_token"] != old["access_token"]
        assert new["refresh_token"] in REFRESH  # the live one the server just issued
        assert DATA[-1][1] == new["access_token"]
        assert mode(CLI_FILE) == 0o600
        assert MCP_FILE.read_bytes() == mcp_before, "the MCP credential must not move"
        assert not list((Path.home() / ".anona").glob(".cli-credentials-*")), "temp file left behind"
        assert not secrets_in(out + err)
        print("OK")
    """, tmp_path))


def test_a_401_renews_the_cli_token_once_and_retries(tmp_path):
    ok(_run("""
        c = seed_cli()
        DEAD.add(c["access_token"])  # looks fresh, refused anyway (revoked server-side)
        rc, out, err = run_main(["record", "a fact", "--space", "work"])
        assert rc == 0, (rc, err)
        assert [d[1] for d in DATA][0] == c["access_token"] and len(DATA) == 2, DATA
        assert DATA[1][1] == doc(CLI_FILE)["access_token"] != c["access_token"]
        print("OK")
    """, tmp_path))


def test_a_401_whose_refresh_also_fails_exits_4_and_says_login_cli(tmp_path):
    ok(_run("""
        c = seed_cli()
        DEAD.add(c["access_token"])
        MODE["refresh"] = "refuse"
        rc, out, err = run_main(["record", "a fact", "--space", "work"])
        assert rc == 4, (rc, err)
        assert "anona login --cli" in err, err
        assert len(DATA) == 1, DATA  # no retry without a new token
        assert not secrets_in(out + err)
        print("OK")
    """, tmp_path))


def test_a_401_twice_exits_4_and_says_login_cli(tmp_path):
    ok(_run("""
        c = seed_cli()
        DEAD.add(c["access_token"])
        orig = mint
        def dead_mint(typ, claimed=False):
            body = orig(typ, claimed); DEAD.add(body["access_token"]); return body
        globals()["mint"] = dead_mint
        rc, out, err = run_main(["record", "a fact", "--space", "work"])
        assert rc == 4 and len(DATA) == 2, (rc, DATA, err)
        assert "anona login --cli" in err, err
        print("OK")
    """, tmp_path))


# --- pinning ------------------------------------------------------------------


def test_the_cli_login_is_pinned_to_its_deployment(tmp_path):
    ok(_run("""
        seed_cli()
        rc, out, err = run_main(["record", "a fact", "--space", "work", "--base-url", "https://other.example"])
        assert rc == 4 and DATA == [], (rc, DATA)
        assert "anona login --base-url https://other.example" in err, err
        os.environ["ANONA_BASE_URL"] = "https://other.example"
        rc, out, err = run_main(["retrieve", "q", "--space", "work"])
        assert rc == 4 and DATA == [], (rc, DATA)
        print("OK")
    """, tmp_path))


def test_a_cli_file_for_a_plain_http_remote_host_is_refused(tmp_path):
    ok(_run("""
        seed_cli(base="http://evil.example")
        rc, out, err = run_main(["record", "a fact", "--space", "work"])
        assert rc == 4 and DATA == [], (rc, DATA)
        assert "https://" in err, err
        print("OK")
    """, tmp_path))


# --- logout and status --------------------------------------------------------


def test_logout_removes_both_and_revokes_both(tmp_path):
    ok(_run("""
        m, c = seed_mcp(), seed_cli()
        rc, out, err = run_main(["logout"])
        assert rc == 0, (rc, err)
        assert not CLI_FILE.exists() and not MCP_FILE.exists()
        assert sorted(REVOKED) == sorted([m["refresh_token"], c["refresh_token"]]), REVOKED
        assert not secrets_in(out + err)
        print("OK")
    """, tmp_path))


def test_logout_cli_removes_only_the_cli_credential(tmp_path):
    ok(_run("""
        m, c = seed_mcp(), seed_cli()
        before = MCP_FILE.read_bytes()
        rc, out, err = run_main(["logout", "--cli"])
        assert rc == 0 and not CLI_FILE.exists() and MCP_FILE.read_bytes() == before, (rc, err)
        assert REVOKED == [c["refresh_token"]], REVOKED
        print("OK")
    """, tmp_path))


def test_logout_mcp_removes_only_the_mcp_credential(tmp_path):
    ok(_run("""
        m, c = seed_mcp(), seed_cli()
        before = CLI_FILE.read_bytes()
        rc, out, err = run_main(["logout", "--mcp"])
        assert rc == 0 and not MCP_FILE.exists() and CLI_FILE.read_bytes() == before, (rc, err)
        assert REVOKED == [m["refresh_token"]], REVOKED
        print("OK")
    """, tmp_path))


def test_logout_cli_needs_no_force_beside_a_live_temp_profile(tmp_path):
    ok(_run("""
        seed_mcp(temp=True, signed_in=False)
        seed_cli()
        rc, out, err = run_main(["logout"])
        assert rc == 1 and CLI_FILE.exists() and MCP_FILE.exists(), "a refused logout removes nothing"
        assert "anona logout --cli" in err, err
        rc, out, err = run_main(["logout", "--cli"])
        assert rc == 0 and not CLI_FILE.exists() and MCP_FILE.exists(), (rc, err)
        print("OK")
    """, tmp_path))


def test_logout_still_works_when_revocation_cannot_reach_the_server(tmp_path):
    ok(_run("""
        seed_cli(base="https://unreachable.invalid")
        rc, out, err = run_main(["logout", "--cli"])
        assert rc == 0 and not CLI_FILE.exists(), (rc, err)
        assert "could not be told to revoke" in err, err
        print("OK")
    """, tmp_path))


def test_status_reports_both_credentials_and_no_secret(tmp_path):
    ok(_run("""
        seed_mcp(); seed_cli()
        rc, out, err = run_main(["status"])
        assert rc == 0, (rc, out, err)
        assert "Command-line login for " + BASE in out, out
        assert "`anona mcp`" in out, out
        assert "An MCP-only login for " + BASE + " is also stored" in out, out
        assert not secrets_in(out + err) and "cid-" not in out, out
        print("OK")
    """, tmp_path))


def test_status_with_only_the_cli_login_is_success(tmp_path):
    ok(_run("""
        seed_cli()
        rc, out, err = run_main(["status"])
        assert rc == 0, (rc, out)
        assert "Command-line login" in out and "`anona mcp`" in out, out
        # One login covers MCP: nothing tells the user to sign in again for it.
        assert "anona login --mcp" not in out, out
        print("OK")
    """, tmp_path))


def test_status_says_an_api_key_shadows_the_cli_login(tmp_path):
    ok(_run("""
        seed_cli()
        os.environ["ANONA_API_KEY"] = "anona_live_FROMENVxxxxxxxxxxxxxxxx"
        rc, out, err = run_main(["status"])
        assert rc == 0, (rc, out)
        assert "not by the data commands while an API key is set" in out, out
        assert "used by `anona mcp`" in out, out
        assert "No `anona login` credential is stored for MCP" not in out, out
        assert "FROMENVxxxx" not in out
        print("OK")
    """, tmp_path))


def test_start_refuses_beside_a_cli_login(tmp_path):
    ok(_run("""
        seed_cli()
        rc, out, err = run_main(["start", "--base-url", BASE])
        assert rc == 1 and "already signed in" in err, (rc, err)
        assert DATA == []
        print("OK")
    """, tmp_path))


# --- which credential `anona mcp` presents ------------------------------------


def test_anona_mcp_presents_the_command_line_login(tmp_path):
    ok(_run("""
        c = seed_cli()
        rc, out, err = mcp_call()
        assert rc == 0 and json.loads(out)["result"] == {"tools": []}, (out, err)
        assert MCPS == [c["access_token"]], MCPS
        print("OK")
    """, tmp_path))


def test_the_command_line_login_beats_an_mcp_login_and_a_temp_profile(tmp_path):
    ok(_run("""
        seed_mcp(temp=True)
        c = seed_cli()
        rc, out, err = mcp_call()
        assert MCPS == [c["access_token"]], MCPS
        assert "temporary profile" not in err, err
        print("OK")
    """, tmp_path))


def test_a_command_line_login_for_another_deployment_is_never_sent(tmp_path):
    ok(_run("""
        m = seed_mcp()
        seed_cli(base="https://other.example")
        rc, out, err = mcp_call()
        assert MCPS == [m["access_token"]], MCPS
        print("OK")
    """, tmp_path))


def test_the_mcp_only_login_still_works_alone(tmp_path):
    ok(_run("""
        m = seed_mcp()
        rc, out, err = mcp_call()
        assert MCPS == [m["access_token"]], MCPS
        print("OK")
    """, tmp_path))


def test_a_refused_command_line_token_is_renewed_in_its_own_file(tmp_path):
    ok(_run("""
        m = seed_mcp()
        c = seed_cli()
        DEAD.add(c["access_token"])
        rc, out, err = mcp_call()
        assert json.loads(out)["result"] == {"tools": []}, (out, err)
        assert len(MCPS) == 2 and MCPS[0] == c["access_token"], MCPS
        assert typ_of(MCPS[1]) == "cli_access", MCPS
        assert doc(CLI_FILE)["access_token"] == MCPS[1]
        assert doc(MCP_FILE)["refresh_token"] == m["refresh_token"]
        print("OK")
    """, tmp_path))


def test_anona_mcp_follows_the_command_line_login_deployment(tmp_path):
    ok(_run("""
        c = seed_cli()
        sys.stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\\n")
        rc, out, err = run_main(["mcp"])
        assert rc == 0, (rc, out, err)
        assert MCPS == [c["access_token"]], (MCPS, err)
        print("OK")
    """, tmp_path))
