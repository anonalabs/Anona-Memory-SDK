"""`record`, `retrieve` and `reason` with an API key.

The `anona login` token is MCP-only and stays that way; an account reaches
these three verbs with an API key instead. Every case runs a real local HTTP
server and asserts on what reached it -- which bearer, which space, or that
nothing arrived at all -- so a refusal that still sent the request, or a
precedence rule that picked the wrong key, fails here. Runs in a subprocess
(see _cli.py) so `import anona` is the public SDK.
"""
from __future__ import annotations

import os
import subprocess
import textwrap

from _cli import SDK_ROOT, ok, run_cli

# This repo's root IS the package, so skills/ sits beside it.
INSTALLER = SDK_ROOT / "skills" / "install.sh"

PRELUDE = """
import io, json, os, sys, time, threading
from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from anona.cli import main as cli_main, target

for _name in ("ANONA_BASE_URL", "ANONA_API_KEY", "ANONA_SPACE_ID"):
    os.environ.pop(_name, None)

SEEN = []
MODE = {"status": 200}

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        SEEN.append((self.path, self.headers.get("authorization"), body))
        status = MODE["status"]
        if status == 401:
            out = {"error": {"code": "invalid_api_key", "message": "Invalid API key."}}
        elif status >= 400:
            out = {"error": {"code": "internal_error", "message": "Something broke."}}
        elif self.path.endswith("reason"):
            out = {"insights": "An answer."}
        elif self.path.endswith("retrieve"):
            out = {"results": [{"content": "the user prefers pnpm", "memory_id": "m1"}]}
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
ANONA = Path.home() / ".anona"

def temp_profile(base=None, signed_in=False, temp=True):
    ANONA.mkdir(parents=True, exist_ok=True)
    c = {
        "access_token": "oauth_MCP_ONLY" if signed_in else "",
        "refresh_token": "r" if signed_in else "",
        "expires_at": time.time() + 3600 if signed_in else 0,
        "client_id": "c" if signed_in else "",
        "base_url": base or BASE,
    }
    if temp:
        c["temp_token"] = "anona_tmp_TEMPTOKEN"
        c["temp_expires_at"] = time.time() + 3600
    (ANONA / "credentials.json").write_text(json.dumps(c))

def config_env(text, mode=0o600):
    ANONA.mkdir(parents=True, exist_ok=True)
    p = ANONA / "config.env"
    p.write_text(text)
    os.chmod(p, mode)
    return p

def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = cli_main.main(argv)
        except SystemExit as exc:
            rc = exc.code
    return rc, out.getvalue(), err.getvalue()

def auths():
    return [a for _, a, _ in SEEN]
"""


def _run(body, tmp_path, env=None, timeout=30):
    return run_cli(PRELUDE + textwrap.dedent(body), env={"HOME": str(tmp_path), **(env or {})}, timeout=timeout)


# --- precedence: flag > env > config.env > temp token -----------------------


def test_flag_beats_env_beats_config_env_beats_temp_token(tmp_path):
    ok(_run("""
        temp_profile()
        config_env("ANONA_API_KEY='anona_live_FROMFILE'\\nANONA_BASE_URL='%s'\\n" % BASE)

        # config.env beats the stored temp token.
        rc, out, err = run_main(["record", "x", "--space", "s"])
        assert rc == 0, (rc, err)
        assert auths()[-1] == "Bearer anona_live_FROMFILE", auths()

        # env beats config.env.
        os.environ["ANONA_API_KEY"] = "anona_live_FROMENV"
        os.environ["ANONA_BASE_URL"] = BASE
        rc, out, err = run_main(["retrieve", "x", "--space", "s"])
        assert rc == 0, (rc, err)
        assert auths()[-1] == "Bearer anona_live_FROMENV", auths()

        # the flag beats env.
        rc, out, err = run_main(["reason", "x", "--space", "s",
                                 "--api-key", "anona_live_FROMFLAG", "--base-url", BASE])
        assert rc == 0, (rc, err)
        assert auths()[-1] == "Bearer anona_live_FROMFLAG", auths()

        # And with no key anywhere, today's behaviour: the temp token.
        del os.environ["ANONA_API_KEY"]
        (ANONA / "config.env").unlink()
        rc, out, err = run_main(["record", "x"])
        assert rc == 0, (rc, err)
        assert auths()[-1] == "Bearer anona_tmp_TEMPTOKEN", auths()
        assert SEEN[-1][2]["space_id"] == "default", SEEN[-1]
        print("OK")
    """, tmp_path))


def test_an_empty_env_key_is_absent_not_a_credential(tmp_path):
    ok(_run("""
        temp_profile()
        os.environ["ANONA_API_KEY"] = ""
        rc, out, err = run_main(["record", "x"])
        assert rc == 0, (rc, err)
        assert auths() == ["Bearer anona_tmp_TEMPTOKEN"], auths()
        print("OK")
    """, tmp_path))


def test_an_api_key_beats_an_mcp_login_and_the_login_token_is_never_sent(tmp_path):
    ok(_run("""
        temp_profile(signed_in=True)
        os.environ["ANONA_API_KEY"] = "anona_live_K"
        os.environ["ANONA_BASE_URL"] = BASE
        for argv in (["record", "x"], ["retrieve", "x"], ["reason", "x"]):
            rc, out, err = run_main(argv + ["--space", "work"])
            assert rc == 0, (argv, rc, err)
        assert auths() == ["Bearer anona_live_K"] * 3, auths()
        assert all(b["space_id"] == "work" for _, _, b in SEEN), SEEN
        print("OK")
    """, tmp_path))


# --- MCP login only: still refused, and the message names the key route ----


def test_mcp_login_only_still_refuses_and_names_the_api_key_options(tmp_path):
    ok(_run("""
        temp_profile(signed_in=True, temp=False)
        for argv in (["record", "x"], ["retrieve", "x"], ["reason", "x"]):
            rc, out, err = run_main(argv)
            assert rc == 4, (argv, rc, err)
            for needle in ("ANONA_API_KEY", "--api-key", "config.env", "anona start", "anona mcp"):
                assert needle in err, (argv, needle, err)
            assert "oauth_MCP_ONLY" not in out + err
        assert SEEN == [], SEEN
        print("OK")
    """, tmp_path))


def test_no_credential_at_all_is_an_auth_exit(tmp_path):
    ok(_run("""
        rc, out, err = run_main(["record", "x"])
        assert rc == 4, (rc, err)
        assert "ANONA_API_KEY" in err and "anona start" in err, err
        assert SEEN == [], SEEN
        print("OK")
    """, tmp_path))


def test_a_401_is_an_auth_exit(tmp_path):
    ok(_run("""
        MODE["status"] = 401
        rc, out, err = run_main(["record", "x", "--space", "s",
                                 "--api-key", "anona_live_K", "--base-url", BASE])
        assert rc == 4, (rc, err)
        assert "invalid_api_key" in err, err
        print("OK")
    """, tmp_path))


# --- config.env is data, never code ----------------------------------------


def test_config_env_is_parsed_not_executed(tmp_path):
    ok(_run("""
        marker = Path.home() / "pwned"
        config_env(
            "# written by hand\\n"
            "export OTHER=1\\n"
            "$(touch %s)\\n"
            "`touch %s`\\n"
            "ANONA_API_KEY='anona_live_K'\\n"
            "ANONA_SPACE_ID='$(touch %s)'\\n"
            "ANONA_BASE_URL='%s'\\n"
            "ANONA_SOMETHING_ELSE='ignored'\\n" % (marker, marker, marker, BASE)
        )
        rc, out, err = run_main(["record", "x"])
        assert rc == 0, (rc, err)
        assert not marker.exists(), "config.env was executed"
        assert SEEN[-1][1] == "Bearer anona_live_K", SEEN
        # The space id is the literal text, never the output of a command.
        assert SEEN[-1][2]["space_id"] == "$(touch %s)" % marker, SEEN[-1]
        print("OK")
    """, tmp_path))


def test_config_env_ignores_unrelated_names(tmp_path):
    ok(_run("""
        config_env("ANONA_API_KEY_OLD='anona_live_WRONG'\\nOTHER_ANONA_API_KEY=anona_live_WRONG\\n"
                   "ANONA_API_KEY=anona_live_RIGHT\\nANONA_BASE_URL=%s\\nANONA_SPACE_ID=s\\n" % BASE)
        rc, out, err = run_main(["retrieve", "x"])
        assert rc == 0, (rc, err)
        assert auths() == ["Bearer anona_live_RIGHT"], auths()
        print("OK")
    """, tmp_path))


def _installer_line(name: str, value: str) -> str:
    """One line of config.env exactly as the skill installer writes it."""
    script = INSTALLER.read_text()
    shq = next(ln for ln in script.splitlines() if ln.startswith("shq()"))
    return subprocess.run(
        ["bash", "-c", shq + '\nprintf "%s=%s\\n" "$1" "$(shq "$2")"', "_", name, value],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_installer_quoting_round_trips(tmp_path):
    space = "it's a $(space) with `ticks` and \\back"
    lines = (
        _installer_line("ANONA_API_KEY", "anona_live_K'Q")
        + _installer_line("ANONA_SPACE_ID", space)
    )
    assert "'\\''" in lines, lines  # the installer's own escape for a quote
    (tmp_path / ".anona").mkdir()
    cfg = tmp_path / ".anona" / "config.env"
    cfg.write_text(lines)
    os.chmod(cfg, 0o600)
    ok(_run(f"""
        os.environ["ANONA_BASE_URL"] = "https://api.anonalabs.com"
        from anona.cli import config_env as ce
        got = ce.load()
        assert got["ANONA_API_KEY"] == "anona_live_K'Q", got
        assert got["ANONA_SPACE_ID"] == {space!r}, got
        print("OK")
    """, tmp_path))


def test_a_group_or_world_readable_config_env_is_warned_about_without_the_value(tmp_path):
    ok(_run("""
        config_env("ANONA_API_KEY='anona_live_SECRETSECRET'\\nANONA_BASE_URL='%s'\\n" % BASE, mode=0o644)
        rc, out, err = run_main(["record", "x", "--space", "s"])
        assert rc == 0, (rc, err)
        assert "config.env" in err and "chmod 600" in err, err
        assert "SECRETSECRET" not in out + err, (out, err)
        os.chmod(ANONA / "config.env", 0o600)
        rc, out, err = run_main(["record", "x", "--space", "s"])
        assert "chmod" not in err, err
        print("OK")
    """, tmp_path))


# --- space ------------------------------------------------------------------


def test_an_api_key_without_a_space_is_a_usage_error_and_sends_nothing(tmp_path):
    ok(_run("""
        temp_profile()
        for argv in (["record", "x"], ["retrieve", "x"], ["reason", "x"]):
            rc, out, err = run_main(argv + ["--api-key", "anona_live_K", "--base-url", BASE])
            assert rc == 2, (argv, rc, err)
            assert "--space" in err and "ANONA_SPACE_ID" in err, err
        assert SEEN == [], SEEN
        print("OK")
    """, tmp_path))


def test_anona_space_id_from_env_then_config_env(tmp_path):
    ok(_run("""
        config_env("ANONA_API_KEY='anona_live_K'\\nANONA_SPACE_ID='fromfile'\\nANONA_BASE_URL='%s'\\n" % BASE)
        rc, out, err = run_main(["record", "x"])
        assert rc == 0, (rc, err)
        assert SEEN[-1][2]["space_id"] == "fromfile", SEEN[-1]
        os.environ["ANONA_SPACE_ID"] = "fromenv"
        rc, out, err = run_main(["record", "x"])
        assert SEEN[-1][2]["space_id"] == "fromenv", SEEN[-1]
        rc, out, err = run_main(["record", "x", "--space", "fromflag"])
        assert SEEN[-1][2]["space_id"] == "fromflag", SEEN[-1]
        print("OK")
    """, tmp_path))


# --- host binding -------------------------------------------------------------


def test_an_env_key_is_not_sent_to_a_foreign_base_url_flag(tmp_path):
    ok(_run("""
        os.environ["ANONA_API_KEY"] = "anona_live_K"
        for argv in (["record", "x"], ["retrieve", "x"], ["reason", "x"]):
            rc, out, err = run_main(argv + ["--space", "s", "--base-url", BASE])
            assert rc == 4, (argv, rc, err)
            assert "--api-key" in err, err
            assert "anona_live_K" not in out + err
        assert SEEN == [], SEEN
        print("OK")
    """, tmp_path))


def test_a_config_env_key_is_not_sent_to_a_foreign_host_named_elsewhere(tmp_path):
    ok(_run("""
        config_env("ANONA_API_KEY='anona_live_K'\\nANONA_SPACE_ID='s'\\n")
        # A foreign ANONA_BASE_URL in the environment is a different source.
        os.environ["ANONA_BASE_URL"] = BASE
        rc, out, err = run_main(["record", "x"])
        assert rc == 4, (rc, err)
        assert SEEN == [], SEEN
        print("OK")
    """, tmp_path))


def test_an_env_key_is_not_sent_to_a_foreign_host_named_in_config_env(tmp_path):
    ok(_run("""
        config_env("ANONA_BASE_URL='%s'\\n" % BASE)
        os.environ["ANONA_API_KEY"] = "anona_live_K"
        rc, out, err = run_main(["record", "x", "--space", "s"])
        assert rc == 4, (rc, err)
        assert SEEN == [], SEEN
        print("OK")
    """, tmp_path))


def test_a_flag_key_is_not_sent_to_a_foreign_host_from_the_environment(tmp_path):
    ok(_run("""
        os.environ["ANONA_BASE_URL"] = BASE
        rc, out, err = run_main(["record", "x", "--space", "s", "--api-key", "anona_live_K"])
        assert rc == 4, (rc, err)
        assert "--base-url" in err, err
        assert SEEN == [], SEEN
        print("OK")
    """, tmp_path))


def test_a_flag_key_may_go_to_a_flag_base_url(tmp_path):
    ok(_run("""
        os.environ["ANONA_API_KEY"] = "anona_live_ENV"
        rc, out, err = run_main(["record", "x", "--space", "s",
                                 "--api-key", "anona_live_K", "--base-url", BASE])
        assert rc == 0, (rc, err)
        assert auths() == ["Bearer anona_live_K"], auths()
        print("OK")
    """, tmp_path))


def test_plain_http_to_a_remote_host_is_refused_even_with_a_flag_key(tmp_path):
    ok(_run("""
        rc, out, err = run_main(["record", "x", "--space", "s", "--api-key", "anona_live_K",
                                 "--base-url", "http://memory.anonalabs.com"])
        assert rc == 4, (rc, err)
        assert "https://" in err, err
        os.environ["ANONA_API_KEY"] = "anona_live_K"
        os.environ["ANONA_BASE_URL"] = "http://10.0.0.5:3001"
        rc, out, err = run_main(["record", "x", "--space", "s"])
        assert rc == 4 and "https://" in err, (rc, err)
        print("OK")
    """, tmp_path))


def test_the_default_hosts_and_only_those_take_an_env_key(tmp_path):
    ok(_run("""
        from anona import AnonaClient
        import inspect
        sdk_default = inspect.signature(AnonaClient.__init__).parameters["base_url"].default
        # Nothing named: the SDK's own default.
        assert target.for_api_key(None, None, "env") == sdk_default == "https://api.anonalabs.com"
        for url in ("https://api.anonalabs.com", "https://memory.anonalabs.com/",
                    "https://API.anonalabs.com"):
            assert target.for_api_key(url, "flag", "env") == url
            assert target.for_api_key(url, "env", "config.env") == url
        for url in ("https://api.anonalabs.com.evil.example", "https://evil.example",
                    "https://api.anonalabs.com:8443", "https://x@api.anonalabs.com",
                    "http://api.anonalabs.com"):
            for key_source in ("env", "config.env"):
                try:
                    target.for_api_key(url, "flag", key_source)
                except target.TargetError:
                    continue
                raise AssertionError("accepted %s for a %s key" % (url, key_source))
        # Same source is allowed (over https, or http to loopback).
        assert target.for_api_key("https://self.example", "env", "env") == "https://self.example"
        assert target.for_api_key("https://self.example", "config.env", "config.env")
        assert target.for_api_key("https://self.example", "flag", "flag")
        print("OK")
    """, tmp_path))


# --- status -------------------------------------------------------------------


def test_status_names_the_key_source_and_only_its_prefix(tmp_path):
    ok(_run("""
        key = "anona_live_abc1" + "Z" * 40
        os.environ["ANONA_API_KEY"] = key
        rc, out, err = run_main(["status"])
        assert rc == 0, (rc, out, err)
        assert "anona_live_abc1…" in out, out
        assert "ANONA_API_KEY" in out, out
        assert key not in out + err and "ZZZZ" not in out + err, out
        del os.environ["ANONA_API_KEY"]
        config_env("ANONA_API_KEY='%s'\\n" % key)
        rc, out, err = run_main(["status"])
        assert rc == 0 and "config.env" in out and key not in out + err, out
        rc, out, err = run_main(["status", "--api-key", key])
        assert rc == 0 and "--api-key" in out and key not in out + err, out
        print("OK")
    """, tmp_path))


def test_status_without_a_key_is_unchanged(tmp_path):
    ok(_run("""
        rc, out, err = run_main(["status"])
        assert rc == 1 and out.strip() == "Not logged in. Run `anona login`.", (rc, out)
        temp_profile()
        rc, out, err = run_main(["status"])
        assert rc == 0 and out.startswith("Temporary profile for"), out
        assert "API key" not in out, out
        print("OK")
    """, tmp_path))


# --- no secret anywhere in output ---------------------------------------------


def test_the_key_never_appears_in_output(tmp_path):
    ok(_run("""
        SECRET = "anona_live_SECRETSECRET"
        def check(argv, want_rc):
            rc, out, err = run_main(argv)
            assert rc == want_rc, (argv, rc, out, err)
            assert SECRET not in out and SECRET not in err, (argv, out, err)
            assert "SECRETSECRET" not in out + err, (argv, out, err)
        flag = ["--api-key", SECRET]
        for verb in ("record", "retrieve", "reason"):
            extra = ["--json"] if verb == "retrieve" else []
            # success
            MODE["status"] = 200
            check([verb, "x", "--space", "s", "--base-url", BASE] + flag + extra, 0)
            # 401 and 500
            MODE["status"] = 401
            check([verb, "x", "--space", "s", "--base-url", BASE] + flag, 4)
            MODE["status"] = 500
            check([verb, "x", "--space", "s", "--base-url", BASE] + flag, 1)
            # usage error, refused host, refused transport, unreachable host
            check([verb, "x", "--base-url", BASE] + flag, 2)
            check([verb, "", "--space", "s", "--base-url", BASE] + flag, 2)
            os.environ["ANONA_API_KEY"] = SECRET
            check([verb, "x", "--space", "s", "--base-url", BASE], 4)
            del os.environ["ANONA_API_KEY"]
            check([verb, "x", "--space", "s", "--base-url", "http://evil.example"] + flag, 4)
            check([verb, "x", "--space", "s", "--base-url", "http://127.0.0.1:1"] + flag, 1)
        os.environ["ANONA_API_KEY"] = SECRET
        check(["status"], 0)
        print("OK")
    """, tmp_path, timeout=120))
