"""`~/.anona/config.env`, read as data.

The skill installer (`skills/install.sh`) writes this file for the agent skill
and for `anona-mcp`: `NAME='value'` lines, single-quoted, with a quote inside a
value written as '\\''. It is meant to be sourceable by a shell, which is
exactly why it must never be sourced or executed here: a value is whatever the
installer was given, and a hand-edited line can hold `$(...)` or backticks.
So it is parsed the way the installer's own `read_config` parses it -- the
three names below and nothing else, quoted or unquoted, never expanded.

Separate from `credentials.json` (store.py): this holds an API key, which the
REST API accepts, and `anona logout` never touches it.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

NAMES = ("ANONA_API_KEY", "ANONA_SPACE_ID", "ANONA_BASE_URL")


def path() -> Path:
    # The active profile's (store.anona_dir): ~/.anona/config.env by default.
    from . import store

    return store.anona_dir() / "config.env"


def _unquote(value: str) -> str:
    # The installer's `shq`: the whole value in single quotes, each embedded
    # quote as '\''. Older installers wrote it unquoted, which is taken as is.
    if len(value) >= 2 and value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("'\\''", "'")
    return value


def parse(text: str) -> dict[str, str]:
    """The recognised names in `text`. The first assignment of a name wins,
    as in the installer's reader; an empty value counts as absent."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        line = line.rstrip("\r")
        if line.startswith("export "):
            line = line[len("export "):]
        name, sep, value = line.partition("=")
        if not sep or name not in NAMES or name in found:
            continue
        value = _unquote(value)
        if value:
            found[name] = value
    return found


def load() -> dict[str, str]:
    """The recognised names in config.env, or {} when there is no file.

    Warns on stderr -- naming the file, never a value -- when the file is
    readable by anyone but its owner: it holds a key with full data-plane
    reach. The installer writes it 600; this catches a hand-made copy.
    """
    p = path()
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        mode = p.stat().st_mode
    except OSError:
        return {}
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(
            f"anona: warning: {p} is readable by other users; "
            f"run `chmod 600 {p}`.",
            file=sys.stderr,
        )
    return parse(text)
