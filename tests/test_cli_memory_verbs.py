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
            # The real /v1/record body. The stub used to answer
            # {"id": "m1", "status": "ok"} -- a shape the gateway never sends,
            # which is how every record case here passed while the verb was
            # unable to tell a stored write from an empty one.
            out = {"memory_id": "m1", "memory_ids": ["m1"], "status": "stored"}
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


def test_record_reports_what_was_stored_and_names_the_ids(tmp_path):
    # The ids are the only thing the CLI knows and the caller does not, and
    # they are what every other surface needs to read, edit or delete the
    # memory. One item routinely extracts to more than one memory, so the
    # count carries information too.
    ok(_run("""
        creds(temp_hours=60)
        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            data = json.dumps({"memory_id": "m1",
                               "memory_ids": ["m1", "m2"],
                               "status": "stored"}).encode()
            self.send_response(201)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        H.do_POST = do_POST
        rc, out, err = run_main(["record", "the user prefers pnpm"])
        assert rc == 0, (rc, err)
        assert "Stored 2 memories in space 'default'" in out, out
        assert "m1" in out and "m2" in out, out
        print("OK")
    """, tmp_path))


def test_record_that_stored_nothing_is_not_reported_as_success(tmp_path):
    # A 201 is acceptance, not storage: extraction runs over the content and
    # short, context-free statements often produce nothing. The verb printed
    # "Stored" and exited 0 for those, so an agent was told a fact had been
    # kept when it had not. Exit code AND stderr, because a shell script reads
    # one and a person reads the other.
    ok(_run("""
        creds(temp_hours=60)
        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            data = json.dumps({"memory_id": None, "memory_ids": None,
                               "status": "nothing_extracted",
                               "message": "No facts could be extracted from that "
                                          "content, so nothing was stored in space "
                                          "'default'. Add a sentence of context."}).encode()
            self.send_response(201)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        H.do_POST = do_POST
        rc, out, err = run_main(["record", "Thanks!"])
        assert rc != 0, "an empty write must not exit 0"
        assert rc == 3, rc
        assert "Stored" not in out, out
        # The SERVER's message, verbatim -- not a copy of the wording kept here.
        assert "No facts could be extracted" in err, err
        assert "Add a sentence of context." in err, err
        print("OK")
    """, tmp_path))


def test_an_older_server_without_the_status_is_still_caught(tmp_path):
    # The honest `status`/`message` pair is itself a fix that has to be
    # deployed. Until it is everywhere, an empty write arrives as
    # `status: "stored"` with no ids and no message -- which is the bug this
    # whole verb guards against, so the guard cannot depend on the server-side
    # fix being live. The ids decide, and the wording falls back to ours.
    ok(_run("""
        creds(temp_hours=60)
        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            data = json.dumps({"memory_id": None, "memory_ids": None,
                               "status": "stored"}).encode()
            self.send_response(201)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        H.do_POST = do_POST
        rc, out, err = run_main(["record", "Thanks!"])
        assert rc == 3, rc
        assert "Stored" not in out, out
        assert "Nothing was stored in space 'default'" in err, err
        assert "sentence of context" in err, err
        print("OK")
    """, tmp_path))


def test_the_servers_message_is_stripped_of_terminal_escapes(tmp_path):
    # `message` is server text printed to a terminal, so it goes through the
    # same sanitizer as memory content and ids.
    ok(_run("""
        creds(temp_hours=60)
        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            evil = "nothing stored\x1b[2K\x1b[1Gspoofed: verified OK"
            data = json.dumps({"status": "nothing_extracted",
                               "message": evil}).encode()
            self.send_response(201)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        H.do_POST = do_POST
        rc, out, err = run_main(["record", "x"])
        assert rc == 3, rc
        assert "\x1b" not in err, repr(err)
        assert "spoofed: verified OK" in err, repr(err)
        print("OK")
    """, tmp_path))


def test_a_queued_write_is_not_reported_as_empty(tmp_path):
    # A write accepted for background extraction has no ids yet. That is a
    # third outcome, and calling it empty would be the same bug pointing the
    # other way.
    ok(_run("""
        creds(temp_hours=60)
        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            data = json.dumps({"job_id": "j1", "status": "processing"}).encode()
            self.send_response(201)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        H.do_POST = do_POST
        rc, out, err = run_main(["record", "x"])
        assert rc == 0, (rc, err)
        assert "Nothing was stored" not in err, err
        assert "Queued in space 'default'" in out, out
        print("OK")
    """, tmp_path))


def test_record_strips_terminal_escapes_from_returned_ids(tmp_path):
    # An id is server text printed to a terminal, so it goes through the same
    # sanitizer as memory content.
    ok(_run("""
        creds(temp_hours=60)
        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            evil = "m1\x1b[2K\x1b[1Gspoofed: verified OK"
            data = json.dumps({"memory_ids": [evil], "status": "stored"}).encode()
            self.send_response(201)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        H.do_POST = do_POST
        rc, out, err = run_main(["record", "x"])
        assert rc == 0, (rc, err)
        assert "\x1b" not in out, repr(out)
        assert "spoofed: verified OK" in out, repr(out)
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
        # No id or score in this stub row, so each prints as "-".
        assert "1. -  -  the user prefers pnpm" in out, out
        print("OK")
    """, tmp_path))


def test_a_signed_in_account_is_refused_before_the_request(tmp_path):
    # `anona login` mints an MCP-scoped OAuth token and the data plane rejects
    # any token carrying a `typ`, so these verbs can only ever get
    # `401 invalid_token` on an account -- a message that reads as a broken
    # login and sends someone back to `anona login` for the same token again.
    # The point of this test is `SEEN == []`: the refusal must come BEFORE the
    # request, because the server's own answer is the misleading one.
    ok(_run("""
        creds(signed_in=True)
        for argv in (["record", "x"], ["retrieve", "x"], ["reason", "x"],
                     ["record", "x", "--space", "work"]):
            rc, out, err = run_main(argv)
            assert rc == 1, (argv, rc)
            assert "temporary profile" in err, (argv, err)
            # It must name both ways an account DOES reach memory, or the
            # message is a refusal with nowhere to go.
            assert "anona mcp" in err and "API key" in err, (argv, err)
        assert SEEN == [], "nothing may be sent on an account credential: %r" % SEEN
        print("OK")
    """, tmp_path))


def test_naming_a_space_does_not_buy_past_the_refusal(tmp_path):
    # --space used to be the thing an account was missing, so the obvious
    # reading of the old message was "pass --space and it works". It does not,
    # and must not look like it might.
    ok(_run("""
        creds(signed_in=True)
        rc, out, err = run_main(["record", "x", "--space", "work"])
        assert rc == 1, rc
        assert "--space" not in err, "must not suggest --space is the fix: %r" % err
        assert SEEN == [], SEEN
        print("OK")
    """, tmp_path))


def test_a_login_beside_a_live_temp_token_is_still_a_login(tmp_path):
    # Signed in AND still holding a live temp token. `_bearer` prefers the
    # account, so the temp profile's "default" must not be silently used --
    # that would write to a space the credential cannot even reach.
    ok(_run("""
        creds(temp_hours=60, signed_in=True)
        rc, out, err = run_main(["record", "x"])
        assert rc == 1, rc
        assert "temporary profile" in err, err
        assert SEEN == [], SEEN
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


# --- retrieve is usable by an agent ---------------------------------------

_ROWS_SERVER = """
ROWS = [
    {"memory_id": "3f2a9c1e-0000-4000-8000-000000000001", "content": "the user prefers pnpm",
     "relevance_score": 1.0978, "created_at": "2026-10-19T10:00:00Z", "timestamp": None,
     "user_id": "alice", "agent_id": None, "session_id": None, "memory_type": "world"},
    {"memory_id": "9b7d0e44-0000-4000-8000-000000000002", "content": "builds run on Node 22",
     "relevance_score": 0.00006, "created_at": "2026-10-19T10:01:00Z", "timestamp": None,
     "user_id": "alice", "agent_id": None, "session_id": None, "memory_type": "observation"},
]
EXTRA = {}
def do_POST(self):
    body = json.loads(self.rfile.read(int(self.headers["content-length"])) or b"{}")
    SEEN.append((self.path, self.headers.get("authorization"), body))
    data = json.dumps({"results": ROWS, **EXTRA}).encode()
    self.send_response(200)
    self.send_header("content-type", "application/json")
    self.send_header("content-length", str(len(data)))
    self.end_headers()
    self.wfile.write(data)
H.do_POST = do_POST
"""


def _rows(body):
    return _ROWS_SERVER + textwrap.dedent(body)


def test_retrieve_flags_pass_straight_through(tmp_path):
    ok(_run(_rows("""
        creds(temp_hours=60)
        rc, out, err = run_main(["retrieve", "pm", "--limit", "3", "--min-score", "0.1",
                                 "--user-id", "alice", "--agent-id", "coder", "--session-id", "s1"])
        assert rc == 0, (rc, err)
        body = SEEN[-1][2]
        assert (body["limit"], body["min_score"]) == (3, 0.1), body
        assert (body["user_id"], body["agent_id"], body["session_id"]) == ("alice", "coder", "s1"), body
        print("OK")
    """), tmp_path))


def test_retrieve_without_flags_sends_no_floor_and_no_scope(tmp_path):
    ok(_run(_rows("""
        creds(temp_hours=60)
        rc, _, err = run_main(["retrieve", "pm"])
        assert rc == 0, (rc, err)
        body = SEEN[-1][2]
        for k in ("min_score", "user_id", "agent_id", "session_id"):
            assert k not in body, (k, body)
        print("OK")
    """), tmp_path))


def test_retrieve_json_is_the_api_rows_and_nothing_else_on_stdout(tmp_path):
    ok(_run(_rows("""
        creds(temp_hours=60)
        rc, out, err = run_main(["retrieve", "pm", "--json"])
        assert rc == 0, (rc, err)
        doc = json.loads(out)  # one document: anything else on stdout fails here
        assert doc == {"results": ROWS}, doc
        print("OK")
    """), tmp_path))


def test_retrieve_json_carries_searched_on_a_routed_read(tmp_path):
    ok(_run(_rows("""
        creds(temp_hours=60)
        EXTRA["searched"] = [{"space_id": "default", "confidence": 0.9, "stage": "rule", "reason": "r"}]
        rc, out, err = run_main(["retrieve", "pm", "--json"])
        assert rc == 0, (rc, err)
        assert json.loads(out)["searched"] == EXTRA["searched"], out
        print("OK")
    """), tmp_path))


def test_retrieve_json_with_no_match_is_still_a_document(tmp_path):
    ok(_run(_rows("""
        creds(temp_hours=60)
        ROWS.clear()
        rc, out, err = run_main(["retrieve", "pm", "--json"])
        assert rc == 0, (rc, err)
        assert json.loads(out) == {"results": []}, out
        print("OK")
    """), tmp_path))


def test_retrieve_json_cannot_carry_a_terminal_escape(tmp_path):
    ok(_run(_rows("""
        creds(temp_hours=60)
        ROWS[0]["content"] = "benign\\x1b[2Kspoofed\\x9b31m\\r\\n"
        rc, out, err = run_main(["retrieve", "pm", "--json"])
        assert rc == 0, (rc, err)
        for ch in ("\\x1b", "\\x9b", "\\r"):
            assert ch not in out, repr(out)
        # Escaped, not dropped: the document still round-trips to the real content.
        assert json.loads(out)["results"][0]["content"] == ROWS[0]["content"]
        print("OK")
    """), tmp_path))


def test_retrieve_text_shows_score_and_short_id(tmp_path):
    ok(_run(_rows("""
        creds(temp_hours=60)
        rc, out, err = run_main(["retrieve", "pm"])
        assert rc == 0, (rc, err)
        lines = out.splitlines()
        assert lines[0] == "1. 1.1  3f2a9c1e  the user prefers pnpm", lines
        assert lines[1] == "2. 6e-05  9b7d0e44  builds run on Node 22", lines
        print("OK")
    """), tmp_path))


def test_retrieve_rejects_a_bad_limit_or_score_as_usage(tmp_path):
    ok(_run(_rows("""
        creds(temp_hours=60)
        for argv in (["--limit", "0"], ["--limit", "101"], ["--limit", "x"], ["--min-score", "-1"], ["--min-score", "nan"]):
            try:
                rc, out, err = run_main(["retrieve", "pm"] + argv)
            except SystemExit as e:
                rc = e.code
            assert rc == 2, (argv, rc)
        assert SEEN == [], SEEN
        print("OK")
    """), tmp_path))
