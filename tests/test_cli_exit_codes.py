"""ANO-428: `anona` exits with a code that says which kind of thing happened.

Agents script on exit codes and parse stderr, so "it failed" is not an answer:
a refused connection, an input the server will reject, an expired credential
and a refusal to act all used to be indistinguishable, and three of them
exited 0.
"""
from __future__ import annotations

import textwrap

from _cli import ok, run_cli

PRELUDE = """
import io, json, sys, time
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from anona.cli import main as cli_main

def creds(base="https://memory.anonalabs.com", temp_offset=3600, signed_in=False):
    d = Path.home() / ".anona"
    d.mkdir(parents=True, exist_ok=True)
    (d / "credentials.json").write_text(json.dumps({
        "access_token": "oauth_A" if signed_in else "",
        "refresh_token": "r" if signed_in else "",
        "expires_at": time.time() + 3600 if signed_in else 0,
        "client_id": "c" if signed_in else "",
        "base_url": base,
        "temp_token": "anona_tmp_T",
        "temp_expires_at": time.time() + temp_offset,
    }))

def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = cli_main.main(argv)
        except SystemExit as exc:
            rc = exc.code
    return rc, out.getvalue(), err.getvalue()
"""


def _run(body, tmp_path, timeout=30):
    return run_cli(PRELUDE + textwrap.dedent(body), env={"HOME": str(tmp_path)}, timeout=timeout)


def test_version_is_a_successful_answer_on_stdout(tmp_path):
    # It did not exist: argparse called it a usage error and exited 2, while
    # the docs tell people to check they are on 0.20.0 or later.
    ok(_run("""
        rc, out, err = run_main(["--version"])
        assert rc == 0, (rc, err)
        assert out.startswith("anona "), out
        assert err == "", err
        print("OK")
    """, tmp_path))


def test_version_reports_the_code_that_is_running(tmp_path):
    # NOT importlib.metadata, which answers for the installed distribution --
    # a different question. Measured during ANO-428: a 0.20.4 checkout
    # printed "anona 0.13.1" off an unrelated installed copy, which is the
    # worst possible answer from a flag whose job is to say what you have.
    ok(_run("""
        import anona
        rc, out, err = run_main(["--version"])
        assert out.strip() == f"anona {anona.__version__}", (out, anona.__version__)
        print("OK")
    """, tmp_path))


def test_help_is_a_successful_answer(tmp_path):
    ok(_run("""
        rc, out, err = run_main(["--help"])
        assert rc == 0, rc
        assert "usage: anona" in out, out
        print("OK")
    """, tmp_path))


def test_status_refuses_a_base_url_that_is_not_the_stored_one(tmp_path):
    # It was read and ignored, so `status --base-url https://other` described
    # the stored deployment while naming another -- the one shape of wrong
    # answer a status command must never give.
    ok(_run("""
        creds(base="https://memory.anonalabs.com")
        rc, out, err = run_main(["status", "--base-url", "https://other.example"])
        assert rc == 1, (rc, out)
        assert "other.example" in err, err
        assert "memory.anonalabs.com" in err, err
        assert out == "", "it must not also describe the stored deployment: %r" % out
        print("OK")
    """, tmp_path))


def test_status_accepts_the_base_url_it_is_already_on(tmp_path):
    # Naming the deployment you are on is not a mismatch.
    ok(_run("""
        creds(base="https://memory.anonalabs.com")
        rc, out, err = run_main(["status", "--base-url", "https://memory.anonalabs.com"])
        assert rc == 0, (rc, err)
        assert "Temporary profile" in out, out
        print("OK")
    """, tmp_path))


def test_an_expired_temp_profile_is_not_success(tmp_path):
    # The profile and everything in it are gone. Exit 0 is how a scripted
    # `anona status && anona record ...` walked into a write with no
    # credential behind it.
    ok(_run("""
        creds(temp_offset=-3600)
        rc, out, err = run_main(["status"])
        assert rc == 1, (rc, out)
        assert "expired" in out, out
        print("OK")
    """, tmp_path))


def test_a_live_temp_profile_is_still_success(tmp_path):
    ok(_run("""
        creds(temp_offset=3600)
        rc, out, err = run_main(["status"])
        assert rc == 0, (rc, out)
        assert "claim it before then" in out, out
        print("OK")
    """, tmp_path))


def test_an_expired_profile_beside_a_login_is_still_success(tmp_path):
    # Expired is only fatal when there is nothing to fall back on.
    ok(_run("""
        creds(temp_offset=-3600, signed_in=True)
        rc, out, err = run_main(["status"])
        assert rc == 0, (rc, out)
        assert "Credential found" in out, out
        print("OK")
    """, tmp_path))


def test_the_space_flag_does_not_claim_to_be_required_when_signed_in(tmp_path):
    # It said "required when signed in", which is backwards: the verbs refuse
    # a signed-in credential outright, so the one state in which it was
    # described as required is the state in which they do not run at all.
    ok(_run("""
        rc, out, err = run_main(["record", "--help"])
        assert rc == 0, rc
        assert "required when signed in" not in out, out
        # The states that need a space are an API key and the command-line
        # login (both reach an account); a temporary profile still has its
        # one space. Help text wraps.
        flat = " ".join(out.split())
        assert "Required with an API key or `anona login` unless ANONA_SPACE_ID is set" in flat, flat
        assert "a temporary profile uses `default`" in flat, flat
        print("OK")
    """, tmp_path))


def test_start_refusing_to_create_a_second_profile_is_not_success(tmp_path):
    # Nothing was created. A script running `anona start` to guarantee itself
    # a fresh profile got exit 0 and a profile left over from an earlier run.
    ok(_run("""
        creds(temp_offset=3600)
        rc, out, err = run_main(["start"])
        assert rc == 1, (rc, out)
        assert "already exists" in out, out
        print("OK")
    """, tmp_path))


def test_start_is_still_success_when_it_creates_one(tmp_path):
    # The guard above must not turn the ordinary path non-zero.
    ok(_run("""
        import json, threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.dumps({"temp_token": "anona_tmp_NEW",
                                   "expires_at": "2099-01-01T00:00:00Z"}).encode()
                self.send_response(201)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *a): pass
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        rc, out, err = run_main(["start", "--base-url", base])
        assert rc == 0, (rc, err)
        assert "Temporary profile created" in out, out
        print("OK")
    """, tmp_path))
