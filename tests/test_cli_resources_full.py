"""The CLI's coverage of the rest of the API: memory models, rules, webhooks,
the entity graph, receipts, ask-user, chat, exports, and the account views.

Every case runs a real local HTTP server and asserts on the exact method,
path, query and body that reached it and on the exact text printed, so a verb
that called the wrong route, sent a flag that was not given, printed a line
other than the documented one, or sent anything at all when it should have
refused, fails here. Runs in a subprocess (see _cli.py) so `import anona` is
the public SDK.
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
MODE = {"status": 200, "stream": False, "blob": 200}
KEY = "anona_live_SECRETSECRET"
LEAK = "anona_live_LEAKEDSECRET"

MODEL = {"id": "mm-1", "space_id": "s", "name": "Build habits",
         "query": "How does this team build?", "content": "Uses pnpm.", "tags": []}
PROFILE = {"space_id": "s", "name": "s", "mission": "M",
           "disposition": {"skepticism": 3, "literalism": 2, "empathy": 4}}
ROUTING = {"space_id": "s", "charter": None, "topics": None, "rules": None,
           "auto_route_enabled": True, "shared_auto_route": False,
           "observed_terms": None, "inferred_terms": None, "rendered_criteria": None}
JOB = {"model_id": "mm-1", "job_id": "job-9", "status": "processing"}

# (method, path regex) -> (status, body). First match wins.
ROUTES = [
    ("GET", r"/v1/spaces/s/models", 200, {"items": [MODEL], "total": 1, "limit": 50, "offset": 0}),
    ("POST", r"/v1/spaces/s/models", 202, JOB),
    ("GET", r"/v1/spaces/s/models/mm-1", 200, MODEL),
    ("PATCH", r"/v1/spaces/s/models/mm-1", 200, MODEL),
    ("DELETE", r"/v1/spaces/s/models/mm-1", 204, None),
    ("POST", r"/v1/spaces/s/models/mm-1/refresh", 202, JOB),
    ("POST", r"/v1/spaces/s/models/mm-1/clear", 200, MODEL),
    ("GET", r"/v1/spaces/s/models/mm-1/history", 200,
     {"entries": [{"previous_content": "x" * 100, "changed_at": "2026-10-02T00:00:00Z"}], "total": 1}),
    ("GET", r"/v1/spaces/s/profile", 200, PROFILE),
    ("PUT", r"/v1/spaces/s/profile", 200, PROFILE),
    ("DELETE", r"/v1/spaces/s/profile", 204, None),
    ("GET", r"/v1/spaces/s/rules", 200,
     {"items": [{"id": "r-1", "space_id": "s", "name": "n", "content": "Never X."}], "total": 1}),
    ("POST", r"/v1/spaces/s/rules", 201, {"id": "r-1", "space_id": "s", "name": "n", "content": "Never X."}),
    ("PATCH", r"/v1/spaces/s/rules/r-1", 200, {"id": "r-1", "space_id": "s", "name": "n", "content": "Y."}),
    ("DELETE", r"/v1/spaces/s/rules/r-1", 204, None),
    ("GET", r"/v1/spaces/s/webhooks", 200,
     {"items": [{"id": "w-1", "url": "https://x.example/h", "enabled": True,
                 "event_types": ["memory.created", "memory.consolidated"]}]}),
    ("POST", r"/v1/spaces/s/webhooks", 201,
     {"id": "w-1", "url": "https://x.example/h", "enabled": True,
      "event_types": ["memory.created"], "secret": "whsec_TESTSECRET"}),
    ("PATCH", r"/v1/spaces/s/webhooks/w-1", 200,
     {"id": "w-1", "url": "https://x.example/h", "enabled": False, "event_types": ["memory.created"]}),
    ("DELETE", r"/v1/spaces/s/webhooks/w-1", 204, None),
    ("GET", r"/v1/spaces/s/webhooks/w-1/deliveries", 200,
     {"items": [{"id": "d1", "event_type": "memory.created", "status": "completed",
                 "last_attempt_at": "2026-10-03T00:00:00Z", "created_at": "2026-10-03T00:00:00Z"}],
      "next_cursor": None}),
    ("GET", r"/v1/spaces/s/graph", 200,
     {"nodes": [{"id": "e1", "label": "Alice", "mention_count": 3}], "edges": [],
      "total_entities": 1, "total_edges": 0}),
    ("POST", r"/v1/spaces/s/graph", 200,
     {"chart": {}, "diagram": None, "mermaid": None, "text": "Here is the chart.", "model": "m"}),
    ("GET", r"/v1/spaces/s/entities", 200,
     {"items": [{"id": "e1", "name": "Alice", "mention_count": 3}], "total": 1, "limit": 100, "offset": 0}),
    ("GET", r"/v1/spaces/s/entities/e1", 200,
     {"id": "e1", "name": "Alice", "mention_count": 3,
      "observations": [{"text": "Likes tea.", "mentioned_at": "2026-10-01"}]}),
    ("GET", r"/v1/receipts/req_1", 200,
     {"request_id": "req_1", "space_id": "s", "included": [{"id": "a"}],
      "excluded": [{"id": "b"}], "token_accounting": {"used": 40, "budget": 100}}),
    ("GET", r"/v1/receipts/req_1/explain", 200,
     {"memory_id": "m1", "outcome": "excluded", "stage": "budget", "detail": "over budget"}),
    ("POST", r"/v1/spaces/s/users/alice/ask", 200, {"space_id": "s", "user_id": "alice", "insights": "She likes tea."}),
    ("GET", r"/v1/space-types", 200, {"items": [{"space_type": "general", "name": "General"}], "total": 1}),
    ("POST", r"/v1/chat/completions", 200,
     {"id": "c1", "choices": [{"message": {"role": "assistant", "content": "Hello!"}}], "usage": {}}),
    ("GET", r"/v1/orgs/me", 200, {"org_id": "o1", "name": "Acme", "slug": "acme", "plan_id": "developer"}),
    ("GET", r"/v1/orgs", 200, {"orgs": [{"org_id": "o1", "name": "Acme", "role": "owner"}], "max_orgs": 3}),
    ("GET", r"/v1/projects", 200, [{"project_id": "p1", "org_id": "o1", "name": "Docs"}]),
    ("GET", r"/v1/api-keys", 200,
     [{"id": "k1", "name": "ci", "key_prefix": "anona_live_ab", "env": "live",
       "created_at": "2026-10-01", "last_used_at": None, "status": "active", "key": LEAK}]),
    ("GET", r"/v1/members", 200, [{"member_id": "m1", "email": "a@x.example", "role": "owner"}]),
    ("GET", r"/v1/members/me", 200, {"member_id": "m1", "email": "a@x.example", "role": "owner"}),
    ("GET", r"/v1/members/invitations", 200,
     {"invitations": [{"invitation_id": "i1", "email": "b@x.example", "role": "developer"}]}),
    ("GET", r"/v1/invitations/pending", 200,
     {"invitations": [{"token": "tok-1", "kind": "space", "org_name": "Acme",
                       "space_id": "notes", "role": "viewer", "expires_at": "x"}]}),
    ("POST", r"/v1/invitations/accept", 200, {"org_id": "o1", "space_id": None}),
    ("POST", r"/v1/invitations/decline", 204, None),
    ("GET", r"/v1/billing", 200,
     {"plan_id": "developer", "plan_name": "Developer", "credits_limit": 5000, "credits_used": 800,
      "credits_remaining": 4200, "credits_banked": 0, "credits_reset_at": "2026-11-01T00:00:00Z"}),
    ("GET", r"/v1/billing/topups", 200,
     {"items": [{"id": "t1", "purchased_at": "2026-10-05", "pack": "small", "credits": 1000, "status": "active"}]}),
    ("GET", r"/v1/usage", 200, {"series": [{"date": "2026-10-01", "ops": 12}], "period_days": 7}),
    ("GET", r"/v1/usage/breakdown", 200,
     {"breakdown": [{"space_id": "s", "memories_added": 2, "searches": 3, "insights": 1}], "period_days": 30}),
    ("GET", r"/v1/spaces/s/exports", 200,
     {"items": [{"job_id": "j1", "status": "completed", "created_at": "2026-10-01"}], "total": 1}),
    ("POST", r"/v1/spaces/s/exports", 202, {"job_id": "j1", "space_id": "s", "status": "pending"}),
    ("GET", r"/v1/spaces/notes/members", 200,
     [{"member_id": "m1", "email": "a@x.example", "role": "viewer", "status": "active"}]),
    ("DELETE", r"/v1/spaces/notes/members/m1", 204, None),
    ("GET", r"/v1/spaces/s/routing-settings", 200, ROUTING),
    ("PUT", r"/v1/spaces/s/routing-settings", 200, ROUTING),
    ("DELETE", r"/v1/spaces/s/routing-settings", 204, None),
]

def canned(method, path):
    if path == "/v1/spaces/s/exports/j1/download":
        if MODE["stream"]:
            return 200, b"ZIPDATA"
        return 200, {"download_url": BASE + "/blob/j1", "expires_in": 300}
    if path == "/blob/j1":
        if MODE["blob"] != 200:
            return MODE["blob"], b"<Error><Code>AccessDenied</Code></Error>"
        return 200, b"ZIPDATA"
    for m, pattern, status, body in ROUTES:
        if m == method and re.fullmatch(pattern, path):
            return status, body
    return 404, {"error": {"code": "not_found", "message": "No route."}}

class H(BaseHTTPRequestHandler):
    def _any(self):
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        parts = urlsplit(self.path)
        SEEN.append((self.command, parts.path, parse_qsl(parts.query),
                     json.loads(raw) if raw else None, self.headers.get("authorization")))
        if MODE["status"] == 401:
            status, out = 401, {"error": {"code": "session_required",
                                          "message": "This needs a dashboard session or `anona login --cli`; an API key or temporary profile cannot use it."}}
        else:
            status, out = canned(self.command, parts.path)
        self.send_response(status)
        if out is None:
            self.send_header("content-length", "0")
            self.end_headers()
            return
        if isinstance(out, bytes):
            data, ctype = out, "application/zip"
        else:
            data, ctype = json.dumps(out).encode(), "application/json"
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _any
    def log_message(self, *a): pass

srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % srv.server_address[1]
KEYED = ["--base-url", BASE, "--api-key", KEY]
AUTH = ["--space", "s"] + KEYED

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
    m, p, q, b, auth = SEEN[-1]
    return (m, p, dict(q), b, auth), out, err

def refused(argv, want=2):
    n = len(SEEN)
    rc, out, err = run_main(argv)
    assert rc == want, (argv, rc, out, err)
    assert len(SEEN) == n, ("sent", argv, SEEN[n:])
    assert out == "", (argv, out)
    return err

def one_json(out):
    assert out.endswith("\\n") and out.count("\\n") == 1, repr(out)
    return json.loads(out)

def body_of(method, path):
    for m, pattern, status, body in ROUTES:
        if m == method and re.fullmatch(pattern, path):
            return body

LONG = "x" * 80

# (argv, method, path, query, request body, exact stdout)
CASES = [
    (["models", "list"] + AUTH, "GET", "/v1/spaces/s/models", {}, None,
     "mm-1  Build habits  How does this team build?\\n"),
    (["models", "list", "--limit", "5", "--offset", "2", "--tags", "a"] + AUTH, "GET", "/v1/spaces/s/models",
     {"limit": "5", "offset": "2", "tags": "a"}, None, "mm-1  Build habits  How does this team build?\\n"),
    (["models", "create", "Build habits", "--query", "How?"] + AUTH, "POST", "/v1/spaces/s/models", {},
     {"name": "Build habits", "query": "How?"}, "Creating memory model mm-1 (job job-9).\\n"),
    (["models", "create", "Build habits", "--query", "How?", "--tags", "a", "--mode", "delta",
      "--refresh-on-new-memories", "--memory-type", "fact", "--max-tokens", "512", "--model-id", "build"] + AUTH,
     "POST", "/v1/spaces/s/models", {},
     {"name": "Build habits", "query": "How?", "model_id": "build", "tags": ["a"], "max_tokens": 512,
      "trigger": {"mode": "delta", "refresh_on_new_memories": True, "memory_type": ["fact"]}},
     "Creating memory model mm-1 (job job-9).\\n"),
    (["models", "get", "mm-1"] + AUTH, "GET", "/v1/spaces/s/models/mm-1", {}, None,
     "Build habits (mm-1)\\nQuery: How does this team build?\\n\\nUses pnpm.\\n"),
    (["models", "edit", "mm-1", "--name", "Renamed"] + AUTH, "PATCH", "/v1/spaces/s/models/mm-1", {},
     {"name": "Renamed"}, "Updated memory model mm-1.\\n"),
    (["models", "delete", "mm-1", "--yes"] + AUTH, "DELETE", "/v1/spaces/s/models/mm-1", {}, None,
     "Deleted memory model mm-1.\\n"),
    (["models", "refresh", "mm-1"] + AUTH, "POST", "/v1/spaces/s/models/mm-1/refresh", {}, None,
     "Refreshing mm-1 (job job-9).\\n"),
    (["models", "clear", "mm-1", "--yes"] + AUTH, "POST", "/v1/spaces/s/models/mm-1/clear", {}, None,
     "Cleared mm-1; its definition is kept.\\n"),
    (["models", "history", "mm-1"] + AUTH, "GET", "/v1/spaces/s/models/mm-1/history", {}, None,
     "2026-10-02T00:00:00Z  " + LONG + "\\n"),
    (["profile", "reset", "--yes"] + AUTH, "DELETE", "/v1/spaces/s/profile", {}, None,
     "Reset the profile of space 's'.\\n"),
    (["rules", "list"] + AUTH, "GET", "/v1/spaces/s/rules", {}, None, "r-1  Never X.\\n"),
    (["rules", "add", "Never X."] + AUTH, "POST", "/v1/spaces/s/rules", {},
     {"name": "Never X.", "content": "Never X."}, "Added rule r-1.\\n"),
    (["rules", "add", "Never X.", "--name", "N", "--priority", "5", "--no-active", "--tags", "a"] + AUTH,
     "POST", "/v1/spaces/s/rules", {},
     {"name": "N", "content": "Never X.", "priority": 5, "is_active": False, "tags": ["a"]}, "Added rule r-1.\\n"),
    (["rules", "edit", "r-1", "--text", "Y.", "--priority", "3"] + AUTH, "PATCH", "/v1/spaces/s/rules/r-1", {},
     {"content": "Y.", "priority": 3}, "Updated rule r-1.\\n"),
    (["rules", "delete", "r-1", "--yes"] + AUTH, "DELETE", "/v1/spaces/s/rules/r-1", {}, None, "Deleted rule r-1.\\n"),
    (["webhooks", "list"] + AUTH, "GET", "/v1/spaces/s/webhooks", {}, None,
     "w-1  https://x.example/h  memory.created,memory.consolidated\\n"),
    (["webhooks", "create", "https://x.example/h", "--events", "memory.created"] + AUTH, "POST",
     "/v1/spaces/s/webhooks", {}, {"url": "https://x.example/h", "event_types": ["memory.created"]},
     "Created webhook w-1.\\nSigning secret: whsec_TESTSECRET\\nSave this secret now; it is not shown again.\\n"),
    (["webhooks", "edit", "w-1", "--no-enabled"] + AUTH, "PATCH", "/v1/spaces/s/webhooks/w-1", {},
     {"enabled": False}, "Updated webhook w-1.\\n"),
    (["webhooks", "delete", "w-1", "--yes"] + AUTH, "DELETE", "/v1/spaces/s/webhooks/w-1", {}, None,
     "Deleted webhook w-1.\\n"),
    (["webhooks", "deliveries", "w-1", "--limit", "5", "--cursor", "c"] + AUTH, "GET",
     "/v1/spaces/s/webhooks/w-1/deliveries", {"limit": "5", "cursor": "c"}, None,
     "2026-10-03T00:00:00Z  completed  memory.created\\n"),
    (["graph"] + AUTH, "GET", "/v1/spaces/s/graph", {}, None, "1 entities, 0 links\\nAlice (3)\\n"),
    (["graph", "--limit", "5", "--min-count", "2"] + AUTH, "GET", "/v1/spaces/s/graph",
     {"limit": "5", "min_count": "2"}, None, "1 entities, 0 links\\nAlice (3)\\n"),
    (["graph", "draw", "by week"] + AUTH, "POST", "/v1/spaces/s/graph", {}, {"request": "by week"},
     "Here is the chart.\\n"),
    (["graph", "draw", "by week", "--model", "m"] + AUTH, "POST", "/v1/spaces/s/graph", {},
     {"request": "by week", "model": "m"}, "Here is the chart.\\n"),
    (["entities", "list", "--limit", "5", "--offset", "1"] + AUTH, "GET", "/v1/spaces/s/entities",
     {"limit": "5", "offset": "1"}, None, "e1  Alice  3\\n"),
    (["entities", "get", "e1"] + AUTH, "GET", "/v1/spaces/s/entities/e1", {}, None,
     "Alice (e1)\\nMentions: 3\\n2026-10-01  Likes tea.\\n"),
    (["receipts", "get", "req_1"] + KEYED, "GET", "/v1/receipts/req_1", {}, None,
     "Receipt req_1 for space 's'\\nIncluded: 1, excluded: 1\\nTokens: used=40, budget=100\\n"),
    (["receipts", "get", "req_1", "--explain", "--memory-id", "m1"] + KEYED, "GET", "/v1/receipts/req_1/explain",
     {"memory_id": "m1"}, None, "Memory m1: excluded\\nStage: budget\\nDetail: over budget\\n"),
    (["ask-user", "alice", "What does she like?", "--model", "m", "--depth", "thorough"] + AUTH, "POST",
     "/v1/spaces/s/users/alice/ask", {}, {"query": "What does she like?", "model": "m", "depth": "thorough"},
     "She likes tea.\\n"),
    (["ask-user", "alice", "What does she like?"] + AUTH, "POST", "/v1/spaces/s/users/alice/ask", {},
     {"query": "What does she like?"}, "She likes tea.\\n"),
    (["space-types"] + KEYED, "GET", "/v1/space-types", {}, None, "general  General\\n"),
    (["chat", "hi"] + AUTH, "POST", "/v1/chat/completions", {},
     {"messages": [{"role": "user", "content": "hi"}], "space_id": "s"}, "Hello!\\n"),
    (["chat", "hi", "--model", "m", "--no-memory"] + AUTH, "POST", "/v1/chat/completions", {},
     {"messages": [{"role": "user", "content": "hi"}], "space_id": "s", "model": "m", "memory": False,
      "auto_record": False},
     "Hello!\\n"),
    (["org"] + KEYED, "GET", "/v1/orgs/me", {}, None, "o1  Acme  acme\\n"),
    (["orgs", "list"] + KEYED, "GET", "/v1/orgs", {}, None, "o1  Acme  owner\\n"),
    (["projects", "list"] + KEYED, "GET", "/v1/projects", {}, None, "p1  Docs\\n"),
    (["keys", "list"] + KEYED, "GET", "/v1/api-keys", {}, None, "anona_live_ab…  ci  2026-10-01  -\\n"),
    (["members", "list"] + KEYED, "GET", "/v1/members", {}, None, "m1  a@x.example  owner\\n"),
    (["members", "me"] + KEYED, "GET", "/v1/members/me", {}, None, "m1  a@x.example  owner\\n"),
    (["members", "invitations"] + KEYED, "GET", "/v1/members/invitations", {}, None,
     "i1  b@x.example  developer\\n"),
    (["invitations", "pending"] + KEYED, "GET", "/v1/invitations/pending", {}, None,
     "tok-1  space  notes  viewer\\n"),
    (["invitations", "accept", "tok-1"] + KEYED, "POST", "/v1/invitations/accept", {}, {"token": "tok-1"},
     "Joined o1.\\n"),
    (["invitations", "decline", "tok-1"] + KEYED, "POST", "/v1/invitations/decline", {}, {"token": "tok-1"},
     "Declined.\\n"),
    (["billing"] + KEYED, "GET", "/v1/billing", {}, None,
     "Plan: Developer\\nCredits: 4200 remaining of 5000 (800 used).\\nPeriod ends: 2026-11-01T00:00:00Z\\n"),
    (["billing", "topups"] + KEYED, "GET", "/v1/billing/topups", {}, None,
     "t1  2026-10-05  small  1000 credits  active\\n"),
    (["usage", "--history", "--days", "7"] + KEYED, "GET", "/v1/usage", {"days": "7"}, None,
     "2026-10-01  12 ops\\n"),
    (["usage", "--breakdown"] + KEYED, "GET", "/v1/usage/breakdown", {}, None,
     "s  2 added  3 searches  1 insights\\n"),
    (["exports", "list"] + AUTH, "GET", "/v1/spaces/s/exports", {}, None, "j1  completed  2026-10-01\\n"),
    (["exports", "create"] + AUTH, "POST", "/v1/spaces/s/exports", {}, None, "Export j1 started.\\n"),
    (["spaces", "members", "list", "notes"] + KEYED, "GET", "/v1/spaces/notes/members", {}, None,
     "m1  a@x.example  viewer\\n"),
    (["routing-settings", "get"] + AUTH, "GET", "/v1/spaces/s/routing-settings", {}, None,
     "routing-settings for space 's':\\n  charter: (platform default)\\n  topics: (platform default)\\n"
     "  rules: (platform default)\\n  auto_route_enabled: True\\n  shared_auto_route: False\\n"),
    (["routing-settings", "reset", "--yes"] + AUTH, "DELETE", "/v1/spaces/s/routing-settings", {}, None,
     "Reset the routing-settings of space 's' to the platform defaults.\\n"),
]
"""


def _run(code, tmp_path):
    return run_cli(PRELUDE + textwrap.dedent(code), env={"HOME": str(tmp_path)}, timeout=180)


def test_each_verb_sends_exactly_its_request_and_prints_its_line(tmp_path):
    ok(_run("""
        for argv, method, path, query, body, text in CASES:
            (m, p, q, b, auth), out, err = sent(argv)
            assert (m, p) == (method, path), (argv, m, p)
            assert q == query, (argv, q)
            assert b == body, (argv, b)
            assert auth == "Bearer " + KEY, (argv, auth)
            assert out == text, (argv, out, text)
            assert err == "", (argv, err)
        print("OK")
    """, tmp_path))


def test_json_passes_the_document_through(tmp_path):
    ok(_run("""
        for argv, method, path, query, body, text in CASES:
            canned_body = body_of(method, path)
            _, out, _ = sent(argv + ["--json"])
            doc = one_json(out)
            if canned_body is not None:
                assert doc == canned_body, (argv, doc)
            else:
                assert isinstance(doc, dict), (argv, doc)
        print("OK")
    """, tmp_path))


def test_repeated_tags_and_memory_types_travel_as_lists(tmp_path):
    ok(_run("""
        (m, p, q, b, _), _, _ = sent(["models", "list", "--tags", "a,b"] + AUTH)
        assert [kv for kv in SEEN[-1][2] if kv[0] == "tags"] == [("tags", "a"), ("tags", "b")], SEEN[-1][2]
        (m, p, q, b, _), _, _ = sent(["models", "create", "n", "--query", "q", "--memory-type", "fact",
                                      "--memory-type", "note"] + AUTH)
        assert b["trigger"] == {"memory_type": ["fact", "note"]}, b
        print("OK")
    """, tmp_path))


def test_a_webhook_reply_without_a_secret_prints_no_secret_lines(tmp_path):
    ok(_run("""
        # A webhook without a secret in the reply prints no secret lines.
        ROUTES.insert(0, ("POST", r"/v1/spaces/s/webhooks", 201,
                          {"id": "w-2", "url": "https://x.example/h", "enabled": True, "event_types": []}))
        _, out, _ = sent(["webhooks", "create", "https://x.example/h"] + AUTH)
        assert out == "Created webhook w-2.\\n", out
        print("OK")
    """, tmp_path))


def test_dashboard_only_verbs_send_nothing_and_exit_4(tmp_path):
    ok(_run("""
        BASE_URL = "https://memory.anonalabs.com"
        want = {
            ("keys", "create"): ("creating an API key", "/dashboard/keys"),
            ("keys", "revoke"): ("revoking an API key", "/dashboard/keys"),
            ("billing", "upgrade"): ("upgrading your plan", "/dashboard/billing"),
            ("billing", "checkout"): ("checking out a plan", "/dashboard/billing"),
            ("billing", "topup"): ("buying credits", "/dashboard/billing"),
            ("billing", "cancel"): ("cancelling your subscription", "/dashboard/billing"),
            ("billing", "portal"): ("managing payment details", "/dashboard/billing"),
            ("org", "rename"): ("renaming the organization", "/dashboard/profile"),
            ("org", "delete"): ("deleting the organization", "/dashboard/profile"),
            ("org", "transfer"): ("transferring the organization", "/dashboard/profile"),
            ("org", "leave"): ("leaving the organization", "/dashboard/profile"),
            ("members", "invite"): ("inviting a member", "/dashboard/members"),
            ("members", "remove"): ("removing a member", "/dashboard/members"),
        }
        for (cmd, act), (what, path) in want.items():
            # `--yes` is accepted and ignored: still nothing sent, still exit 4.
            for extra in ([], ["some-arg"], ["--json"], ["--yes"], ["some-arg", "--yes"]):
                n = len(SEEN)
                rc, out, err = run_main([cmd, act] + extra + KEYED)
                assert rc == 4, (cmd, act, rc, out, err)
                assert out == "", (cmd, act, out)
                assert err == f"anona: {what} is done in the dashboard: {BASE_URL}{path}\\n", (cmd, act, err)
                assert len(SEEN) == n, (cmd, act, "sent a request")
        # Nested under `spaces members`; SPACE and MEMBER_ID still parse.
        # With a SPACE the line names that space's page, its id escaped as a
        # path segment.
        for extra, page in (([], ""), (["notes", "m1"], "/notes"), (["notes", "m1", "--json"], "/notes"),
                            (["notes", "m1", "--yes"], "/notes"), (["acme:a b/c", "m1"], "/acme%3Aa%20b%2Fc"),
                            (["it's(1)*!", "m1"], "/it%27s%281%29%2A%21")):
            n = len(SEEN)
            rc, out, err = run_main(["spaces", "members", "remove"] + extra + KEYED)
            assert rc == 4, (extra, rc, out, err)
            assert out == "", (extra, out)
            assert err == f"anona: removing a space member is done in the dashboard: {BASE_URL}/dashboard/spaces{page}\\n", err
            assert len(SEEN) == n, (extra, "sent a request")
        # `projects create` is gone: no dashboard page makes a project.
        rc, out, err = run_main(["projects", "create", "Docs"] + KEYED)
        assert rc == 2 and "invalid choice" in err, (rc, err)
        print("OK")
    """, tmp_path))


def test_destructive_verbs_need_yes(tmp_path):
    ok(_run("""
        guarded = [
            ["models", "delete", "mm-1"] + AUTH,
            ["models", "clear", "mm-1"] + AUTH,
            ["profile", "reset"] + AUTH,
            ["rules", "delete", "r-1"] + AUTH,
            ["webhooks", "delete", "w-1"] + AUTH,
            ["routing-settings", "reset"] + AUTH,
        ]
        for argv in guarded:
            err = refused(argv)
            assert "Pass --yes to do it. Nothing was sent." in err, (argv, err)
        print("OK")
    """, tmp_path))


def test_usage_mistakes_exit_2_and_send_nothing(tmp_path):
    ok(_run("""
        bad = [
            ["models", "edit", "mm-1"] + AUTH,
            ["rules", "edit", "r-1"] + AUTH,
            ["rules", "add", "   "] + AUTH,
            ["webhooks", "edit", "w-1"] + AUTH,
            ["profile", "set"] + AUTH,
            ["profile", "--empathy", "3"] + AUTH,
            ["graph", "draw"] + AUTH,
            ["graph", "--model", "m"] + AUTH,
            ["usage", "--days", "3"] + KEYED,
            ["usage", "--history", "--breakdown"] + KEYED,
            ["receipts", "get", "req_1", "--memory-id", "m1"] + KEYED,
            ["receipts", "get", "req_1", "--explain"] + KEYED,
            ["chat", " "] + AUTH,
            ["ask-user", "alice", " "] + AUTH,
            ["models", "create", "n", "--query", "q", "--max-tokens", "10"] + AUTH,
            ["profile", "set", "--empathy", "9"] + AUTH,
        ]
        for argv in bad:
            refused(argv)
        print("OK")
    """, tmp_path))


def test_profile_set_one_dial_keeps_the_mission(tmp_path):
    ok(_run("""
        n = len(SEEN)
        rc, out, err = run_main(["profile", "set", "--empathy", "5"] + AUTH)
        assert rc == 0, (rc, out, err)
        assert [(m, p) for m, p, *_ in SEEN[n:]] == [("GET", "/v1/spaces/s/profile"), ("PUT", "/v1/spaces/s/profile")]
        assert SEEN[-1][3] == {"mission": "M",
                               "disposition": {"skepticism": 3, "literalism": 2, "empathy": 5}}, SEEN[-1][3]
        assert out.startswith("Space: s\\nMission: M\\n"), out
        # And the mission alone keeps the dials.
        rc, out, err = run_main(["profile", "set", "--mission", "New mission."] + AUTH)
        assert rc == 0, (rc, out, err)
        assert SEEN[-1][3] == {"mission": "New mission.",
                               "disposition": {"skepticism": 3, "literalism": 2, "empathy": 4}}, SEEN[-1][3]
        # The bare form is the unchanged read.
        n = len(SEEN)
        rc, out, err = run_main(["profile"] + AUTH)
        assert rc == 0 and len(SEEN) == n + 1 and SEEN[-1][0] == "GET", (rc, SEEN[n:])
        print("OK")
    """, tmp_path))


def test_routing_settings_set_reads_then_writes_every_field(tmp_path):
    ok(_run("""
        n = len(SEEN)
        rc, out, err = run_main(["routing-settings", "set", "--charter", "Billing", "--topics", "a,b",
                                 "--no-auto-route"] + AUTH)
        assert rc == 0, (rc, out, err)
        assert [(m, p) for m, p, *_ in SEEN[n:]] == [("GET", "/v1/spaces/s/routing-settings"),
                                                      ("PUT", "/v1/spaces/s/routing-settings")]
        assert SEEN[-1][3] == {"charter": "Billing", "topics": ["a", "b"], "rules": None,
                               "auto_route_enabled": False, "shared_auto_route": False}, SEEN[-1][3]
        # The switches are booleans the API will not take as null.
        refused(["routing-settings", "set", "--clear", "auto_route_enabled"] + AUTH)
        print("OK")
    """, tmp_path))


def test_exports_download_saves_a_link_and_a_stream_without_the_credential(tmp_path):
    ok(_run(f"""
        out = {str(tmp_path / "bundle.zip")!r}
        n = len(SEEN)
        rc, o, e = run_main(["exports", "download", "j1", "--out", out] + AUTH)
        assert rc == 0, (rc, o, e)
        assert o == f"Saved 7 bytes to {{out}}.\\n", o
        assert Path(out).read_bytes() == b"ZIPDATA"
        api, blob = SEEN[n:]
        assert api[1] == "/v1/spaces/s/exports/j1/download" and api[4] == "Bearer " + KEY, api
        # The presigned link is fetched bare: the API key must not go to it.
        assert blob[1] == "/blob/j1" and blob[4] is None, blob
        assert not Path(out + ".part").exists()
        # A body that is the bundle itself.
        MODE["stream"] = True
        out2 = {str(tmp_path / "stream.zip")!r}
        rc, o, e = run_main(["exports", "download", "j1", "--out", out2, "--json"] + AUTH)
        assert rc == 0, (rc, o, e)
        assert one_json(o) == {{"job_id": "j1", "path": out2, "bytes": 7}}, o
        assert Path(out2).read_bytes() == b"ZIPDATA"
        print("OK")
    """, tmp_path))


def test_an_expired_download_link_is_exit_7_not_the_api_status(tmp_path):
    ok(_run(f"""
        for status in (403, 401, 404):
            MODE["blob"] = status
            out = {str(tmp_path / "gone.zip")!r}
            n = len(SEEN)
            rc, o, e = run_main(["exports", "download", "j1", "--out", out] + AUTH)
            # The storage host refused, not the API: transport class, and no
            # renewal or retry against the API.
            assert rc == 7, (status, rc, o, e)
            assert o == "", o
            assert e == f"anona: download link expired or refused (HTTP {{status}}); run download again.\\n", e
            assert [p for _, p, *_ in SEEN[n:]] == ["/v1/spaces/s/exports/j1/download", "/blob/j1"], SEEN[n:]
            assert not Path(out).exists()
        print("OK")
    """, tmp_path))


def test_exports_download_refuses_to_overwrite(tmp_path):
    ok(_run(f"""
        out = Path({str(tmp_path / "keep.zip")!r})
        out.write_bytes(b"PRECIOUS")
        err = refused(["exports", "download", "j1", "--out", str(out)] + AUTH)
        assert "already exists. Pass --yes to overwrite it. Nothing was sent." in err, err
        assert out.read_bytes() == b"PRECIOUS"
        rc, o, e = run_main(["exports", "download", "j1", "--out", str(out), "--yes"] + AUTH)
        assert rc == 0 and out.read_bytes() == b"ZIPDATA", (rc, o, e)
        print("OK")
    """, tmp_path))


def test_session_required_is_exit_4(tmp_path):
    ok(_run("""
        MODE["status"] = 401
        for argv in (["keys", "list"], ["org"], ["billing"], ["usage", "--history"], ["exports", "list", "--space", "s"]):
            n = len(SEEN)
            rc, out, err = run_main(argv + KEYED)
            assert rc == 4, (argv, rc, out, err)
            assert out == "", (argv, out)
            assert err.startswith("anona: session_required: "), (argv, err)
            assert "anona login --cli" in err, (argv, err)
            assert "Traceback" not in err
            # Not pre-refused: the server was asked, and decides.
            assert len(SEEN) == n + 1, (argv, SEEN[n:])
        print("OK")
    """, tmp_path))


def test_keys_list_never_prints_a_secret(tmp_path):
    ok(_run("""
        _, out, _ = sent(["keys", "list"] + KEYED)
        assert LEAK not in out and "LEAKEDSECRET" not in out, out
        assert out == "anona_live_ab\\u2026  ci  2026-10-01  -\\n", out
        # --json is the API's document as it came.
        _, out, _ = sent(["keys", "list", "--json"] + KEYED)
        assert one_json(out)[0]["key"] == LEAK
        print("OK")
    """, tmp_path))


def test_a_temporary_profile_reaches_the_space_verbs(tmp_path):
    ok(_run("""
        ANONA = Path.home() / ".anona"
        ANONA.mkdir(parents=True, exist_ok=True)
        (ANONA / "credentials.json").write_text(json.dumps({
            "access_token": "", "refresh_token": "", "expires_at": 0, "client_id": "",
            "base_url": BASE, "temp_token": "anona_tmp_TEMPTOKEN",
            "temp_expires_at": time.time() + 3600}))
        ROUTES.insert(0, ("GET", r"/v1/spaces/default/graph", 200,
                          {"nodes": [], "edges": [], "total_entities": 0, "total_edges": 0}))
        (m, p, q, b, auth), out, _ = sent(["graph", "--base-url", BASE])
        assert p == "/v1/spaces/default/graph" and auth == "Bearer anona_tmp_TEMPTOKEN", (p, auth)
        assert out == "0 entities, 0 links\\n", out
        print("OK")
    """, tmp_path))


def test_models_edit_merges_the_trigger_instead_of_replacing_it(tmp_path):
    ok(_run("""
        held = {**MODEL, "trigger": {"mode": "delta", "refresh_on_new_memories": True, "memory_type": ["fact"]}}
        ROUTES.insert(0, ("GET", r"/v1/spaces/s/models/mm-1", 200, held))
        n = len(SEEN)
        rc, out, err = run_main(["models", "edit", "mm-1", "--no-refresh-on-new-memories"] + AUTH)
        assert rc == 0 and out == "Updated memory model mm-1.\\n", (rc, out, err)
        assert [(m, p) for m, p, *_ in SEEN[n:]] == [("GET", "/v1/spaces/s/models/mm-1"),
                                                      ("PATCH", "/v1/spaces/s/models/mm-1")]
        assert SEEN[-1][3] == {"trigger": {"mode": "delta", "refresh_on_new_memories": False,
                                           "memory_type": ["fact"]}}, SEEN[-1][3]
        # A model with no trigger yet: only what was named, nothing invented.
        ROUTES.insert(0, ("GET", r"/v1/spaces/s/models/mm-1", 200, MODEL))
        rc, out, err = run_main(["models", "edit", "mm-1", "--mode", "delta", "--name", "N"] + AUTH)
        assert rc == 0 and SEEN[-1][3] == {"name": "N", "trigger": {"mode": "delta"}}, SEEN[-1][3]
        print("OK")
    """, tmp_path))


def test_models_create_with_a_null_model_id_reply_falls_back_to_the_flag(tmp_path):
    ok(_run("""
        ROUTES.insert(0, ("POST", r"/v1/spaces/s/models", 202, {"model_id": None, "job_id": "job-9", "status": "processing"}))
        _, out, _ = sent(["models", "create", "n", "--query", "q", "--model-id", "mine"] + AUTH)
        assert out == "Creating memory model mine (job job-9).\\n", out
        _, out, _ = sent(["models", "create", "n", "--query", "q"] + AUTH)
        assert out == "Creating memory model - (job job-9).\\n", out
        print("OK")
    """, tmp_path))


def test_exports_download_never_writes_through_a_planted_part_symlink(tmp_path):
    ok(_run(f"""
        victim = Path({str(tmp_path / "victim.txt")!r})
        victim.write_text("DO NOT TOUCH")
        out = Path({str(tmp_path / "bundle.zip")!r})
        Path(str(out) + ".part").symlink_to(victim)
        rc, o, e = run_main(["exports", "download", "j1", "--out", str(out)] + AUTH)
        assert rc == 0 and out.read_bytes() == b"ZIPDATA", (rc, o, e)
        assert victim.read_text() == "DO NOT TOUCH"
        assert o == f"Saved 7 bytes to {{out}}.\\n", o
        # Only the planted link is left beside the bundle; no temp file.
        assert sorted(p.name for p in out.parent.iterdir() if p.name.startswith(".anona-export-")) == []
        print("OK")
    """, tmp_path))


def test_exports_download_cleans_up_its_temp_file_when_the_body_breaks(tmp_path):
    ok(_run(f"""
        from anona.cli import resources
        out = {str(tmp_path / "half.zip")!r}
        def broken():
            yield b"ZIP"
            raise RuntimeError("connection lost")
        try:
            resources._save(out, broken())
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected the failure to propagate")
        left = [p.name for p in Path(out).parent.iterdir()]
        assert not any(n.startswith(".anona-export-") for n in left), left
        assert not Path(out).exists()
        print("OK")
    """, tmp_path))
