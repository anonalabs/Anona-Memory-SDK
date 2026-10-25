"""Inputs and server values the CLI used to pass through unchecked.

A space name reaching the terminal raw, an empty scope or space sent as if
it meant something, a non-string answer crashing `reason`, and a JSON `true`
read as the integer 1.
"""
from __future__ import annotations

from _cli import ok
from _cli_stub import run


def test_a_space_name_never_reaches_the_terminal_raw(tmp_path):
    # It comes from --space, ANONA_SPACE_ID or config.env -- text an agent
    # may have copied out of a page it read.
    ok(run("""
        evil = "s\\x1b[2Jx"
        enc = "s%1B%5B2Jx"
        route("POST", "/v1/record", lambda r: reply(201, {**BODIES["record_async"], "job_id": None,
              "status": "stored", "memory_id": "m1", "memory_ids": ["m1"]}))
        reqs, out, err = sent(["record", "fact", "--space", evil] + KEYED)
        assert "Stored 1 memory in space 's [2Jx'" in out and "\\x1b" not in out + err, (out, err)
        route("POST", "/v1/record", lambda r: reply(202, BODIES["record_async"]))
        reqs, out, err = sent(["record", "fact", "--async", "--space", evil] + KEYED)
        assert "Queued in space 's [2Jx'" in out and "\\x1b" not in out + err, (out, err)
        # An older server that queues with no job id: the other "Queued" line.
        route("POST", "/v1/record", lambda r: reply(202, {**BODIES["record_async"], "job_id": None}))
        reqs, out, err = sent(["record", "fact", "--space", evil] + KEYED)
        assert "Queued in space 's [2Jx'" in out and "\\x1b" not in out + err, (out, err)
        route("POST", "/v1/reason", lambda r: reply(200, {"insights": "ok", "sources": {}, "rules_applied": []}))
        reqs, out, err = sent(["reason", "q", "--space", evil] + KEYED)
        assert "Reasoning over space 's [2Jx'" in err and "\\x1b" not in out + err, (out, err)
        os.environ["ANONA_SPACE_ID"] = evil
        route("POST", "/v1/record", lambda r: reply(202, BODIES["record_async"]))
        reqs, out, err = sent(["record", "fact", "--async"] + KEYED)
        assert "\\x1b" not in out + err, out
        print("OK")
    """, tmp_path))


def test_an_empty_scope_is_a_usage_error_everywhere(tmp_path):
    ok(run("""
        f = Path.home() / "a.md"; f.write_text("x")
        for flag in ("--user-id", "--agent-id", "--session-id"):
            for value in ("", "  "):
                for argv in (["record", "x"], ["retrieve", "x"], ["reason", "x"], ["memories", "list"],
                             ["documents", "upload", str(f)]):
                    err = refused(argv + [flag, value] + AUTH)
                    assert "must not be empty" in err, (argv, flag, err)
        refused(["user-profile", ""] + AUTH)
        print("OK")
    """, tmp_path))


def test_an_empty_space_is_a_usage_error_even_on_a_temp_profile(tmp_path):
    # It reached the SDK as no space at all, which raised route_conflict and
    # exited 1 as though a request had failed.
    ok(run("""
        ANONA.mkdir(parents=True, exist_ok=True)
        (ANONA / "credentials.json").write_text(json.dumps({
            "access_token": "", "refresh_token": "", "expires_at": 0, "client_id": "",
            "base_url": BASE, "temp_token": "anona_tmp_TEMPTOKEN", "temp_expires_at": time.time() + 3600}))
        for argv in (["record", "x"], ["retrieve", "x"], ["reason", "x"], ["memories", "list"],
                     ["reason-settings", "get"], ["doctor"]):
            err = refused(argv + ["--space", "", "--base-url", BASE])
            assert "must not be empty" in err, (argv, err)
        refused(["record", "x", "--space", " "] + KEYED)
        print("OK")
    """, tmp_path))


def test_reason_prints_a_non_string_answer_instead_of_crashing(tmp_path):
    ok(run("""
        for insights, want in ((42, "42"), (True, "True"), ({"a": 1}, '{"a": 1}'), (["x"], '["x"]')):
            route("POST", "/v1/reason", lambda r, i=insights: reply(200, {"insights": i, "sources": {}, "rules_applied": []}))
            reqs, out, err = sent(["reason", "q"] + AUTH)
            assert out.strip() == want, (insights, out, err)
        route("POST", "/v1/reason", lambda r: reply(200, {"insights": None, "sources": {}, "rules_applied": []}))
        reqs, out, err = sent(["reason", "q"] + AUTH)
        assert out.startswith("No answer."), out
        print("OK")
    """, tmp_path))


def test_a_json_true_is_not_a_number(tmp_path):
    ok(run("""
        page = {**BODIES["memory_list"], "offset": True}
        route("GET", "/v1/spaces/s/memories", lambda r: reply(200, page))
        reqs, out, err = sent(["memories", "list"] + AUTH)
        assert "Showing 1-1 of 1." in out, out
        hit = {**BODIES["search_results"]}
        hit["results"] = [{**hit["results"][0], "relevance_score": True}]
        route("POST", "/v1/retrieve", lambda r: reply(200, hit))
        reqs, out, err = sent(["retrieve", "q"] + AUTH)
        assert out.startswith("1. -  mem-0001"), out
        print("OK")
    """, tmp_path))
