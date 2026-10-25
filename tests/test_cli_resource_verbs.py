"""The CLI's resource verbs: spaces, memories, jobs, profile, model,
user-profile and usage.

Each is a thin wrapper over one SDK method. Every case runs a real local HTTP
server and asserts on the exact method, path, query and body that reached it,
so a verb that called the wrong route, sent a flag that was not given, or sent
anything at all when it should have refused, fails here. Runs in a subprocess
(see _cli.py) so `import anona` is the public SDK.
"""
from __future__ import annotations

import textwrap

from _cli import ok, run_cli

PRELUDE = """
import io, json, os, re, sys, threading, time
from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl
from anona.cli import main as cli_main

for _name in ("ANONA_BASE_URL", "ANONA_API_KEY", "ANONA_SPACE_ID"):
    os.environ.pop(_name, None)

SEEN = []
MODE = {"status": 200, "empty": False, "hostile": False, "job_not_found": False}
KEY = "anona_live_SECRETSECRET"
ESC = "\\x1b[2J"

SPACE = {"space_id": "notes", "name": "notes", "description": "Team notes",
         "created_at": "2026-10-01T00:00:00Z", "shared_by": None, "qualified_id": None}
SHARED = {"space_id": "demo", "name": "demo", "description": None,
          "created_at": None, "shared_by": "Acme", "qualified_id": "acme:demo"}
ITEM = {"id": "mem-0001-aaaa", "content": "The user prefers pnpm.", "text": "The user prefers pnpm.",
        "type": "observation", "state": "active", "tags": [], "source_ids": []}

def canned(method, path):
    if MODE["empty"]:
        if path == "/v1/spaces/":
            return 200, {"spaces": [], "total": 0}
        if path.endswith("/memories"):
            return 200, {"items": [], "total": 0, "limit": 50, "offset": 0}
        if path.endswith("/history"):
            return 200, {"memory_id": "mem-0001-aaaa", "history": []}
    if path == "/v1/spaces/" and method == "GET":
        return 200, {"spaces": [SPACE, SHARED], "total": 2}
    if path == "/v1/spaces/" and method == "POST":
        return 201, {**SPACE, "space_id": "fresh", "name": "fresh", "space_type": None}
    if re.fullmatch(r"/v1/spaces/[^/]+", path) and method == "GET":
        return 200, SPACE
    if path.endswith("/memories") and method == "GET":
        return 200, {"items": [ITEM], "total": 1, "limit": 50, "offset": 0}
    if path.endswith("/history"):
        return 200, {"memory_id": "mem-0001-aaaa", "history": [
            {"previous_content": "The user prefers npm.", "changed_at": "2026-10-02T00:00:00Z",
             "reason": "corrected"}]}
    if "/memories/" in path and method == "PATCH":
        return 200, {**ITEM, "content": "The user prefers bun.", "text": "The user prefers bun."}
    if "/memories/" in path and method == "DELETE":
        return 204, None
    if "/jobs/" in path and method == "GET" and MODE["job_not_found"]:
        # The API's answer for an id the space does not hold: 200, not 404.
        return 200, {"job_id": "job-0001-bbbb", "status": "not_found",
                     "memory_count": None, "memory_ids": None}
    if "/jobs/" in path and method == "GET":
        return 200, {"job_id": "job-0001-bbbb", "status": "completed", "created_at": None,
                     "completed_at": None, "error": None, "memory_count": 2,
                     "memory_ids": ["mem-a", "mem-b"], "items_submitted": 3}
    if "/jobs/" in path and method == "DELETE":
        return 200, {"job_id": "job-0001-bbbb", "status": "cancelled",
                     "cancelled_parts": 2, "running_parts": 1}
    if path.endswith("/users/alice/profile"):
        return 200, {"space_id": "s", "user_id": "alice", "memory_count": 1,
                     "first_seen": "2026-09-01T00:00:00Z", "last_active": "2026-10-01T00:00:00Z",
                     "memories": [ITEM]}
    if path.endswith("/profile"):
        return 200, {"space_id": "s", "name": "s", "mission": "Remember team decisions.",
                     "disposition": {"skepticism": 3, "literalism": 2, "empathy": 4}}
    if "/models/" in path:
        return 200, {"id": "mm-0001-cccc", "space_id": "s", "name": "Build habits",
                     "query": "How does this team build?",
                     "content": "Uses pnpm.\\n\\nShips Tuesdays.", "tags": []}
    if path == "/v1/usage/me":
        return 200, {"credits_remaining": 4200, "credits_limit": 5000, "credits_used": 800,
                     "rate_limit_per_min": 60, "credits_banked": 0}
    return 404, {"error": {"code": "not_found", "message": "No route."}}

def hostile(doc):
    # Every string the server sends grows a terminal escape.
    if isinstance(doc, dict):
        return {k: hostile(v) for k, v in doc.items()}
    if isinstance(doc, list):
        return [hostile(v) for v in doc]
    if isinstance(doc, str):
        return doc + ESC
    return doc

class H(BaseHTTPRequestHandler):
    def _any(self):
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        parts = urlsplit(self.path)
        SEEN.append((self.command, parts.path, dict(parse_qsl(parts.query)),
                     json.loads(raw) if raw else None, self.headers.get("authorization")))
        status = MODE["status"]
        if status == 401:
            out = {"error": {"code": "invalid_api_key", "message": "Invalid API key."}}
        elif status == 403:
            out = {"error": {"code": "temp_profile_single_space",
                             "message": "A temporary profile has one space.", "request_id": "req_9"}}
        elif status == 404:
            out = {"error": {"code": "not_found", "message": "Not found.", "request_id": "req_4"}}
        elif status >= 400:
            out = {"error": {"code": "internal_error", "message": "Something broke.",
                             "request_id": "req_1"}}
        else:
            status, out = canned(self.command, parts.path)
            if MODE["hostile"] and out is not None:
                out = hostile(out)
        self.send_response(status)
        if out is None:
            self.send_header("content-length", "0")
            self.end_headers()
            return
        data = json.dumps(out).encode()
        if status >= 500 or status == 429:
            # The SDK retries an idempotent 5xx; no wait, or the suite crawls.
            self.send_header("retry-after", "0")
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    do_GET = do_POST = do_PATCH = do_DELETE = _any
    def log_message(self, *a): pass

srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % srv.server_address[1]
KEYED = ["--base-url", BASE, "--api-key", KEY]
AUTH = ["--space", "s"] + KEYED
ANONA = Path.home() / ".anona"

def temp_profile():
    ANONA.mkdir(parents=True, exist_ok=True)
    (ANONA / "credentials.json").write_text(json.dumps({
        "access_token": "", "refresh_token": "", "expires_at": 0, "client_id": "",
        "base_url": BASE, "temp_token": "anona_tmp_TEMPTOKEN",
        "temp_expires_at": time.time() + 3600}))

def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = cli_main.main(argv)
        except SystemExit as exc:
            rc = exc.code
    o, e = out.getvalue(), err.getvalue()
    assert KEY not in o + e and "SECRETSECRET" not in o + e, (argv, o, e)
    return rc, o, e

def sent(argv, want_rc=0):
    n = len(SEEN)
    rc, out, err = run_main(argv)
    assert rc == want_rc, (argv, rc, out, err)
    assert len(SEEN) == n + 1, (argv, SEEN[n:])
    return SEEN[-1], out, err

def refused(argv, want=2):
    n = len(SEEN)
    rc, out, err = run_main(argv)
    assert rc == want, (argv, rc, out, err)
    assert len(SEEN) == n, ("sent", argv, SEEN[n:])
    assert out == "", (argv, out)
    return err

def one_json(out):
    assert out.endswith("\\n") and out.count("\\n") == 1, repr(out)
    assert all(ord(c) < 128 for c in out), repr(out)
    return json.loads(out)

# Every verb, as (argv, method, path, query, body). `None` query means "no
# query string"; the list default of the SDK is what it always sends.
VERBS = [
    (["spaces", "list"] + KEYED, "GET", "/v1/spaces/", {}, None),
    (["spaces", "create", "fresh"] + KEYED, "POST", "/v1/spaces/", {},
     {"name": "fresh", "description": None}),
    (["spaces", "get", "notes"] + KEYED, "GET", "/v1/spaces/notes", {}, None),
    (["memories", "list"] + AUTH, "GET", "/v1/spaces/s/memories",
     {"limit": "50", "offset": "0"}, None),
    (["memories", "edit", "mem-0001-aaaa", "--text", "The user prefers bun."] + AUTH,
     "PATCH", "/v1/spaces/s/memories/mem-0001-aaaa", {}, {"text": "The user prefers bun."}),
    (["memories", "delete", "mem-0001-aaaa", "--yes"] + AUTH,
     "DELETE", "/v1/spaces/s/memories/mem-0001-aaaa", {}, None),
    (["memories", "history", "mem-0001-aaaa"] + AUTH,
     "GET", "/v1/spaces/s/memories/mem-0001-aaaa/history", {}, None),
    (["jobs", "get", "job-0001-bbbb"] + AUTH, "GET", "/v1/spaces/s/jobs/job-0001-bbbb", {}, None),
    (["jobs", "cancel", "job-0001-bbbb", "--yes"] + AUTH,
     "DELETE", "/v1/spaces/s/jobs/job-0001-bbbb", {}, None),
    (["profile"] + AUTH, "GET", "/v1/spaces/s/profile", {}, None),
    (["model", "mm-0001-cccc"] + AUTH, "GET", "/v1/spaces/s/models/mm-0001-cccc", {}, None),
    (["user-profile", "alice"] + AUTH, "GET", "/v1/spaces/s/users/alice/profile", {}, None),
    (["usage"] + KEYED, "GET", "/v1/usage/me", {}, None),
]
"""


def _run(code, tmp_path):
    return run_cli(PRELUDE + textwrap.dedent(code), env={"HOME": str(tmp_path)}, timeout=120)


def test_each_verb_sends_exactly_its_request(tmp_path):
    ok(_run("""
        for argv, method, path, query, body in VERBS:
            (m, p, q, b, auth), out, err = sent(argv)
            assert (m, p) == (method, path), (argv, m, p)
            assert q == query, (argv, q)
            assert b == body, (argv, b)
            assert auth == "Bearer " + KEY, (argv, auth)
        print("OK")
    """, tmp_path))


def test_flags_reach_the_request_and_unset_ones_stay_out(tmp_path):
    ok(_run("""
        (m, p, q, b, _), _, _ = sent(["memories", "list", "--limit", "5", "--offset", "10",
                                      "--q", "pnpm", "--type", "observation",
                                      "--no-prefer-observations", "--user-id", "u1",
                                      "--agent-id", "a1", "--session-id", "s1"] + AUTH)
        assert q == {"limit": "5", "offset": "10", "q": "pnpm", "type": "observation",
                     "prefer_observations": "false", "user_id": "u1", "agent_id": "a1",
                     "session_id": "s1"}, q
        (m, p, q, b, _), _, _ = sent(["memories", "list", "--prefer-observations"] + AUTH)
        assert q["prefer_observations"] == "true", q
        (m, p, q, b, _), _, _ = sent(["memories", "edit", "mem-1", "--text", "New.",
                                      "--reason", "typo"] + AUTH)
        assert b == {"text": "New.", "reason": "typo"}, b
        (m, p, q, b, _), _, _ = sent(["spaces", "create", "fresh", "--description", "d"] + KEYED)
        assert b == {"name": "fresh", "description": "d"}, b
        (m, p, q, b, _), _, _ = sent(["user-profile", "alice", "--limit", "3", "--offset", "6"] + AUTH)
        assert q == {"limit": "3", "offset": "6"}, q
        print("OK")
    """, tmp_path))


def test_json_is_one_document_on_every_verb(tmp_path):
    ok(_run("""
        for argv, method, path, query, body in VERBS:
            _, out, err = sent(argv + ["--json"])
            doc = one_json(out)
            assert isinstance(doc, dict), (argv, doc)
        # The documents are the API's, not a reshaping of them.
        _, out, _ = sent(["spaces", "list", "--json"] + KEYED)
        assert one_json(out) == {"spaces": [json.loads(json.dumps(SPACE)), SHARED], "total": 2}
        _, out, _ = sent(["memories", "list", "--json"] + AUTH)
        assert one_json(out)["items"][0]["id"] == "mem-0001-aaaa", out
        _, out, _ = sent(["jobs", "cancel", "job-0001-bbbb", "--yes", "--json"] + AUTH)
        assert one_json(out)["running_parts"] == 1, out
        # A delete answers 204 with no body; the document names what went.
        _, out, _ = sent(["memories", "delete", "mem-0001-aaaa", "--yes", "--json"] + AUTH)
        assert one_json(out) == {"deleted": True, "memory_id": "mem-0001-aaaa", "space_id": "s"}, out
        print("OK")
    """, tmp_path))


def test_plain_output_names_full_ids(tmp_path):
    ok(_run("""
        def text(argv):
            _, out, err = sent(argv)
            return out
        out = text(["spaces", "list"] + KEYED)
        assert "notes" in out and "acme:demo" in out and "Acme" in out, out
        assert len(out.strip().splitlines()) == 2, out
        assert "fresh" in text(["spaces", "create", "fresh"] + KEYED)
        assert "Team notes" in text(["spaces", "get", "notes"] + KEYED)
        out = text(["memories", "list"] + AUTH)
        assert "mem-0001-aaaa" in out and "The user prefers pnpm." in out, out
        assert "1 of 1" in out, out
        assert "mem-0001-aaaa" in text(["memories", "edit", "mem-0001-aaaa", "--text", "x"] + AUTH)
        assert "Deleted memory mem-0001-aaaa" in text(["memories", "delete", "mem-0001-aaaa", "--yes"] + AUTH)
        out = text(["memories", "history", "mem-0001-aaaa"] + AUTH)
        assert "The user prefers npm." in out and "corrected" in out, out
        out = text(["jobs", "get", "job-0001-bbbb"] + AUTH)
        assert "completed" in out and "mem-a" in out and "mem-b" in out, out
        out = text(["jobs", "cancel", "job-0001-bbbb", "--yes"] + AUTH)
        assert "2" in out and "1" in out and "job-0001-bbbb" in out, out
        out = text(["profile"] + AUTH)
        assert "Remember team decisions." in out and "skepticism" in out, out
        out = text(["model", "mm-0001-cccc"] + AUTH)
        # Markdown keeps its paragraph breaks, like `reason`.
        assert "Uses pnpm.\\n\\nShips Tuesdays." in out and "Build habits" in out, out
        out = text(["user-profile", "alice"] + AUTH)
        assert "alice" in out and "mem-0001-aaaa" in out, out
        out = text(["usage"] + KEYED)
        assert "4200" in out and "5000" in out and "60" in out, out
        print("OK")
    """, tmp_path))


def test_empty_lists_are_success(tmp_path):
    ok(_run("""
        MODE["empty"] = True
        for argv in (["spaces", "list"] + KEYED, ["memories", "list"] + AUTH,
                     ["memories", "history", "mem-0001-aaaa"] + AUTH):
            _, out, err = sent(argv)
            assert out.strip(), (argv, out)
            _, out, err = sent(argv + ["--json"])
            one_json(out)
        print("OK")
    """, tmp_path))


def test_server_errors_map_to_exit_codes(tmp_path):
    ok(_run("""
        for status, want in ((401, 4), (403, 4), (404, 5), (429, 6), (500, 7)):
            MODE["status"] = status
            for argv, *_ in VERBS:
                for extra in ([], ["--json"]):
                    # Not `sent`: the SDK retries a 5xx, so one command may
                    # make more than one request.
                    n = len(SEEN)
                    rc, out, err = run_main(argv + extra)
                    assert rc == want, (argv, status, rc, out, err)
                    assert len(SEEN) > n, (argv, "nothing sent")
                    assert out == "", (argv, status, out)
                    assert err.startswith("anona: "), (argv, err)
        MODE["status"] = 404
        _, _, err = sent(["memories", "history", "nope"] + AUTH, want_rc=5)
        assert err.strip() == "anona: not_found: Not found. (request_id req_4)", err
        print("OK")
    """, tmp_path))


def test_jobs_get_on_a_job_the_space_does_not_hold_exits_5(tmp_path):
    ok(_run("""
        MODE["job_not_found"] = True
        for extra in ([], ["--json"]):
            rc, out, err = run_main(["jobs", "get", "job-0001-bbbb"] + AUTH + extra)
            assert rc == 5, (rc, out, err)
            assert out == "", out
            assert err.strip() == "anona: job_not_found: No job with that id in this space.", err
        print("OK")
    """, tmp_path))


def test_destructive_verbs_need_yes_and_send_nothing_without_it(tmp_path):
    ok(_run("""
        err = refused(["memories", "delete", "mem-0001-aaaa"] + AUTH)
        assert "mem-0001-aaaa" in err and "'s'" in err and "--yes" in err, err
        assert "Nothing was sent" in err, err
        err = refused(["memories", "delete", "mem-0001-aaaa", "--json"] + AUTH)
        assert "--yes" in err, err
        err = refused(["jobs", "cancel", "job-0001-bbbb"] + AUTH)
        assert "job-0001-bbbb" in err and "--yes" in err, err
        # On a temp profile too, and before the credential is used at all.
        temp_profile()
        err = refused(["memories", "delete", "mem-0001-aaaa", "--base-url", BASE])
        assert "'default'" in err, err
        refused(["jobs", "cancel", "job-0001-bbbb", "--base-url", BASE])
        print("OK")
    """, tmp_path))


def test_an_api_key_needs_a_space_for_space_verbs_only(tmp_path):
    ok(_run("""
        for argv, *_ in VERBS:
            if "--space" not in argv:
                continue
            i = argv.index("--space")
            refused(argv[:i] + argv[i + 2:])
        # ANONA_SPACE_ID stands in for the flag.
        os.environ["ANONA_SPACE_ID"] = "envspace"
        (m, p, *_), _, _ = sent(["profile"] + KEYED)
        assert p == "/v1/spaces/envspace/profile", p
        del os.environ["ANONA_SPACE_ID"]
        # And the account-level verbs need none.
        sent(["spaces", "list"] + KEYED)
        sent(["usage"] + KEYED)
        print("OK")
    """, tmp_path))


def test_a_temp_profile_uses_its_default_space(tmp_path):
    ok(_run("""
        temp_profile()
        (m, p, q, b, auth), _, _ = sent(["memories", "list", "--base-url", BASE])
        assert p == "/v1/spaces/default/memories" and auth == "Bearer anona_tmp_TEMPTOKEN", (p, auth)
        (m, p, *_), _, _ = sent(["usage", "--base-url", BASE])
        assert p == "/v1/usage/me", p
        # A second space is the server's call; the CLI only reports it.
        MODE["status"] = 403
        _, out, err = sent(["spaces", "create", "another", "--base-url", BASE], want_rc=4)
        assert out == "" and "temp_profile_single_space" in err, (out, err)
        print("OK")
    """, tmp_path))


def test_server_text_cannot_reach_the_terminal_raw(tmp_path):
    ok(_run("""
        MODE["hostile"] = True
        for argv, *_ in VERBS:
            _, out, err = sent(argv)
            assert "\\x1b" not in out + err, (argv, out, err)
        print("OK")
    """, tmp_path))


def test_usage_errors_send_nothing(tmp_path):
    ok(_run("""
        refused(["memories", "edit", "mem-1"] + AUTH)                 # no --text
        refused(["memories", "edit", "mem-1", "--text", "  "] + AUTH)  # empty
        refused(["memories", "list", "--limit", "0"] + AUTH)
        refused(["memories", "list", "--limit", "201"] + AUTH)
        refused(["memories", "list", "--offset", "-1"] + AUTH)
        refused(["spaces", "create", " "] + KEYED)
        refused(["spaces"] + KEYED)
        refused(["memories"] + AUTH)
        refused(["jobs"] + AUTH)
        print("OK")
    """, tmp_path))


def test_every_verb_is_in_help(tmp_path):
    ok(_run("""
        rc, out, err = run_main(["--help"])
        assert rc == 0, (rc, err)
        for verb in ("spaces", "memories", "jobs", "profile", "model", "user-profile", "usage"):
            assert re.search(r"^\\s+%s\\s+\\S" % re.escape(verb), out, re.M), (verb, out)
        for group, actions in (("spaces", ("list", "create", "get")),
                               ("memories", ("list", "edit", "delete", "history")),
                               ("jobs", ("get", "cancel"))):
            rc, out, err = run_main([group, "--help"])
            assert rc == 0, (group, rc, err)
            for action in actions:
                assert re.search(r"^\\s+%s\\s+\\S" % action, out, re.M), (group, action, out)
        print("OK")
    """, tmp_path))
