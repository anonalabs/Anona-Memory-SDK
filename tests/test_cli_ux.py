"""The CLI's shared behaviour: exit codes, error rendering, retries and
timeouts, --quiet, named profiles, `anona doctor`, `anona guide` and
`anona completion`.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from _cli import ok, run_cli
from _cli_stub import run

# --- exit codes ---------------------------------------------------------------


def test_exit_codes_are_one_table(tmp_path):
    ok(run("""
        from anona.cli import exits
        codes = [c for c, _, _ in exits.EXIT_CODES]
        assert codes == sorted(set(codes)) == list(range(9)), codes
        f = exits.for_status
        assert [f(401), f(403), f(404), f(402), f(429), f(408), f(500), f(503), f(409), f(422)] == \
            [4, 4, 5, 6, 6, 7, 7, 7, 1, 1]
        assert f(200, transport=True) == 7
        print("OK")
    """, tmp_path))


def test_an_envelope_without_a_code_never_prints_a_python_dict(tmp_path):
    ok(run("""
        route("POST", "/v1/record", lambda r: reply(400, {"detail": {"why": "bad", "field": "content"}}))
        reqs, out, err = sent(["record", "x"] + AUTH, want_rc=1)
        assert "{'" not in err and "'}" not in err, err
        assert '"why": "bad"' in err, err
        route("POST", "/v1/record", lambda r: reply(400, {"detail": "Plain words."}))
        reqs, out, err = sent(["record", "x"] + AUTH, want_rc=1)
        assert err.strip() == "anona: the server answered HTTP 400: Plain words.", err
        print("OK")
    """, tmp_path))


# --- retries, timeouts, --quiet ----------------------------------------------


def test_a_write_is_never_resent_after_a_5xx_or_a_timeout(tmp_path):
    # A 5xx or a timeout on record may have stored the memory; re-sending
    # would store it twice.
    ok(run("""
        route("POST", "/v1/record", lambda r: envelope(503, "engine_unavailable"))
        reqs, out, err = sent(["record", "x", "--retries", "3"] + AUTH, want_rc=7, n_requests=1)
        def slow(r):
            time.sleep(1.5)
            return reply(201, BODIES["record_async"])
        route("POST", "/v1/record", slow)
        reqs, out, err = sent(["record", "x", "--timeout", "0.3", "--retries", "3"] + AUTH, want_rc=7, n_requests=1)
        assert "timed out" in err, err
        route("POST", "/v1/record/batch", lambda r: envelope(500, "internal_error"))
        f = Path.home() / "b.jsonl"; f.write_text('"a"\\n')
        sent(["record", "--batch", str(f), "--retries", "3"] + AUTH, want_rc=7, n_requests=1)
        print("OK")
    """, tmp_path))


def test_429_is_retried_with_retry_after_and_bounded_by_retries(tmp_path):
    ok(run("""
        state = {"n": 0}
        def limited(r):
            state["n"] += 1
            if state["n"] == 1:
                return reply(429, {"error": {"code": "rate_limited", "message": "slow down"}}, {"retry-after": "0"})
            return reply(201, BODIES["record_async"])
        route("POST", "/v1/record", limited)
        reqs, out, err = sent(["record", "x", "--async"] + AUTH, n_requests=2)
        assert "rate limited, retrying in 0 s (1/2)" in err, err
        route("POST", "/v1/record", lambda r: envelope(429, "rate_limited"))
        sent(["record", "x", "--retries", "0"] + AUTH, want_rc=6, n_requests=1)
        sent(["record", "x", "--retries", "1"] + AUTH, want_rc=6, n_requests=2)
        # A safe-to-repeat GET is retried on a 5xx; --quiet hides the notice.
        route("GET", "/v1/spaces/s/reason-settings", lambda r: envelope(503, "unavailable"))
        reqs, out, err = sent(["reason-settings", "get", "--retries", "2", "--quiet"] + AUTH, want_rc=7, n_requests=3)
        assert "retrying" not in err and err.startswith("anona: unavailable"), err
        refused(["record", "x", "--retries", "11"] + AUTH)
        refused(["record", "x", "--timeout", "0"] + AUTH)
        print("OK")
    """, tmp_path))


def test_quiet_drops_progress_but_never_the_result_or_the_error(tmp_path):
    ok(run("""
        route("POST", "/v1/reason", lambda r: reply(200, {"insights": "Tuesdays.", "sources": {}, "rules_applied": []}))
        reqs, out, err = sent(["reason", "when?"] + AUTH)
        assert "Reasoning over space" in err, err
        reqs, out, err = sent(["reason", "when?", "-q"] + AUTH)
        assert err == "" and out.strip() == "Tuesdays.", (out, err)
        route("POST", "/v1/reason", lambda r: envelope(404, "space_not_found"))
        reqs, out, err = sent(["--quiet", "reason", "when?"] + AUTH, want_rc=5)
        assert "space_not_found" in err, err
        print("OK")
    """, tmp_path))


# --- named profiles -----------------------------------------------------------


def test_a_profile_is_a_separate_credential_directory(tmp_path):
    ok(run("""
        from anona.cli import store
        (ANONA / "profiles" / "work").mkdir(parents=True)
        cfg = ANONA / "profiles" / "work" / "config.env"
        cfg.write_text("ANONA_API_KEY='%s'\\nANONA_BASE_URL='%s'\\nANONA_SPACE_ID='w'\\n" % (KEY, BASE))
        cfg.chmod(0o600)
        route("GET", "/v1/usage/me", lambda r: reply(200, {"credits_remaining": 1, "credits_limit": 2,
              "credits_used": 1, "rate_limit_per_min": 60}))
        route("GET", "/v1/spaces/w/memories", lambda r: reply(200, BODIES["memory_list"]))
        # Default profile: nothing configured, so nothing is sent.
        refused(["usage"], want=4)
        sent(["usage", "--profile", "work"])
        reqs, _, _ = sent(["--profile", "work", "memories", "list"])
        assert reqs[0]["path"] == "/v1/spaces/w/memories", reqs
        os.environ["ANONA_PROFILE"] = "work"
        sent(["usage"])
        os.environ["ANONA_PROFILE"] = "../escape"
        assert "ANONA_PROFILE" in refused(["usage"])
        del os.environ["ANONA_PROFILE"]
        refused(["usage", "--profile", "../x"])
        refused(["usage", "--profile", ""])
        # `default` is the plain directory.
        os.environ["ANONA_PROFILE"] = "default"
        assert store.anona_dir() == ANONA
        del os.environ["ANONA_PROFILE"]
        print("OK")
    """, tmp_path))


def test_a_profiles_locks_and_files_live_beside_its_own_credentials(tmp_path):
    # Every lock is `<file>.lock` / `<file>.nodelock` next to the file it
    # guards, so a profile shares nothing -- not even a lock -- with another,
    # and the default profile's files are where the Node CLI expects them.
    ok(run("""
        import time as _t
        from anona.cli import store, tokens
        os.environ["ANONA_PROFILE"] = "ci"
        c = store.Credentials(access_token="a", refresh_token="r", expires_at=_t.time() + 3600,
                              client_id="c", base_url=BASE)
        store.save(c, store.cli_path())
        with tokens.credential_lock(store.cli_path()):
            assert (ANONA / "profiles" / "ci" / "cli-credentials.json.nodelock").exists()
        assert (ANONA / "profiles" / "ci" / "cli-credentials.json").exists()
        assert not (ANONA / "cli-credentials.json").exists()
        assert oct((ANONA / "profiles" / "ci").stat().st_mode & 0o777) == "0o700"
        del os.environ["ANONA_PROFILE"]
        assert store.cli_path() == ANONA / "cli-credentials.json"
        assert store.path() == ANONA / "credentials.json"
        print("OK")
    """, tmp_path))


# --- doctor -------------------------------------------------------------------


def test_doctor_explains_the_credential_and_never_prints_it(tmp_path):
    ok(run("""
        health = []
        def h(r):
            health.append(r["auth"])
            return reply(200, {"status": "ok", "service": "Anona Memory API", "version": "1.0.0"})
        route("GET", "/health", h)
        route("GET", "/v1/spaces/notes", lambda r: reply(200, BODIES["space"]))
        os.environ["ANONA_API_KEY"] = KEY
        os.environ["ANONA_BASE_URL"] = BASE
        n = len(SEEN)
        rc, out, err = run_main(["doctor", "--space", "notes"])
        assert rc == 0, (rc, out, err)
        assert health == [None], "the health probe must carry no credential: %r" % health
        space_req = [r for r in SEEN[n:] if r["path"] == "/v1/spaces/notes"]
        assert len(space_req) == 1 and space_req[0]["auth"] == "Bearer " + KEY, SEEN[n:]
        assert "anona_live_SECR…" in out and "ANONA_API_KEY" in out, out
        assert "Why:" in out and "accessible" in out and "All good." in out, out
        rc, out, err = run_main(["doctor", "--space", "notes", "--json"])
        doc = one_json(out)
        assert rc == 0 and doc["ok"] is True, doc
        assert doc["credential"]["kind"] == "api_key" and doc["credential"]["display"] == "anona_live_SECR…", doc
        assert doc["health"]["ok"] is True and doc["space"]["accessible"] is True, doc
        print("OK")
    """, tmp_path))


def test_doctor_exit_code_names_the_first_problem(tmp_path):
    ok(run("""
        route("GET", "/health", lambda r: reply(200, {"status": "ok"}))
        # No credential at all: exit 4, and nothing is sent anywhere.
        n = len(SEEN)
        rc, out, err = run_main(["doctor"])
        assert rc == 4 and len(SEEN) == n and "Credential: none" in out, (rc, out, SEEN[n:])
        os.environ["ANONA_API_KEY"] = KEY
        os.environ["ANONA_BASE_URL"] = BASE
        route("GET", "/v1/spaces/gone", lambda r: envelope(404, "space_not_found"))
        rc, out, err = run_main(["doctor", "--space", "gone"])
        assert rc == 5 and "NOT accessible (space_not_found)" in out, (rc, out)
        rc, out, err = run_main(["doctor"])
        assert rc == 2 and "Space: none" in out, (rc, out)
        route("GET", "/health", lambda r: reply(503, {"status": "down"}))
        rc, out, err = run_main(["doctor", "--space", "gone"])
        assert rc == 7 and "Health: HTTP 503" in out, (rc, out)
        print("OK")
    """, tmp_path))


def test_doctor_reports_credits_and_an_empty_balance_is_a_problem(tmp_path):
    ok(run("""
        route("GET", "/health", lambda r: reply(200, {"status": "ok"}))
        route("GET", "/v1/spaces/notes", lambda r: reply(200, BODIES["space"]))
        os.environ["ANONA_API_KEY"] = KEY
        os.environ["ANONA_BASE_URL"] = BASE
        # A usage call that fails is an unknown balance, never a doctor failure.
        route("GET", "/v1/usage/me", lambda r: envelope(500, "internal_error"))
        rc, out, err = run_main(["doctor", "--space", "notes"])
        assert rc == 0 and "Credits: unknown (internal_error)." in out and "All good." in out, (rc, out, err)
        route("GET", "/v1/usage/me", lambda r: reply(200, {"credits_remaining": 0, "credits_limit": 1000,
                                                           "credits_used": 1000, "rate_limit_per_min": 60}))
        rc, out, err = run_main(["doctor", "--space", "notes"])
        assert rc == 6, (rc, out, err)
        assert "Credits: 0 remaining of 1000." in out and "credits exhausted" in out, out
        assert "All good." not in out, out
        rc, out, err = run_main(["doctor", "--space", "notes", "--json"])
        doc = one_json(out)
        assert rc == 6 and doc["ok"] is False, doc
        assert doc["credits"] == {"remaining": 0, "limit": 1000, "used": 1000, "exhausted": True, "error": None}, doc
        print("OK")
    """, tmp_path))


def test_doctor_names_a_shadowed_login(tmp_path):
    ok(run("""
        import time as _t
        from anona.cli import store
        store.save(store.Credentials(access_token="a", refresh_token="r", expires_at=_t.time() + 3600,
                                     client_id="c", base_url=BASE), store.cli_path())
        route("GET", "/health", lambda r: reply(200, {"status": "ok"}))
        os.environ["ANONA_API_KEY"] = KEY
        os.environ["ANONA_BASE_URL"] = BASE
        rc, out, err = run_main(["doctor", "--json"])
        doc = one_json(out)
        assert any("command-line login" in s for s in doc["credential"]["shadowed"]), doc
        print("OK")
    """, tmp_path))


# --- guide --------------------------------------------------------------------

_GUIDE = """
from anona.cli import introspect, guide, exits
import shlex
parser = cli_main._parser()
leaves = introspect.tree(parser).leaves()
names = [c.name for c in leaves]
rc, text, err = run_main(["guide"])
assert rc == 0 and err == "", (rc, err)
rc, js, err = run_main(["guide", "--json"])
doc = json.loads(js)

def json_leaves(cmds):
    out = []
    for c in cmds:
        out.extend(json_leaves(c["subcommands"]) if c["subcommands"] else [c])
    return out
"""


def test_guide_lists_every_command_in_text_and_json(tmp_path):
    # The drift guard: a subcommand registered on the parser and missing from
    # either form of the guide fails here.
    ok(run(_GUIDE + """
sub = [a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction"][0]
registered = set(sub.choices)
for name in registered:
    assert ("  anona " + name) in text, ("missing from the text guide", name)
for c in leaves:
    assert ("  " + introspect.usage(c)) in text, ("missing from the text guide", c.name)
    for opt in c.options:
        assert opt.flags[0] in text
assert {c["path"][0] for c in doc["commands"]} == registered, (sorted(registered),)
jl = json_leaves(doc["commands"])
assert [c["name"] for c in jl] == names, ([c["name"] for c in jl], names)
for c, j in zip(leaves, jl):
    assert [o["flags"] for o in j["options"]] == [o.flags for o in c.options], c.name
assert [e["code"] for e in doc["exit_codes"]] == [c for c, _, _ in exits.EXIT_CODES]
for code, name, _ in exits.EXIT_CODES:
    assert f"  {code}  {name}:" in text, name
print("OK")
""", tmp_path))


def test_every_command_has_a_parsing_example_and_an_output_shape(tmp_path):
    ok(run(_GUIDE + """
assert sorted(guide.EXAMPLES) == sorted(names), sorted(set(names) ^ set(guide.EXAMPLES))
assert sorted(guide.JSON_SHAPES) == sorted(names), sorted(set(names) ^ set(guide.JSON_SHAPES))
for name, example in guide.EXAMPLES.items():
    argv = shlex.split(example)
    assert argv[0] == "anona", example
    ns = parser.parse_args(argv[1:])
    got = [ns.command] + ([ns.action] if getattr(ns, "action", None) else [])
    assert " ".join(got) == name, (name, example, got)
for j in json_leaves(doc["commands"]):
    assert j["example"] and j["json_output"], j["name"]
print("OK")
""", tmp_path))


def test_help_llm_is_the_guide(tmp_path):
    ok(run(_GUIDE + """
rc, out, err = run_main(["help", "--llm"])
assert rc == 0 and out == text, "help --llm differs from guide"
rc, out, err = run_main(["help", "--llm", "--json"])
assert out == js
rc, out, err = run_main(["help"])
assert rc == 0 and "usage: anona" in out
assert run_main(["help", "--json"])[0] == 2
print("OK")
""", tmp_path))


# --- completion ---------------------------------------------------------------


def _script(tmp_path, shell: str) -> str:
    result = run_cli(
        "from anona.cli import main\nimport sys\nsys.exit(main.main(['completion', %r]))\n" % shell,
        env={"HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _complete(script_path, words: list[str]) -> list[str]:
    line = " ".join(words)
    cmd = (
        f"source {script_path}; COMP_WORDS=({line}); COMP_CWORD={len(words) - 1}; "
        "_anona; printf '%s\\n' \"${COMPREPLY[@]}\""
    )
    out = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return out.stdout.split()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_bash_completion_offers_every_command_and_flag(tmp_path):
    script = _script(tmp_path, "bash")
    path = tmp_path / "anona.bash"
    path.write_text(script)
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
    tree = json.loads(
        run_cli(
            "from anona.cli import main\nmain.main(['guide', '--json'])\n", env={"HOME": str(tmp_path)}
        ).stdout
    )
    top = {c["path"][0] for c in tree["commands"]}
    assert set(_complete(path, ["anona", "''"])) == top
    assert set(_complete(path, ["anona", "spaces", "''"])) == {"list", "create", "get", "delete"}
    assert set(_complete(path, ["anona", "reason-settings", "''"])) == {"get", "set", "reset"}
    flags = _complete(path, ["anona", "record", "--"])
    for flag in ("--async", "--batch", "--wait", "--wait-timeout", "--space", "--json", "--profile", "--quiet"):
        assert flag in flags, (flag, flags)
    assert "--clear" in _complete(path, ["anona", "reason-settings", "set", "--"])
    assert "--clear" not in _complete(path, ["anona", "reason-settings", "get", "--"])


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not installed")
def test_zsh_completion_parses(tmp_path):
    path = tmp_path / "_anona"
    path.write_text(_script(tmp_path, "zsh"))
    assert subprocess.run(["zsh", "-n", str(path)]).returncode == 0


def test_fish_completion_names_every_command(tmp_path):
    script = _script(tmp_path, "fish")
    for word in ("record", "documents", "upload", "reason-settings", "doctor", "guide", "completion"):
        assert f"-a '{word}'" in script, word
    assert "-l 'wait-timeout'" in script
    if shutil.which("fish"):
        path = tmp_path / "anona.fish"
        path.write_text(script)
        assert subprocess.run(["fish", "--no-execute", str(path)]).returncode == 0
