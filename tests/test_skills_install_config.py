"""skills/install.sh writes ~/.anona/config.env as data, not as shell code.

The file is meant to be sourced (the anona-memory skill tells the agent to run
`. ~/.anona/config.env`), and the installer itself used to source it on a
re-install. Values were written unquoted, so a space id with a space in it ran
its second word as a command, and a value holding `$(...)` executed it. Values
are now single-quoted on write and parsed, never sourced, on read.

Each case runs the real installer against this checkout (ANONA_SKILLS_SRC) in a
throwaway HOME, with `curl` stubbed so nothing reaches the network, and in a new
session so there is no /dev/tty to prompt on.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SKILLS = Path(__file__).resolve().parent.parent / "skills"
_INSTALL = _SKILLS / "install.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@pytest.fixture
def sandbox(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text("#!/bin/sh\nprintf 200\n")
    curl.chmod(0o755)
    env = {
        "HOME": str(home),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "ANONA_SKILLS_SRC": str(_SKILLS),
    }
    return home, env, tmp_path


def _install(env, cwd, *args):
    r = subprocess.run(
        ["bash", str(_INSTALL), "--dir", str(cwd / "skills"), "--no-mcp", "--force", *args],
        env=env, cwd=cwd, capture_output=True, text=True, timeout=60, check=False,
        start_new_session=True,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    return r


def _source(shell, home, cwd):
    """What a shell sees after `. ~/.anona/config.env`, the way the skill does it."""
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} not installed")
    r = subprocess.run(
        [shell, "-c", '. "$HOME/.anona/config.env"; printf "%s\\n%s\\n" "$ANONA_API_KEY" "$ANONA_SPACE_ID"'],
        env={"HOME": str(home), "PATH": os.environ["PATH"]}, cwd=cwd,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.split("\n")[:2]


HOSTILE_KEY = "anona_test_k$(touch PWNED)`touch PWNED2`"


@pytest.mark.parametrize("shell", ["sh", "dash", "bash"])
def test_written_values_source_back_verbatim_and_run_nothing(sandbox, shell):
    home, env, cwd = sandbox
    _install(env, cwd, "--api-key", HOSTILE_KEY, "--space", "team notes it's")
    assert _source(shell, home, cwd) == [HOSTILE_KEY, "team notes it's"]
    assert not (cwd / "PWNED").exists() and not (cwd / "PWNED2").exists()


def test_reinstall_reads_the_file_without_sourcing_it(sandbox):
    # An older installer wrote this unquoted. Sourcing it would run `touch`.
    home, env, cwd = sandbox
    conf = home / ".anona"
    conf.mkdir()
    (conf / "config.env").write_text(
        "# old\nANONA_API_KEY=anona_test_old\nANONA_SPACE_ID=x $(touch PWNED)\nEVIL=$(touch PWNED3)\n"
    )
    _install(env, cwd)
    assert not (cwd / "PWNED").exists() and not (cwd / "PWNED3").exists()
    # The key survives the re-install, and so does the space id, now quoted.
    assert _source("bash", home, cwd) == ["anona_test_old", "x $(touch PWNED)"]
    assert not (cwd / "PWNED").exists()


def test_reinstall_round_trips_its_own_quoting(sandbox):
    home, env, cwd = sandbox
    _install(env, cwd, "--api-key", "anona_test_rt", "--space", "it's a 'space'")
    _install(env, cwd)  # reads the quoted file back, writes it again
    assert _source("dash", home, cwd) == ["anona_test_rt", "it's a 'space'"]
