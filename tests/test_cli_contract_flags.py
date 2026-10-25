"""The CLI's request flags and `--json` on record, retrieve and reason.

Every case runs a real local HTTP server and asserts on the exact body that
reached it: a flag that is passed through shows up under its API name, a flag
that is not given is absent from the body (so a bare command sends what it
always did), and a usage error sends nothing at all. `--json` is checked for
the one property a script relies on: stdout is one parseable JSON document and
nothing else, or empty on failure. Runs in a subprocess (see _cli.py) so
`import anona` is the public SDK.
"""
from __future__ import annotations

import textwrap

from _cli import ok, run_cli

PRELUDE = """
import io, json, os, sys, threading
from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, HTTPServer
from anona.cli import main as cli_main

for _name in ("ANONA_BASE_URL", "ANONA_API_KEY", "ANONA_SPACE_ID"):
    os.environ.pop(_name, None)

SEEN = []
MODE = {"status": 200, "record": "stored", "retrieve": "hit"}
KEY = "anona_live_SECRETSECRET"

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        SEEN.append((self.path, body))
        status = MODE["status"]
        if status == 401:
            out = {"error": {"code": "invalid_api_key", "message": "Invalid API key."}}
        elif status >= 400:
            out = {"error": {"code": "internal_error", "message": "Something broke.",
                             "request_id": "req_1"}}
        elif self.path.endswith("reason"):
            out = {"insights": "An answer.\\nSecond line.",
                   "sources": {"models": [], "memories": 2, "notes": 1},
                   "rules_applied": [], "model": "m-x", "usage": {"credits": 3}}
        elif self.path.endswith("retrieve"):
            out = {"results": [{"content": "the user prefers pnpm \\u00e9", "memory_id": "m1"}]
                   if MODE["retrieve"] == "hit" else []}
        elif MODE["record"] == "nothing":
            out = {"memory_id": None, "memory_ids": [], "status": "nothing_extracted",
                   "message": "Nothing was stored."}
        else:
            out = {"memory_id": "m1", "memory_ids": ["m1"], "status": "stored"}
        data = json.dumps(out).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def log_message(self, *a): pass

srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % srv.server_address[1]
AUTH = ["--space", "s", "--base-url", BASE, "--api-key", KEY]

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
    assert KEY not in o + e and "SECRETSECRET" not in o + e, (argv, o, e)
    return rc, o, e

def body(argv, stdin=None):
    n = len(SEEN)
    rc, out, err = run_main(argv + AUTH, stdin)
    assert rc == 0, (argv, rc, out, err)
    assert len(SEEN) == n + 1, (argv, SEEN[n:])
    return SEEN[-1][1]

def refused(argv, stdin=None, want=2):
    n = len(SEEN)
    rc, out, err = run_main(argv + AUTH, stdin)
    assert rc == want, (argv, rc, out, err)
    assert len(SEEN) == n, ("sent", argv, SEEN[n:])
    assert out == "", (argv, out)
    return err

def one_json(out):
    # Exactly one document, and nothing but it, on stdout.
    assert out.endswith("\\n") and out.count("\\n") == 1, repr(out)
    assert all(ord(c) < 128 for c in out), repr(out)
    return json.loads(out)
"""


def _run(code, tmp_path):
    return run_cli(PRELUDE + textwrap.dedent(code), env={"HOME": str(tmp_path)}, timeout=120)


# --- record ------------------------------------------------------------------


def test_record_bare_sends_what_it_always_did(tmp_path):
    ok(_run("""
        b = body(["record", "The user prefers pnpm."])
        assert b == {"space_id": "s", "content": "The user prefers pnpm.", "metadata": {}}, b
        print("OK")
    """, tmp_path))


def test_record_flags_reach_the_body(tmp_path):
    ok(_run("""
        b = body(["record", "x", "--tags", "a,b", "--tags", "c",
                  "--timestamp", "2025-06-01T09:00:00Z",
                  "--metadata", '{"source": "cli", "n": 1}',
                  "--user-id", "u1", "--agent-id", "a1", "--session-id", "s1"])
        assert b["tags"] == ["a", "b", "c"], b
        assert b["timestamp"] == "2025-06-01T09:00:00Z", b
        assert b["metadata"] == {"source": "cli", "n": 1}, b
        assert (b["user_id"], b["agent_id"], b["session_id"]) == ("u1", "a1", "s1"), b
        # Each alone, and the rest stay absent.
        b = body(["record", "x", "--user-id", "u1"])
        assert set(b) == {"space_id", "content", "metadata", "user_id"}, b
        b = body(["record", "x", "--timestamp", "2025-06-01"])
        assert set(b) == {"space_id", "content", "metadata", "timestamp"}, b
        assert b["timestamp"] == "2025-06-01", b
        print("OK")
    """, tmp_path))


def test_record_content_from_stdin_and_file(tmp_path):
    ok(_run(f"""
        b = body(["record", "-"], stdin="From stdin, line one.\\nLine two.\\n")
        assert b["content"] == "From stdin, line one.\\nLine two.\\n", b
        p = {str(tmp_path / "note.txt")!r}
        open(p, "w", encoding="utf-8").write("From a file.")
        b = body(["record", "--file", p])
        assert b["content"] == "From a file.", b
        print("OK")
    """, tmp_path))


def test_record_usage_errors_send_nothing(tmp_path):
    ok(_run(f"""
        p = {str(tmp_path / "note.txt")!r}
        open(p, "w").write("x")
        refused(["record", "x", "--file", p])          # both sources
        refused(["record"])                            # neither
        refused(["record", "-"], stdin="   \\n")        # empty stdin
        e = {str(tmp_path / "empty.txt")!r}
        open(e, "w").write("")
        refused(["record", "--file", e])               # empty file
        refused(["record", "--file", {str(tmp_path / "missing.txt")!r}])
        refused(["record", "x", "--timestamp", "yesterday"])
        refused(["record", "x", "--timestamp", "2025-13-01"])
        refused(["record", "x", "--metadata", "{{not json"])
        refused(["record", "x", "--metadata", "[1, 2]"])
        refused(["record", "x", "--metadata", '"a string"'])
        refused(["record", "x", "--tags", ""])
        refused(["record", "x", "--tags", "a,,b"])
        print("OK")
    """, tmp_path))


def test_record_json_on_success_nothing_stored_and_failure(tmp_path):
    ok(_run("""
        rc, out, err = run_main(["record", "x", "--json"] + AUTH)
        assert rc == 0, (rc, out, err)
        assert one_json(out) == {"memory_id": "m1", "memory_ids": ["m1"], "status": "stored"}

        MODE["record"] = "nothing"
        rc, out, err = run_main(["record", "x", "--json"] + AUTH)
        assert rc == 3, (rc, out, err)
        assert one_json(out)["status"] == "nothing_extracted", out
        assert "Nothing was stored." in err, err

        MODE["status"] = 500
        rc, out, err = run_main(["record", "x", "--json"] + AUTH)
        assert rc == 7 and out == "", (rc, out, err)
        assert err.strip() == "anona: internal_error: Something broke. (request_id req_1)", err
        MODE["status"] = 401
        rc, out, err = run_main(["record", "x", "--json"] + AUTH)
        assert rc == 4 and out == "", (rc, out, err)
        print("OK")
    """, tmp_path))


# --- retrieve ----------------------------------------------------------------


def test_retrieve_bare_body_is_unchanged(tmp_path):
    ok(_run("""
        b = body(["retrieve", "q"])
        assert b == {"space_id": "s", "query": "q", "limit": 10, "mode": "accurate"}, b
        print("OK")
    """, tmp_path))


def test_retrieve_flags_reach_the_body(tmp_path):
    ok(_run("""
        b = body(["retrieve", "q", "--tags", "a,b", "--tags-match", "all_strict",
                  "--as-of", "2026-01-01T00:00:00+00:00",
                  "--occurred-after", "2025-01-01", "--occurred-before", "2025-12-31T23:59:59Z"])
        assert b["tags"] == ["a", "b"], b
        assert b["tags_match"] == "all_strict", b
        assert b["as_of"] == "2026-01-01T00:00:00+00:00", b
        assert b["occurred_after"] == "2025-01-01", b
        assert b["occurred_before"] == "2025-12-31T23:59:59Z", b
        b = body(["retrieve", "q", "--tags", "a"])
        assert set(b) == {"space_id", "query", "limit", "mode", "tags"}, b
        b = body(["retrieve", "q", "--as-of", "2026-01-01"])
        assert set(b) == {"space_id", "query", "limit", "mode", "as_of"}, b
        print("OK")
    """, tmp_path))


def test_retrieve_usage_errors_send_nothing(tmp_path):
    ok(_run("""
        refused(["retrieve", "q", "--as-of", "last week"])
        refused(["retrieve", "q", "--occurred-after", "2025-02-30"])
        refused(["retrieve", "q", "--occurred-before", "soon"])
        refused(["retrieve", "q", "--tags", "a", "--tags-match", "some"])
        # A match mode with nothing to match is a mistake, not a no-op.
        refused(["retrieve", "q", "--tags-match", "all"])
        refused(["retrieve", "q", "--tags", ","])
        print("OK")
    """, tmp_path))


def test_retrieve_json_on_hit_and_no_match(tmp_path):
    ok(_run("""
        rc, out, err = run_main(["retrieve", "q", "--json"] + AUTH)
        assert rc == 0, (rc, out, err)
        doc = one_json(out)
        assert doc["results"][0]["content"] == "the user prefers pnpm \\u00e9", doc
        MODE["retrieve"] = "none"
        rc, out, err = run_main(["retrieve", "q", "--json"] + AUTH)
        assert rc == 0 and one_json(out) == {"results": []}, (rc, out, err)
        MODE["status"] = 500
        rc, out, err = run_main(["retrieve", "q", "--json"] + AUTH)
        assert rc == 7 and out == "", (rc, out, err)
        print("OK")
    """, tmp_path))


# --- reason ------------------------------------------------------------------


def test_reason_bare_body_is_unchanged(tmp_path):
    ok(_run("""
        b = body(["reason", "q"])
        assert b == {"space_id": "s", "query": "q"}, b
        print("OK")
    """, tmp_path))


def test_reason_flags_reach_the_body(tmp_path):
    ok(_run("""
        b = body(["reason", "q", "--depth", "thorough",
                  "--user-id", "u1", "--agent-id", "a1", "--session-id", "s1"])
        assert b == {"space_id": "s", "query": "q", "user_id": "u1", "agent_id": "a1",
                     "session_id": "s1", "depth": "thorough"}, b
        b = body(["reason", "q", "--depth", "fast"])
        assert b == {"space_id": "s", "query": "q", "depth": "fast"}, b
        refused(["reason", "q", "--depth", "medium"])
        print("OK")
    """, tmp_path))


def test_reason_json_carries_the_receipt(tmp_path):
    ok(_run("""
        rc, out, err = run_main(["reason", "q", "--json"] + AUTH)
        assert rc == 0, (rc, out, err)
        doc = one_json(out)
        assert doc["insights"] == "An answer.\\nSecond line.", doc
        assert doc["sources"] == {"models": [], "memories": 2, "notes": 1}, doc
        assert doc["model"] == "m-x" and doc["rules_applied"] == [], doc
        MODE["status"] = 500
        rc, out, err = run_main(["reason", "q", "--json"] + AUTH)
        assert rc == 7 and out == "", (rc, out, err)
        print("OK")
    """, tmp_path))


def test_json_escapes_control_characters(tmp_path):
    # Terminal-safe: an ESC in stored content arrives escaped, never raw.
    ok(_run("""
        H_post = H.do_POST
        def evil(self):
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            SEEN.append((self.path, json.loads(body or b"{}")))
            data = json.dumps({"insights": "a\\x1b[2Jb\\u202e"}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        H.do_POST = evil
        rc, out, err = run_main(["reason", "q", "--json"] + AUTH)
        assert rc == 0 and "\\x1b" not in out and "\\u202e" not in out, repr(out)
        assert one_json(out)["insights"] == "a\\x1b[2Jb\\u202e"
        print("OK")
    """, tmp_path))
