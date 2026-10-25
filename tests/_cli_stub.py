"""A recording stub gateway for the CLI tests, serving real gateway bodies.

PRELUDE is prepended to each subprocess snippet (see _cli.py for why every
case runs in a subprocess). It starts a local HTTP server that records every
request -- method, path, query, decoded body, auth header -- and answers from
ROUTES, a list of (method, path regex, responder) the case can change. Bodies
come from BODIES, the snapshot of the gateway's own models
(_cli_gateway_bodies.py); a case never invents one.
"""
from __future__ import annotations

import json
import textwrap

import _cli_gateway_bodies
from _cli import run_cli

_PRELUDE = r"""
import io, json, os, re, sys, threading, time
from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl
from anona.cli import main as cli_main

for _name in ("ANONA_BASE_URL", "ANONA_API_KEY", "ANONA_SPACE_ID", "ANONA_PROFILE"):
    os.environ.pop(_name, None)

BODIES = json.loads(BODIES_JSON)
SEEN = []
KEY = "anona_live_SECRETSECRETSECRET"

def reply(status, body=None, headers=None):
    return (status, body, headers or {})

def envelope(status, code, message="m", request_id="req_1"):
    return reply(status, {"error": {"code": code, "message": message, "request_id": request_id}},
                 {"retry-after": "0"} if status == 429 or status >= 500 else {})

ROUTES = []  # (method, path regex, callable(req) -> (status, body, headers))

def route(method, pattern, responder):
    # Newest first, so a case can override a default.
    ROUTES.insert(0, (method, re.compile(pattern), responder))

class H(BaseHTTPRequestHandler):
    def _any(self):
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        parts = urlsplit(self.path)
        ctype = self.headers.get("content-type") or ""
        body = None
        if raw and ctype.startswith("application/json"):
            body = json.loads(raw)
        elif raw:
            body = raw
        req = {"method": self.command, "path": parts.path, "query": dict(parse_qsl(parts.query)),
               "body": body, "auth": self.headers.get("authorization"), "ctype": ctype}
        SEEN.append(req)
        for method, rx, responder in ROUTES:
            if method == self.command and rx.fullmatch(parts.path):
                status, out, headers = responder(req)
                break
        else:
            status, out, headers = 404, {"error": {"code": "not_found", "message": "No route."}}, {}
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        if out is None:
            self.send_header("content-length", "0")
            self.end_headers()
            return
        data = json.dumps(out).encode()
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _any
    def log_message(self, *a): pass

srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % srv.server_address[1]
KEYED = ["--base-url", BASE, "--api-key", KEY]
AUTH = ["--space", "s"] + KEYED
ANONA = Path.home() / ".anona"

def run_main(argv, stdin=None):
    out, err = io.StringIO(), io.StringIO()
    old_stdin = sys.stdin
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = cli_main.main(argv)
            except SystemExit as exc:
                rc = exc.code
    finally:
        sys.stdin = old_stdin
    o, e = out.getvalue(), err.getvalue()
    assert KEY not in o + e and "SECRETSECRET" not in o + e, ("key leaked", argv, o, e)
    return rc, o, e

def sent(argv, want_rc=0, n_requests=1, stdin=None):
    n = len(SEEN)
    rc, out, err = run_main(argv, stdin=stdin)
    assert rc == want_rc, (argv, rc, out, err)
    assert len(SEEN) == n + n_requests, (argv, [ (r["method"], r["path"]) for r in SEEN[n:]])
    for r in SEEN[n:]:
        assert r["auth"] == "Bearer " + KEY, ("the credential was not sent", r)
    return SEEN[n:], out, err

def refused(argv, want=2, stdin=None):
    n = len(SEEN)
    rc, out, err = run_main(argv, stdin=stdin)
    assert rc == want, (argv, rc, out, err)
    assert len(SEEN) == n, ("sent", argv, SEEN[n:])
    assert out == "", (argv, out)
    assert err.startswith("anona") or "usage:" in err, (argv, err)
    return err

def one_json(out):
    assert out.endswith("\n") and out.count("\n") == 1, repr(out)
    return json.loads(out)
"""


def prelude() -> str:
    bodies = json.dumps(_cli_gateway_bodies.load())
    return f"BODIES_JSON = {bodies!r}\n" + _PRELUDE


def run(body: str, tmp_path, timeout: int = 60, env: dict | None = None):
    return run_cli(prelude() + textwrap.dedent(body), env={"HOME": str(tmp_path), **(env or {})}, timeout=timeout)
