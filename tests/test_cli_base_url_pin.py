"""A stored credential only ever goes to the deployment that issued it.

`--base-url` and ANONA_BASE_URL used to re-target the stored token at any host,
over plain HTTP, with no warning: one flag typed by an agent -- or injected into
its instructions by a page it read -- handed the token to whoever ran the
server. Each case runs a local HTTP server and asserts on what actually reached
it, so a refusal that still sent the request fails here. Runs in a subprocess
(see _cli.py) so `import anona` is the public SDK.
"""
from __future__ import annotations

import textwrap

from _cli import ok, run_cli

PRELUDE = """
import io, json, os, sys, time, threading
from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from anona.cli import main as cli_main, target

os.environ.pop("ANONA_BASE_URL", None)
SEEN = []

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        SEEN.append((self.path, self.headers.get("authorization")))
        data = json.dumps({"memory_ids": ["m1"], "status": "ok", "results": []}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *a): pass

srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % srv.server_address[1]

def temp_profile(base):
    d = Path.home() / ".anona"
    d.mkdir(parents=True, exist_ok=True)
    (d / "credentials.json").write_text(json.dumps({
        "access_token": "", "refresh_token": "", "expires_at": 0, "client_id": "",
        "base_url": base,
        "temp_token": "anona_tmp_SECRET", "temp_expires_at": time.time() + 3600,
    }))

def run_main(argv, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    old = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli_main.main(argv)
    finally:
        sys.stdin = old
    return rc, out.getvalue(), err.getvalue()

TOOLS_LIST = '{"jsonrpc":"2.0","id":1,"method":"tools/list"}\\n'
"""


def _run(body, tmp_path):
    return run_cli(PRELUDE + textwrap.dedent(body), env={"HOME": str(tmp_path)})


def test_data_verbs_refuse_a_base_url_the_credential_was_not_issued_for(tmp_path):
    ok(_run("""
        temp_profile("https://memory.anonalabs.com")
        for argv in (["record", "x"], ["retrieve", "x"], ["reason", "x"]):
            rc, out, err = run_main(argv + ["--base-url", BASE])
            assert rc == 4, (argv, rc, out, err)  # an auth failure exits 4
            assert "the stored credential is for https://memory.anonalabs.com, not " + BASE in err, err
            assert "anona login --base-url " + BASE in err, err
        assert SEEN == [], SEEN
        print("OK")
    """, tmp_path))


def test_mcp_refuses_a_base_url_the_credential_was_not_issued_for(tmp_path):
    ok(_run("""
        temp_profile("https://memory.anonalabs.com")
        for argv in (["mcp", "--base-url", BASE], ["--base-url=" + BASE, "mcp"]):
            rc, out, err = run_main(argv, stdin=TOOLS_LIST)
            assert rc == 1, (argv, rc, out, err)
            assert "the stored credential is for https://memory.anonalabs.com" in err, err
            # Stdout is the MCP client's JSON-RPC stream; nothing else goes there.
            assert out == "", out
        assert SEEN == [], SEEN
        print("OK")
    """, tmp_path))


def test_env_var_is_refused_like_the_flag(tmp_path):
    ok(_run("""
        temp_profile("https://memory.anonalabs.com")
        os.environ["ANONA_BASE_URL"] = BASE
        rc, _, err = run_main(["record", "x"])
        assert rc == 4 and "the stored credential is for" in err, (rc, err)
        rc, _, err = run_main(["mcp"], stdin=TOOLS_LIST)
        assert rc == 1 and "the stored credential is for" in err, (rc, err)
        assert SEEN == [], SEEN
        print("OK")
    """, tmp_path))


def test_naming_the_credentials_own_deployment_still_works(tmp_path):
    # Positive control: the refusal is about a DIFFERENT host, not any flag.
    ok(_run("""
        temp_profile(BASE)
        rc, _, err = run_main(["record", "x", "--base-url", BASE + "/"])
        assert rc == 0, (rc, err)
        os.environ["ANONA_BASE_URL"] = BASE
        rc, _, err = run_main(["retrieve", "x"])
        assert rc == 0, (rc, err)
        assert [p for p, _ in SEEN] == ["/v1/record", "/v1/retrieve"], SEEN
        assert all(a == "Bearer anona_tmp_SECRET" for _, a in SEEN), SEEN
        print("OK")
    """, tmp_path))


def test_plain_http_is_refused_except_on_loopback(tmp_path):
    ok(_run("""
        for url in ("https://memory.anonalabs.com", "https://x.example/", "http://127.0.0.1:3001",
                    "http://localhost:3001", "http://[::1]:3001"):
            target.check_transport(url)
        for url in ("http://memory.anonalabs.com", "http://10.0.0.5:3001",
                    "http://127.0.0.1.evil.example", "http://localhost@evil.example",
                    "ftp://memory.anonalabs.com", "memory.anonalabs.com", "https://"):
            try:
                target.check_transport(url)
            except target.TargetError:
                continue
            raise AssertionError("accepted " + url)
        print("OK")
    """, tmp_path))


def test_a_stored_plain_http_deployment_is_refused_before_any_request(tmp_path):
    ok(_run("""
        temp_profile("http://memory.anonalabs.com")
        rc, _, err = run_main(["record", "x"])
        assert rc == 4 and "https://" in err, (rc, err)
        rc, _, err = run_main(["mcp"], stdin=TOOLS_LIST)
        assert rc == 1 and "https://" in err, (rc, err)
        print("OK")
    """, tmp_path))


def test_start_and_login_refuse_plain_http_to_a_remote_host(tmp_path):
    # Both receive a credential back, so the same rule applies to them.
    ok(_run("""
        for argv in (["start"], ["login", "--no-browser"]):
            rc, _, err = run_main(argv + ["--base-url", "http://memory.anonalabs.com"])
            assert rc == 1 and "https://" in err, (argv, rc, err)
        print("OK")
    """, tmp_path))
