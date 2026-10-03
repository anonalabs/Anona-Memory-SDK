"""Where the OAuth credential lives on disk.

Separate from `~/.anona/config.env`, which holds an API key for the skill and
for `anona-mcp`. The two are different credentials with different reach: a key
works against REST, this does not. Keeping them in separate files means
`anona logout` cannot silently destroy a key the user minted by hand.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BASE_URL = "https://memory.anonalabs.com"


@dataclass
class Credentials:
    # repr=False on both tokens: a dataclass repr reaches logs, tracebacks and
    # debugger output without anyone deciding it should.
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float
    client_id: str
    base_url: str = DEFAULT_BASE_URL
    # An unclaimed profile made by `anona start`. A temp-only file has empty
    # OAuth fields (see `signed_in`). repr=False for the same reason as above.
    temp_token: str | None = field(default=None, repr=False)
    temp_expires_at: float | None = None

    def live_temp_token(self) -> str | None:
        """The unclaimed-profile token, unless it is absent, undated or past its deadline.

        An expired profile is already gone server-side, so presenting it can
        only fail.
        """
        if not self.temp_token:
            return None
        if self.temp_expires_at is None or self.temp_expires_at <= time.time():
            return None
        return self.temp_token

    @property
    def signed_in(self) -> bool:
        return bool(self.access_token)


def path() -> Path:
    return Path(os.path.expanduser("~")) / ".anona" / "credentials.json"


def _opt(value, kind):
    # A wrong-typed optional field reads as absent; it must not make the whole
    # credential unreadable.
    return value if isinstance(value, kind) and not isinstance(value, bool) else None


def load() -> Credentials | None:
    """The stored credential, or None when there is none that can be read.

    A corrupt or unreadable file reads as "logged out" rather than raising.
    This runs at the start of every `anona mcp` process, and a traceback there
    is an MCP client that fails to start with an error naming a JSON decoder.
    """
    p = path()
    try:
        raw = json.loads(p.read_text())
        return Credentials(
            access_token=raw["access_token"],
            refresh_token=raw["refresh_token"],
            expires_at=float(raw["expires_at"]),
            client_id=raw["client_id"],
            base_url=raw.get("base_url", DEFAULT_BASE_URL),
            temp_token=_opt(raw.get("temp_token"), str),
            temp_expires_at=_opt(raw.get("temp_expires_at"), (int, float)),
        )
    except Exception:
        return None


def save(c: Credentials) -> None:
    """Write 0600, atomically.

    Atomic because refresh ROTATES: the server burns the old refresh token the
    moment the new one is issued. A half-written file at that instant leaves
    the user holding neither, and the only recovery is approving again.
    """
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    doc: dict = {
        "access_token": c.access_token,
        "refresh_token": c.refresh_token,
        "expires_at": c.expires_at,
        "client_id": c.client_id,
        "base_url": c.base_url,
    }
    # Only when set, so the file an ordinary login writes is byte-for-byte
    # what it always was.
    if c.temp_token is not None:
        doc["temp_token"] = c.temp_token
        doc["temp_expires_at"] = c.temp_expires_at
    payload = json.dumps(doc, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".credentials-")
    try:
        # Wrap the fd first so it is closed even if fchmod raises.
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(payload)
        os.replace(tmp, p)
    except Exception:
        # Leave the previous file intact and remove the partial one. Without
        # this, a failed save leaves .credentials-XXXX litter in a 0700 dir.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def clear() -> None:
    try:
        path().unlink()
    except FileNotFoundError:
        pass
