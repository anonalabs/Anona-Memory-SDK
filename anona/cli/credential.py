"""Which API key `record`, `retrieve` and `reason` use, and where it came from.

Highest first: `--api-key`, then ANONA_API_KEY in the environment, then
ANONA_API_KEY in ~/.anona/config.env. With none of them the verbs fall back to
the command-line login (`anona login --cli`), then to the stored
temporary-profile token; memory._token_and_space owns that half.

The MCP login is never one of these. It is MCP-scoped (`typ=mcp_access`) and
the REST API refuses it by design; MCP and the CLI's data verbs hold separate
credentials on purpose.

The source travels with the key because it decides where the key may be sent
(target.for_api_key) and is what `anona status` names. The key itself is
never printed: `display()` is the only form that reaches output.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

_PREFIXES = ("anona_live_", "anona_test_")


@dataclass(frozen=True)
class ApiKey:
    key: str = field(repr=False)
    source: str  # "flag", "env" or "config.env"

    def display(self) -> str:
        """The key's prefix and a few characters, never enough to use it."""
        for prefix in _PREFIXES:
            if self.key.startswith(prefix):
                rest = self.key[len(prefix):]
                # A short key would give away a large share of itself.
                return prefix + (rest[:4] if len(rest) >= 16 else "") + "…"
        return (self.key[:4] if len(self.key) >= 24 else "") + "…"


def find_api_key(flag: str | None, config: dict[str, str]) -> ApiKey | None:
    if flag:
        return ApiKey(flag, "flag")
    env = os.environ.get("ANONA_API_KEY")
    if env:
        return ApiKey(env, "env")
    from_file = config.get("ANONA_API_KEY")
    if from_file:
        return ApiKey(from_file, "config.env")
    return None


def find_base_url(flag: str | None, config: dict[str, str]) -> tuple[str | None, str | None]:
    """(base URL, its source), or (None, None) when nothing names one."""
    if flag:
        return flag, "flag"
    env = os.environ.get("ANONA_BASE_URL")
    if env:
        return env, "env"
    from_file = config.get("ANONA_BASE_URL")
    if from_file:
        return from_file, "config.env"
    return None, None


def find_space(flag: str | None, config: dict[str, str]) -> str | None:
    return flag or os.environ.get("ANONA_SPACE_ID") or config.get("ANONA_SPACE_ID") or None


# Anything shaped like an Anona credential, wherever it turns up in text we
# did not write -- argparse echoes unrecognised arguments verbatim, so
# `anona start --api-key <key>` would otherwise print the key back.
_CREDENTIAL = re.compile(r"\b(anona_(?:live|test|tmp)_)[A-Za-z0-9_\-]+")


def redact(text: str) -> str:
    return _CREDENTIAL.sub(lambda m: m.group(1) + "…", text)
