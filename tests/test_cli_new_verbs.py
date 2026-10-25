"""The CLI verbs added for the gateway routes it lacked.

record --async / --batch / --wait, retrieve's compound filters and block
format, memories list filters, spaces delete, documents, the three space
settings and llm-models. Every response is a gateway model's own body
(_cli_gateway_bodies.py), and every case asserts what reached the server --
or that nothing did.
"""
from __future__ import annotations

from _cli import ok
from _cli_stub import run


def test_record_async_queues_and_prints_the_job(tmp_path):
    ok(run("""
        route("POST", "/v1/record", lambda r: reply(202, BODIES["record_async"]))
        reqs, out, err = sent(["record", "The team ships on Tuesdays.", "--async"] + AUTH)
        body = reqs[0]["body"]
        assert body["async"] is True and body["space_id"] == "s", body
        assert "job-rec-1" in out and "anona jobs get job-rec-1" in out, out
        reqs, out, err = sent(["record", "x", "--async", "--json"] + AUTH)
        assert one_json(out) == BODIES["record_async"], out
        # Without --async nothing about async is sent.
        route("POST", "/v1/record", lambda r: reply(201, {**BODIES["record_async"], "job_id": None,
              "status": "stored", "memory_id": "m1", "memory_ids": ["m1"]}))
        reqs, out, err = sent(["record", "x"] + AUTH)
        assert "async" not in reqs[0]["body"], reqs[0]["body"]
        print("OK")
    """, tmp_path))


def test_record_async_wait_polls_to_the_outcome(tmp_path):
    ok(run("""
        import anona.cli.jobs as jobs
        jobs.FIRST_INTERVAL = 0.01
        route("POST", "/v1/record", lambda r: reply(202, BODIES["record_async"]))
        state = {"polls": 0}
        def job(r):
            state["polls"] += 1
            return reply(200, BODIES["job_processing"] if state["polls"] < 2 else BODIES["job_completed"])
        route("GET", "/v1/spaces/s/jobs/job-rec-1", job)
        reqs, out, err = sent(["record", "x", "--async", "--wait"] + AUTH, n_requests=3)
        assert [r["method"] for r in reqs] == ["POST", "GET", "GET"], reqs
        assert "completed, 2 memories" in out and "mem-a, mem-b" in out, out

        state["polls"] = 5
        reqs, out, err = sent(["record", "x", "--async", "--wait", "--json"] + AUTH, n_requests=2)
        doc = one_json(out)
        assert doc == {"submitted": BODIES["record_async"], "jobs": [BODIES["job_completed"]]}, doc

        # Finished, nothing stored: the same exit 3 a synchronous record gives.
        route("GET", "/v1/spaces/s/jobs/job-rec-1", lambda r: reply(200, BODIES["job_completed_empty"]))
        reqs, out, err = sent(["record", "x", "--async", "--wait"] + AUTH, want_rc=3, n_requests=2)
        assert "nothing was stored" in err, err

        route("GET", "/v1/spaces/s/jobs/job-rec-1", lambda r: reply(200, BODIES["job_failed"]))
        reqs, out, err = sent(["record", "x", "--async", "--wait"] + AUTH, want_rc=1, n_requests=2)
        assert "Extraction failed." in out, out

        # Still running when the wait runs out: exit 8, polled exactly once.
        route("GET", "/v1/spaces/s/jobs/job-rec-1", lambda r: reply(200, BODIES["job_processing"]))
        reqs, out, err = sent(["record", "x", "--async", "--wait", "--wait-timeout", "0"] + AUTH,
                              want_rc=8, n_requests=2)
        assert "still running" in err, err
        print("OK")
    """, tmp_path))


def test_record_wait_flags_need_something_to_wait_on(tmp_path):
    ok(run("""
        refused(["record", "x", "--wait"] + AUTH)
        refused(["record", "x", "--async", "--wait-timeout", "5"] + AUTH)
        refused(["record", "x", "--async", "--wait", "--wait-timeout", "-1"] + AUTH)
        print("OK")
    """, tmp_path))


def test_record_batch_sends_the_file_as_items(tmp_path):
    ok(run("""
        import anona.cli.jobs as jobs
        jobs.FIRST_INTERVAL = 0.01
        f = Path.home() / "items.jsonl"
        f.write_text('{"content": "Alice owns billing.", "tags": ["team:billing"]}\\n'
                     '\\n'
                     '"Bob owns search."\\n')
        route("POST", "/v1/record/batch", lambda r: reply(202, BODIES["record_batch"]))
        reqs, out, err = sent(["record", "--batch", str(f), "--user-id", "u1"] + AUTH)
        body = reqs[0]["body"]
        assert body == {"space_id": "s", "items": [
            {"content": "Alice owns billing.", "tags": ["team:billing"]},
            {"content": "Bob owns search."}], "user_id": "u1"}, body
        assert "job-bat-1" in out and "job-bat-2" in out, out
        # stdin, and --wait polls every job the batch returned.
        route("GET", "/v1/spaces/s/jobs/job-bat-[12]", lambda r: reply(200, BODIES["job_completed"]))
        reqs, out, err = sent(["record", "--batch", "-", "--wait", "--json"] + AUTH, n_requests=3,
                              stdin='"one"\\n"two"\\n')
        assert [r["path"] for r in reqs[1:]] == ["/v1/spaces/s/jobs/job-bat-1", "/v1/spaces/s/jobs/job-bat-2"], reqs
        doc = one_json(out)
        assert doc["submitted"] == BODIES["record_batch"] and len(doc["jobs"]) == 2, doc
        print("OK")
    """, tmp_path))


def test_a_bad_batch_file_sends_nothing(tmp_path):
    ok(run("""
        f = Path.home() / "bad.jsonl"
        for text, needle in (
            ('{"content": "a", "colour": "red"}\\n', "unknown field"),
            ('{"content": ""}\\n', "non-empty"),
            ("not json\\n", "not valid JSON"),
            ("[1, 2]\\n", "JSON object"),
            ("\\n\\n", "no items"),
            ("".join('"m%d"\\n' % i for i in range(101)), "at most 100"),
        ):
            f.write_text(text)
            err = refused(["record", "--batch", str(f)] + AUTH)
            assert needle in err, (needle, err)
        f.write_text('"fine"\\n')
        assert "not both" in refused(["record", "x", "--batch", str(f)] + AUTH)
        assert "--tags" in refused(["record", "--batch", str(f), "--tags", "a"] + AUTH)
        refused(["record", "--batch", str(Path.home() / "missing.jsonl")] + AUTH)
        print("OK")
    """, tmp_path))


def test_retrieve_compound_filters_reach_the_body(tmp_path):
    ok(run("""
        route("POST", "/v1/retrieve", lambda r: reply(200, BODIES["search_results"]))
        groups = '[{"or": [{"tags": ["project:alpha"]}, {"tags": ["project:beta"]}]}, {"not": {"tags": ["status:archived"]}}]'
        reqs, out, err = sent(["retrieve", "ship", "--tag-groups", groups, "--member-id", "me",
                               "--no-prefer-observations", "--user-id", "u"] + AUTH)
        body = reqs[0]["body"]
        assert body["tag_groups"] == json.loads(groups), body
        assert body["member_id"] == "me" and body["prefer_observations"] is False, body
        assert body["user_id"] == "u", body
        assert "The team ships on Tuesdays." in out, out
        # One object is a one-element array; @FILE reads it.
        reqs, _, _ = sent(["retrieve", "ship", "--tag-groups", '{"tags": ["a"], "match": "any"}'] + AUTH)
        assert reqs[0]["body"]["tag_groups"] == [{"tags": ["a"], "match": "any"}], reqs[0]["body"]
        g = Path.home() / "groups.json"
        g.write_text(groups)
        reqs, _, _ = sent(["retrieve", "ship", "--tag-groups", "@" + str(g)] + AUTH)
        assert reqs[0]["body"]["tag_groups"] == json.loads(groups)
        # Unset flags are not sent at all.
        reqs, _, _ = sent(["retrieve", "ship"] + AUTH)
        for k in ("tag_groups", "member_id", "prefer_observations", "format"):
            assert k not in reqs[0]["body"], (k, reqs[0]["body"])
        for bad in ("not json", "[]", "[1]", '"s"'):
            refused(["retrieve", "ship", "--tag-groups", bad] + AUTH)
        print("OK")
    """, tmp_path))


def test_retrieve_block_format(tmp_path):
    ok(run("""
        route("POST", "/v1/retrieve", lambda r: reply(200, BODIES["search_block"]))
        reqs, out, err = sent(["retrieve", "ship", "--format", "block", "--max-tokens", "500",
                               "--tags", "a"] + AUTH)
        body = reqs[0]["body"]
        assert body["format"] == "block" and body["context_max_tokens"] == 500, body
        assert body["tags"] == ["a"] and body["limit"] == 10 and body["mode"] == "accurate", body
        # The block itself, line breaks kept, so it pipes into a prompt.
        assert out == BODIES["search_block"]["context"] + "\\n", repr(out)
        reqs, out, err = sent(["retrieve", "ship", "--format", "block", "--json"] + AUTH)
        assert one_json(out) == BODIES["search_block"], out
        assert "context_max_tokens" not in reqs[0]["body"]
        refused(["retrieve", "ship", "--max-tokens", "50"] + AUTH)
        print("OK")
    """, tmp_path))


def test_memories_list_member_and_tag_groups(tmp_path):
    ok(run("""
        route("GET", "/v1/spaces/s/memories", lambda r: reply(200, BODIES["memory_list"]))
        groups = '[{"not": {"tags": ["status:archived"]}}]'
        reqs, out, err = sent(["memories", "list", "--member-id", "me", "--tag-groups", groups] + AUTH)
        q = reqs[0]["query"]
        assert q["member_id"] == "me", q
        assert json.loads(q["tag_groups"]) == json.loads(groups), q
        assert q["limit"] == "50" and q["offset"] == "0", q
        assert "mem-0001-aaaa" in out, out
        reqs, out, err = sent(["memories", "list"] + AUTH)
        assert reqs[0]["query"] == {"limit": "50", "offset": "0"}, reqs[0]["query"]
        print("OK")
    """, tmp_path))


def test_spaces_delete_needs_yes(tmp_path):
    ok(run("""
        route("DELETE", "/v1/spaces/old", lambda r: reply(204))
        err = refused(["spaces", "delete", "old"] + KEYED)
        assert "--yes" in err and "old" in err, err
        reqs, out, err = sent(["spaces", "delete", "old", "--yes"] + KEYED)
        assert reqs[0]["method"] == "DELETE" and reqs[0]["path"] == "/v1/spaces/old", reqs
        assert "Deleted space 'old'" in out, out
        reqs, out, err = sent(["spaces", "delete", "old", "--yes", "--json"] + KEYED)
        assert one_json(out) == {"deleted": True, "space_id": "old"}, out
        route("DELETE", "/v1/spaces/old", lambda r: envelope(403, "space_owner_only"))
        reqs, out, err = sent(["spaces", "delete", "old", "--yes"] + KEYED, want_rc=4)
        assert err.strip() == "anona: space_owner_only: m (request_id req_1)", err
        print("OK")
    """, tmp_path))


def test_documents_upload_sends_every_file_and_the_scope(tmp_path):
    ok(run("""
        import anona.cli.jobs as jobs
        jobs.FIRST_INTERVAL = 0.01
        a = Path.home() / "a.md"; a.write_text("# Alpha notes")
        b = Path.home() / "b.txt"; b.write_text("Beta notes")
        route("POST", "/v1/spaces/s/documents", lambda r: reply(202, BODIES["upload"]))
        reqs, out, err = sent(["documents", "upload", str(a), str(b), "--user-id", "u1",
                               "--tags", "q3,notes"] + AUTH)
        r = reqs[0]
        assert r["ctype"].startswith("multipart/form-data"), r["ctype"]
        raw = r["body"]
        assert raw.count(b'name="files"') == 2, raw
        assert b'filename="a.md"' in raw and b'filename="b.txt"' in raw, raw
        assert b"# Alpha notes" in raw and b"Beta notes" in raw, raw
        assert b'name="user_id"' in raw and b"u1" in raw, raw
        assert b'name="tags"' in raw and b"q3,notes" in raw, raw
        assert b'name="agent_id"' not in raw, raw
        assert "job-up-1" in out, out
        # --wait polls the upload's job; an upload has no item count, so a
        # completed job is success whatever its memory count.
        route("GET", "/v1/spaces/s/jobs/job-up-1", lambda r: reply(200, BODIES["job_completed_empty"]))
        reqs, out, err = sent(["documents", "upload", str(a), "--wait", "--json"] + AUTH, n_requests=2)
        assert one_json(out)["jobs"] == [BODIES["job_completed_empty"]], out
        print("OK")
    """, tmp_path))


def test_documents_upload_surfaces_413_and_415_and_is_never_retried(tmp_path):
    ok(run("""
        a = Path.home() / "a.exe"; a.write_bytes(b"MZ")
        for status, code, rc in ((415, "unsupported_file_type", 1), (413, "upload_too_large", 1),
                                 (429, "rate_limited", 6), (503, "engine_unavailable", 7)):
            route("POST", "/v1/spaces/s/documents", lambda r, s=status, c=code: envelope(s, c, "Not that."))
            reqs, out, err = sent(["documents", "upload", str(a)] + AUTH, want_rc=rc)
            assert err.strip() == f"anona: {code}: Not that. (request_id req_1)", err
            assert out == "", out
        err = refused(["documents", "upload", str(Path.home() / "missing.pdf")] + AUTH)
        assert "not a readable file" in err, err
        refused(["documents", "upload", str(a), "--wait-timeout", "3"] + AUTH)
        print("OK")
    """, tmp_path))


def test_documents_list_get_delete(tmp_path):
    ok(run("""
        route("GET", "/v1/spaces/s/documents", lambda r: reply(200, BODIES["document_list"]))
        route("GET", "/v1/spaces/s/documents/doc-1", lambda r: reply(200, {"id": "doc-1", "text_length": 12}))
        route("DELETE", "/v1/spaces/s/documents/doc-1", lambda r: reply(204))
        reqs, out, err = sent(["documents", "list", "--source", "file"] + AUTH)
        assert reqs[0]["query"] == {"limit": "100", "offset": "0", "source": "file"}, reqs[0]["query"]
        assert "doc-1" in out and "3 memories" in out, out
        reqs, out, err = sent(["documents", "get", "doc-1"] + AUTH)
        assert "text_length: 12" in out, out
        assert "doc-1" in refused(["documents", "delete", "doc-1"] + AUTH)
        reqs, out, err = sent(["documents", "delete", "doc-1", "--yes", "--json"] + AUTH)
        assert one_json(out) == {"deleted": True, "document_id": "doc-1", "space_id": "s"}, out
        print("OK")
    """, tmp_path))


def test_reason_settings_set_keeps_what_it_was_not_told_to_change(tmp_path):
    # The PUT is a full replace: sending only the model would clear the depth.
    ok(run("""
        stored = dict(BODIES["reason_settings"])
        def put(r):
            stored.update(r["body"])
            return reply(200, {**stored, "space_id": "s"})
        route("GET", "/v1/spaces/s/reason-settings", lambda r: reply(200, stored))
        route("PUT", "/v1/spaces/s/reason-settings", put)
        reqs, out, err = sent(["reason-settings", "set", "--model", "fast"] + AUTH, n_requests=2)
        assert [r["method"] for r in reqs] == ["GET", "PUT"], reqs
        body = reqs[1]["body"]
        assert sorted(body) == sorted(BODIES["reason_settings_fields"]), body
        assert body == {"model": "fast", "depth": "thorough"}, body
        assert "depth: thorough" in out, out
        reqs, out, err = sent(["reason-settings", "set", "--clear", "depth", "--json"] + AUTH, n_requests=2)
        assert reqs[1]["body"] == {"model": "fast", "depth": None}, reqs[1]["body"]
        assert one_json(out)["depth"] is None
        refused(["reason-settings", "set"] + AUTH)
        refused(["reason-settings", "set", "--depth", "fast", "--clear", "depth"] + AUTH)
        refused(["reason-settings", "set", "--depth", "slow"] + AUTH)
        print("OK")
    """, tmp_path))


def test_chat_and_extraction_settings_set_read_modify_write(tmp_path):
    ok(run("""
        route("GET", "/v1/spaces/s/chat-settings", lambda r: reply(200, BODIES["chat_settings"]))
        route("PUT", "/v1/spaces/s/chat-settings", lambda r: reply(200, {**r["body"], "space_id": "s"}))
        reqs, out, err = sent(["chat-settings", "set", "--memory-limit", "3", "--no-auto-record"] + AUTH, n_requests=2)
        body = reqs[1]["body"]
        want = {k: v for k, v in BODIES["chat_settings"].items() if k != "space_id"}
        want.update(memory_limit=3, auto_record=False)
        assert body == want and sorted(body) == sorted(BODIES["chat_settings_fields"]), body
        refused(["chat-settings", "set", "--memory-limit", "21"] + AUTH)

        route("GET", "/v1/spaces/s/extraction-settings", lambda r: reply(200, BODIES["extraction_settings"]))
        route("PUT", "/v1/spaces/s/extraction-settings", lambda r: reply(200, {**r["body"], "space_id": "s"}))
        labels = Path.home() / "labels.json"
        labels.write_text('[{"key": "name", "type": "multi-text", "tag": true}]')
        reqs, out, err = sent(["extraction-settings", "set", "--clear", "guidance", "--labels-file", str(labels)] + AUTH,
                              n_requests=2)
        body = reqs[1]["body"]
        want = {k: v for k, v in BODIES["extraction_settings"].items() if k != "space_id"}
        want.update(guidance=None, labels=[{"key": "name", "type": "multi-text", "tag": True}])
        assert body == want and sorted(body) == sorted(BODIES["extraction_settings_fields"]), body
        print("OK")
    """, tmp_path))


def test_settings_get_and_reset(tmp_path):
    ok(run("""
        for kind in ("chat-settings", "reason-settings", "extraction-settings"):
            key = kind.replace("-", "_")
            route("GET", f"/v1/spaces/s/{kind}", lambda r, k=key: reply(200, BODIES[k]))
            route("DELETE", f"/v1/spaces/s/{kind}", lambda r: reply(204))
            reqs, out, err = sent([kind, "get", "--json"] + AUTH)
            assert one_json(out) == BODIES[key], out
            reqs, out, err = sent([kind, "get"] + AUTH)
            assert out.startswith(f"{kind} for space 's':"), out
            err = refused([kind, "reset"] + AUTH)
            assert "--yes" in err and kind in err, err
            reqs, out, err = sent([kind, "reset", "--yes"] + AUTH)
            assert reqs[0]["method"] == "DELETE", reqs
        print("OK")
    """, tmp_path))


def test_llm_models_lists_the_catalog(tmp_path):
    ok(run("""
        route("GET", "/v1/models", lambda r: reply(200, BODIES["llm_models"]))
        reqs, out, err = sent(["llm-models"] + KEYED)
        for m in BODIES["llm_models"]["data"]:
            assert m["id"] in out, (m["id"], out)
        reqs, out, err = sent(["llm-models", "--json"] + KEYED)
        assert one_json(out) == BODIES["llm_models"]
        print("OK")
    """, tmp_path))
