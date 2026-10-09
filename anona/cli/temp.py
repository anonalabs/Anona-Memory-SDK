"""`anona start`: make a temporary memory profile with no identity.

The profile is deleted 72 hours after creation unless claimed with
`anona login`. It has no email address attached, so this CLI is the only
place its deadline can ever be shown: `start` prints it and `status` repeats it.

The space behind it is created on first write, not here. Nothing in this module
claims it is ready to query.
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime

import httpx

from . import store

TEMP_TOKEN_PREFIX = "anona_tmp_"


class StartError(Exception):
    """A clean, message-only exit."""


def _parse_deadline(value: object) -> float:
    if not isinstance(value, str):
        raise StartError("The server returned no expiry time, so nothing was saved.")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StartError("The server returned an expiry time that is not a date.") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def describe_deadline(expires_at: float, now: float | None = None) -> str:
    """'expires 2026-10-06 12:51 UTC (in 71 hours)', or the past-tense form."""
    now = time.time() if now is None else now
    when = datetime.fromtimestamp(expires_at, UTC).strftime("%Y-%m-%d %H:%M UTC")
    left = expires_at - now
    if left <= 0:
        return f"expired {when}; its memories have been or will shortly be deleted"
    hours, minutes = divmod(int(left // 60), 60)
    span = f"{hours} hours" if hours else f"{minutes} minutes"
    return f"expires {when} (in {span}), after which its memories are deleted"


def run_start(base_url: str) -> int:
    existing = store.load()
    if existing is not None and existing.signed_in:
        print(
            "anona start: you are already signed in. Run `anona logout` first "
            "if you want a temporary profile instead.",
            file=sys.stderr,
        )
        return 1
    if (
        existing is not None
        and existing.temp_token
        and existing.temp_expires_at is not None
        and existing.temp_expires_at > time.time()
    ):
        # Creation is rate limited per address, and a second profile would
        # orphan the first one's memories.
        print("A temporary profile already exists; not creating another.")
        print(f"It {describe_deadline(existing.temp_expires_at)}.")
        print("Run `anona login` before then to keep it.")
        return 0
    try:
        try:
            resp = httpx.post(base_url.rstrip("/") + "/v1/temp/profiles", timeout=30)
        except httpx.HTTPError as exc:
            raise StartError(f"Could not reach {base_url}: {type(exc).__name__}.") from exc
        if resp.status_code == 429:
            raise StartError("Too many temporary profiles from this address today. Try again tomorrow.")
        if resp.status_code != 201:
            raise StartError(f"The server refused to create a profile ({resp.status_code}).")
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise StartError("The server returned something that is not a JSON object.")
        token = body.get("temp_token")
        if not isinstance(token, str) or not token.startswith(TEMP_TOKEN_PREFIX):
            raise StartError("The server returned no usable token, so nothing was saved.")
        deadline = _parse_deadline(body.get("expires_at"))
        store.save(
            store.Credentials(
                access_token="",
                refresh_token="",
                expires_at=0.0,
                client_id="",
                base_url=base_url,
                temp_token=token,
                temp_expires_at=deadline,
            )
        )
    except StartError as exc:
        print(f"anona start: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"anona start: could not save the profile to {store.path()}: {exc.strerror}.", file=sys.stderr)
        return 1
    print(f"Temporary profile created. It {describe_deadline(deadline)}.")
    print("Nothing warns you before then: run `anona status` to check, and")
    print("`anona login` to claim it and keep what it holds.")
    print("Its space, `default`, is normally created now; if a retrieve reports no space, the first memory you write creates it.")
    return 0
