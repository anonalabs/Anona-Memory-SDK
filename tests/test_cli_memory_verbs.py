"""Three verbs for people who do not want MCP, and for the pre-restart session.

Each case runs a local HTTP server and points the CLI at it, so what is
asserted is the request that actually left the process: which bearer, which
space. Runs in a subprocess (see _cli.py) so `import anona` is the public SDK.
"""
from __future__ import annotations

import textwrap

from _cli import ok, run_cli

PRELUDE = """
import io, json, sys, time, threading
from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from anona.cli import main as cli_main

SEEN = []

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])) or b"{}")
        SEEN.append((self.path, self.headers.get("authorization"), body))
        if self.path.endswith("reason"):
            out = {"insights": "First paragraph.\\n\\nSecond paragraph."}
        elif self.path.endswith("retrieve"):
            out = {"results": [{"content": "the user prefers pnpm"}]}
        else:
            out = {"id": "m1", "status": "ok"}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *a): pass

srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % srv.server_address[1]

def creds(temp_hours=None, signed_in=False):
    d = Path.home() / ".anona"
    d.mkdir(parents=True, exist_ok=True)
    c = {
        "access_token": "oauth_ACCESS" if signed_in else "",
        "refresh_token": "r" if signed_in else "",
        "expires_at": time.time() + 3600 if signed_in else 0,
        "client_id": "c" if signed_in else "",
        "base_url": BASE,
    }
    if temp_hours is not None:
        c["temp_token"] = "anona_tmp_TESTTOKEN"
        c["temp_expires_at"] = time.time() + temp_hours * 3600
    (d / "credentials.json").write_text(json.dumps(c))

def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli_main.main(argv)
    return rc, out.getvalue(), err.getvalue()
"""


def _run(body, tmp_path, timeout=30):
    return run_cli(PRELUDE + textwrap.dedent(body), env={"HOME": str(tmp_path)}, timeout=timeout)


def test_record_with_no_credential_says_what_to_do(tmp_path):
    ok(_run("""
        rc, out, err = run_main(["record", "the user prefers pnpm"])
        assert rc == 1, rc
        assert "anona start" in err, err
        assert SEEN == [], SEEN
        print("OK")
    """, tmp_path))


def test_verbs_are_known_and_have_help(tmp_path):
    ok(_run("""
        for verb in ("record", "retrieve", "reason"):
            try:
                run_main([verb, "--help"])
            except SystemExit as e:
                assert e.code == 0, e.code
            else:
                raise AssertionError("--help should exit")
        print("OK")
    """, tmp_path))


def test_temp_profile_record_defaults_to_the_default_space(tmp_path):
    ok(_run("""
        creds(temp_hours=60)
        rc, out, err = run_main(["record", "the user prefers pnpm"])
        assert rc == 0, (rc, err)
        path, auth, body = SEEN[-1]
        assert path == "/v1/record", path
        assert auth == "Bearer anona_tmp_TESTTOKEN", auth
        assert body["space_id"] == "default", body
        assert "default" in out, out
        print("OK")
    """, tmp_path))


def test_temp_profile_retrieve_prints_results(tmp_path):
    ok(_run("""
        creds(temp_hours=60)
        rc, out, err = run_main(["retrieve", "package manager"])
        assert rc == 0, (rc, err)
        path, auth, body = SEEN[-1]
        assert body["space_id"] == "default", body
        assert body["query"] == "package manager", body
        assert "1. the user prefers pnpm" in out, out
        print("OK")
    """, tmp_path))


def test_signed_in_account_must_name_a_space(tmp_path):
    ok(_run("""
        creds(signed_in=True)
        for argv in (["record", "x"], ["retrieve", "x"], ["reason", "x"]):
            rc, out, err = run_main(argv)
            assert rc == 1, (argv, rc)
            assert "--space" in err, err
        assert SEEN == [], "nothing may be sent without a space: %r" % SEEN
        print("OK")
    """, tmp_path))


def test_signed_in_account_with_space_uses_that_space(tmp_path):
    ok(_run("""
        creds(signed_in=True)
        rc, out, err = run_main(["record", "x", "--space", "work"])
        assert rc == 0, (rc, err)
        path, auth, body = SEEN[-1]
        assert body["space_id"] == "work", body
        assert auth == "Bearer oauth_ACCESS", auth
        print("OK")
    """, tmp_path))


def test_oauth_credential_beats_a_leftover_temp_token_and_needs_a_space(tmp_path):
    # Signed in AND still holding a live temp token: the account wins, so the
    # temp profile's "default" must not be assumed.
    ok(_run("""
        creds(temp_hours=60, signed_in=True)
        rc, out, err = run_main(["record", "x"])
        assert rc == 1, rc
        assert "--space" in err, err
        rc, out, err = run_main(["record", "x", "--space", "work"])
        assert rc == 0, (rc, err)
        assert SEEN[-1][1] == "Bearer oauth_ACCESS", SEEN[-1][1]
        print("OK")
    """, tmp_path))


def test_expired_temp_profile_is_a_message_not_a_traceback(tmp_path):
    ok(_run("""
        creds(temp_hours=-1)
        rc, out, err = run_main(["record", "x"])
        assert rc == 1, rc
        assert "expired" in err, err
        assert SEEN == [], SEEN
        print("OK")
    """, tmp_path))


def test_server_error_is_a_message_not_a_traceback(tmp_path):
    ok(_run("""
        creds(temp_hours=60)
        srv.shutdown(); srv.server_close()
        rc, out, err = run_main(["record", "x"])
        assert rc == 1, rc
        assert "anona:" in err and "HTTP 503" in err, err
        assert "Traceback" not in err, err
        print("OK")
    """, tmp_path))


def test_retrieve_strips_terminal_escapes_from_memory_content(tmp_path):
    # Memory content is attacker-influenceable. Raw ESC would let it erase and
    # forge CLI output.
    ok(_run("""
        creds(temp_hours=60)
        EVIL = "benign\\x1b[2K\\x1b[1Gspoofed: verified OK\\x9b31m\\r\\nline2\\x07"
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])) or b"{}")
            data = json.dumps({"results": [{"content": EVIL}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        H.do_POST = do_POST
        rc, out, err = run_main(["retrieve", "q"])
        assert rc == 0, (rc, err)
        for ch in ("\\x1b", "\\x9b", "\\r", "\\x07"):
            assert ch not in out, repr(out)
        assert "spoofed: verified OK" in out, repr(out)
        assert out.count("\\n") == 1, repr(out)
        print("OK")
    """, tmp_path))


def test_error_detail_is_stripped_of_terminal_escapes(tmp_path):
    ok(_run("""
        creds(temp_hours=60)
        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            # Not JSON: the SDK keeps resp.text as `detail`, a str. (A JSON
            # dict detail is str()'d via repr, which already escapes ESC.)
            data = "no space \\x1b[2Kforged".encode()
            self.send_response(404)
            self.send_header("content-type", "text/plain")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        H.do_POST = do_POST
        rc, out, err = run_main(["record", "x"])
        assert rc == 1, rc
        assert "\\x1b" not in err, repr(err)
        assert "forged" in err, repr(err)
        print("OK")
    """, tmp_path))


def test_temp_profile_reason_defaults_to_the_default_space(tmp_path):
    ok(_run("""
        creds(temp_hours=60)
        rc, out, err = run_main(["reason", "what does the user prefer"])
        assert rc == 0, (rc, err)
        path, auth, body = SEEN[-1]
        assert path == "/v1/reason", path
        assert auth == "Bearer anona_tmp_TESTTOKEN", auth
        assert body["space_id"] == "default", body
        assert body["query"] == "what does the user prefer", body
        print("OK")
    """, tmp_path))


def test_reason_keeps_paragraph_breaks_in_the_answer(tmp_path):
    # The difference from `retrieve`, which deliberately flattens each memory
    # to one line. An answer is prose; flattening it runs the paragraphs
    # together, so reusing retrieve's sanitizer would silently wreck this.
    ok(_run("""
        creds(temp_hours=60)
        rc, out, err = run_main(["reason", "q"])
        assert rc == 0, (rc, err)
        assert "First paragraph.\\n\\nSecond paragraph." in out, repr(out)
        print("OK")
    """, tmp_path))


def test_reason_answer_goes_to_stdout_and_the_progress_note_to_stderr(tmp_path):
    # `anona reason q > answer.md` must capture the answer and nothing else.
    ok(_run("""
        creds(temp_hours=60)
        rc, out, err = run_main(["reason", "q"])
        assert rc == 0, (rc, err)
        assert "First paragraph." in out, repr(out)
        assert "Reasoning over space 'default'" in err, repr(err)
        assert "Reasoning over" not in out, repr(out)
        print("OK")
    """, tmp_path))


def test_reason_strips_escapes_and_carriage_returns_but_not_newlines(tmp_path):
    # Same threat as retrieve: the answer is synthesized from recorded content,
    # so an injected sequence rides through the model. A bare CR is the one to
    # watch here -- it is legal inside a block that keeps newlines, and on its
    # own it returns the cursor to column 0 so later text overwrites the line
    # already printed, which is most of what an escape sequence would buy.
    ok(_run("""
        creds(temp_hours=60)
        EVIL = "real answer\\x1b[2K\\x1b[1G\\rspoofed: verified OK\\x9b31m\\x07\\n\\nkept"
        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            data = json.dumps({"insights": EVIL}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        H.do_POST = do_POST
        rc, out, err = run_main(["reason", "q"])
        assert rc == 0, (rc, err)
        for ch in ("\\x1b", "\\x9b", "\\r", "\\x07"):
            assert ch not in out, (repr(ch), repr(out))
        assert "spoofed: verified OK" in out, repr(out)
        # The newlines the answer legitimately carried are still there.
        assert "\\n\\nkept" in out, repr(out)
        print("OK")
    """, tmp_path))


def test_reason_with_no_answer_is_not_an_error(tmp_path):
    # The call succeeded; the space just had nothing to answer from. Must not
    # print a blank line and must not look like a failure.
    ok(_run("""
        creds(temp_hours=60)
        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            data = json.dumps({"insights": "   "}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        H.do_POST = do_POST
        rc, out, err = run_main(["reason", "q"])
        assert rc == 0, (rc, err)
        assert "No answer" in out, repr(out)
        print("OK")
    """, tmp_path))


def test_reason_server_error_is_a_message_not_a_traceback(tmp_path):
    ok(_run("""
        creds(temp_hours=60)
        srv.shutdown(); srv.server_close()
        rc, out, err = run_main(["reason", "x"])
        assert rc == 1, rc
        assert "anona:" in err and "HTTP 503" in err, err
        assert "Traceback" not in err, err
        print("OK")
    """, tmp_path))
