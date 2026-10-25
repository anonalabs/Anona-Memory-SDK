"""How `anona` is started and how it ends: `python -m anona.cli`, a closed
pipe, `anona help COMMAND`, the --base-url help, and two usage rules found
while documenting the CLI (record --async --batch; job cancel's route).
"""
from __future__ import annotations

import os
import subprocess
import sys

from _cli import SDK_ROOT, ok
from _cli_stub import run


def _env(tmp_path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANONA_") and k != "PYTHONOPTIMIZE"}
    env["HOME"] = str(tmp_path)
    return env


def _module(args, tmp_path, **kw) -> subprocess.CompletedProcess:
    # cwd is the SDK root, which `-m` puts first on sys.path: the public
    # `anona`, not anona-extensions (see _cli.py).
    return subprocess.run(
        [sys.executable, "-m", "anona.cli", *args],
        cwd=str(SDK_ROOT), env=_env(tmp_path), text=True, timeout=60, **kw,
    )


def test_python_dash_m_runs_the_cli(tmp_path):
    r = _module(["--version"], tmp_path, capture_output=True)
    assert r.returncode == 0 and r.stdout.startswith("anona "), (r.returncode, r.stdout, r.stderr)
    r = _module(["help"], tmp_path, capture_output=True)
    assert r.returncode == 0 and "usage: anona" in r.stdout, r.stderr


def _into_a_closed_pipe(args, tmp_path) -> subprocess.CompletedProcess:
    """Run with stdout on a pipe whose reader is already gone: the first write
    fails, deterministically, whatever the output's size."""
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        return _module(args, tmp_path, stdout=write_end, stderr=subprocess.PIPE)
    finally:
        os.close(write_end)


def test_a_closed_pipe_ends_quietly(tmp_path):
    for args in (["guide"], ["help", "--llm"], ["guide", "--json"], ["help", "record"], ["completion", "bash"],
                 ["--help"]):
        r = _into_a_closed_pipe(args, tmp_path)
        assert r.returncode == 141, (args, r.returncode, r.stderr)
        assert r.stderr == "", (args, r.stderr)


def test_help_command_prints_that_commands_help(tmp_path):
    ok(run("""
        for topic in (["record"], ["spaces", "delete"], ["reason-settings", "set"], ["spaces"]):
            rc, want, _ = run_main(topic + ["--help"])
            rc2, got, err = run_main(["help"] + topic)
            assert rc == 0 and rc2 == 0 and got == want and err == "", (topic, got[:200], err)
        for bad in (["nope"], ["spaces", "nope"], ["record", "extra"]):
            err = refused(["help"] + bad)
            assert "no command" in err, (bad, err)
        assert "Commands here: list, create, get, delete" in refused(["help", "spaces", "nope"])
        refused(["help", "record", "--llm"])
        refused(["help", "record", "--json"])
        # Bare `help` and `help --llm` are unchanged.
        rc, out, _ = run_main(["help"])
        assert rc == 0 and out.startswith("usage: anona"), out[:100]
        rc, out, _ = run_main(["help", "--llm"])
        assert rc == 0 and out.startswith("ANONA CLI REFERENCE"), out[:100]
        print("OK")
    """, tmp_path))


def test_base_url_help_states_both_defaults(tmp_path):
    ok(run("""
        from anona.cli import store, target
        for argv in (["--help"], ["record", "--help"], ["login", "--help"]):
            rc, out, _ = run_main(argv)
            flat = " ".join(out.split())
            assert target.DEFAULT_API_BASE_URL + " for a command using an API key" in flat, flat
            assert store.DEFAULT_BASE_URL + " for login and start" in flat, flat
        print("OK")
    """, tmp_path))


def test_record_async_with_batch_is_a_usage_error(tmp_path):
    ok(run("""
        f = Path.home() / "b.jsonl"; f.write_text('"a"\\n')
        err = refused(["record", "--batch", str(f), "--async"] + AUTH)
        assert "--batch is always queued; drop --async." in err, err
        print("OK")
    """, tmp_path))


def test_job_cancel_is_a_delete_of_the_job(tmp_path):
    # The gateway route is DELETE /v1/spaces/{id}/jobs/{job_id}; there is no
    # POST .../cancel.
    ok(run("""
        route("DELETE", "/v1/spaces/s/jobs/job-1", lambda r: envelope(409, "job_not_cancellable", "Already running."))
        reqs, out, err = sent(["jobs", "cancel", "job-1", "--yes"] + AUTH, want_rc=1)
        assert (reqs[0]["method"], reqs[0]["path"]) == ("DELETE", "/v1/spaces/s/jobs/job-1"), reqs
        assert err.strip() == "anona: job_not_cancellable: Already running. (request_id req_1)", err
        print("OK")
    """, tmp_path))
