"""Shared harness for the `anona` CLI tests.

Each case runs in a subprocess, so a test that leaves process state behind --
a loaded module, a patched `webbrowser`, a stray credential file -- cannot
reach the next one. The CLI reads $HOME, which is what makes that isolation
worth paying for: every case gets its own.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

if sys.flags.optimize:
    raise RuntimeError(
        "These tests run under PYTHONOPTIMIZE, which strips every bare assert "
        "and makes the whole suite pass having checked nothing. Re-run without it."
    )

# This repo's root IS the package.
SDK_ROOT = Path(__file__).resolve().parent.parent


# No case may launch a browser on whoever runs the suite. Every case goes
# through run_cli, so the guard lives here. It raises a BaseException, not an
# Exception: login.run_login wraps webbrowser.open in `except Exception: pass`
# (a failed launch must not abort a login), which would swallow an ordinary
# error and let the test pass having reached a real browser. A case that WANTS
# an opener patches `login.webbrowser.open` itself, which replaces this.
_BROWSER_GUARD = """
import webbrowser as _wb
class BrowserLaunchedInTest(BaseException):
    pass
def _no_browser(*a, **k):
    raise BrowserLaunchedInTest("a test tried to launch a browser: %r" % (a,))
_wb.open = _wb.open_new = _wb.open_new_tab = _no_browser
"""


def run_cli(snippet: str, env: dict | None = None, timeout: int = 30) -> subprocess.CompletedProcess:
    header = f"import sys\nsys.path.insert(0, {str(SDK_ROOT)!r})\n" + _BROWSER_GUARD
    # PYTHONOPTIMIZE would strip every bare `assert` from the snippet, so every
    # test would pass having checked nothing. Remove it whether it is inherited
    # or passed in `env`: no case may run with assertions off.
    full_env = {**os.environ, **(env or {})}
    full_env.pop("PYTHONOPTIMIZE", None)
    return subprocess.run(
        [sys.executable, "-c", header + textwrap.dedent(snippet)],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=full_env,
    )


def ok(result: subprocess.CompletedProcess) -> None:
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert result.returncode == 0 and lines and lines[-1] == "OK", (
        f"returncode={result.returncode}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
