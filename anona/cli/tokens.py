"""A valid access token for `anona mcp`, refreshed and persisted when needed.

The access token lives an hour; the refresh token 30 days, renewed on every
use, so a credential that is used never expires. That rolling renewal is what
lets the product say "approve once". Refresh ROTATES: the server burns the old
refresh token the instant it issues the new one and treats a replay as theft.
Everything here exists so that rotation never strands the user.

POSIX only: `fcntl` does not exist on Windows, and this CLI has already
accepted that rather than carrying a second locking path.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import time

import httpx

from . import oauth, store

# Refresh this long before expiry, so a token never lapses mid-request.
SKEW_SECONDS = 60


# Mirrors the Node CLI's lockTuning. Module-level so tests need not wait a minute.
NODELOCK_STALE_SECONDS = 60.0
NODELOCK_TIMEOUT_SECONDS = 90.0
NODELOCK_POLL_SECONDS = 0.025


class NotLoggedIn(Exception):
    """No usable credential. The message is what the user should read."""


_RELOGIN = "Run `anona login` again."


def _fresh(c: store.Credentials) -> bool:
    return c.expires_at - SKEW_SECONDS > time.time()


def _lock_path():
    p = store.path()
    return p.with_name(p.name + ".lock")


@contextlib.contextmanager
def _node_lock(file_path):
    """The Node CLI's lock: an exclusive-create file, `credentials.json.nodelock`.

    Node has no flock, so it serialises on this file instead, and the two
    schemes do not interlock on their own. A Python and a Node `anona mcp`
    refreshing together could then both rotate, and the server treats the
    replayed refresh token as theft and revokes EVERY refresh token for this
    client_id, which both implementations share through one credentials.json.
    Both sides would be logged out. So Python takes this lock too, around its
    flock. Node takes only this one, so no lock-ordering cycle can exist.

    Unlike Python's own `.lock` file, this one MUST be removed on release: Node
    creates it with O_EXCL, so a leftover file blocks everyone until stale.
    """
    lock_path = file_path.with_name(file_path.name + ".nodelock")
    started = time.monotonic()
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # Stale recovery: a holder that was killed never releases.
            try:
                if time.time() - lock_path.stat().st_mtime > NODELOCK_STALE_SECONDS:
                    lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue  # released between our open and our stat: retry
            if time.monotonic() - started > NODELOCK_TIMEOUT_SECONDS:
                raise oauth.OAuthError("Timed out waiting for another anona process to finish refreshing.")
            time.sleep(NODELOCK_POLL_SECONDS)
            continue
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)
        break
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def credential_lock():
    """Exclusive access to the credential file, shared with the Node CLI.

    `access_token` holds it across read, refresh and write; `login` and
    `logout` hold it around their write so a refresh that finishes just after
    cannot overwrite what they stored (or resurrect what they deleted).
    """
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(lock.parent, 0o700)
    with _node_lock(store.path()):
        # Several MCP clients means several `anona mcp` processes, and they all
        # see the same expired token at the same moment. One must rotate; the
        # rest must wait and use its result.
        with open(lock, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            yield


def _must_refresh(c: store.Credentials, force_refresh: bool, rejected: str | None) -> bool:
    if not _fresh(c):
        return True
    # The server refused a token we still believed in. Refresh only if what is
    # stored is still that token: if it differs, another process rotated while
    # we were waiting, and refreshing again would burn its new refresh token.
    return force_refresh and (rejected is None or c.access_token == rejected)


def access_token(
    client: httpx.Client, *, force_refresh: bool = False, rejected: str | None = None
) -> str:
    """A valid access token, refreshing under the lock when needed.

    `force_refresh` is for a caller whose request was refused with 401 although
    the token looked fresh; pass the refused token as `rejected` so the check
    happens against the credential found AFTER taking the lock. The credential
    file is only ever written from here (and by login/logout), under that lock.
    """
    c = store.load()
    if c is None:
        raise NotLoggedIn(f"Not logged in. {_RELOGIN}")
    if not _must_refresh(c, force_refresh, rejected):
        return c.access_token

    # Hold the lock across read, refresh and write.
    with credential_lock():
        # Re-read: whoever held the lock before us may have refreshed already.
        # Refreshing again would replay a refresh token that is now burned.
        c = store.load()
        if c is None:
            raise NotLoggedIn(f"Not logged in. {_RELOGIN}")
        if not _must_refresh(c, force_refresh, rejected):
            return c.access_token
        fresh = _refresh(c, client)
        # Persist before returning: the old refresh token is already dead, so
        # a crash after this point must find the new pair on disk.
        store.save(fresh)
        return fresh.access_token


def _refresh(c: store.Credentials, client: httpx.Client) -> store.Credentials:
    meta = oauth.discover(c.base_url, client)
    resp = client.post(
        meta["token_endpoint"],
        data={
            "grant_type": "refresh_token",
            "refresh_token": c.refresh_token,
            "client_id": c.client_id,
        },
        timeout=15,
    )
    if resp.status_code in (400, 401):
        # Rejected: expired after 30 idle days, or revoked. Leave the file
        # alone; deleting it adds nothing and hides the evidence.
        raise NotLoggedIn(_RELOGIN)
    # Anything else (5xx, network) is transient and says nothing about the
    # credential, so it must not read as a logout.
    resp.raise_for_status()
    try:
        body = resp.json()
    except ValueError as exc:
        raise oauth.OAuthError("The token endpoint returned something that is not JSON.") from exc
    if not isinstance(body, dict):
        raise oauth.OAuthError("The token endpoint returned JSON that is not an object.")
    access = body.get("access_token")
    refresh = body.get("refresh_token")
    expires_in = body.get("expires_in")
    # A rotated response without a refresh token must not be saved: it would
    # overwrite the only copy with nothing.
    if not access:
        raise oauth.OAuthError("The token endpoint returned no access_token.")
    if not refresh:
        raise oauth.OAuthError("The token endpoint returned no refresh_token.")
    if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or expires_in <= 0:
        raise oauth.OAuthError("The token endpoint returned no usable expires_in.")
    return store.Credentials(
        access_token=access,
        refresh_token=refresh,
        expires_at=time.time() + expires_in,
        client_id=c.client_id,
        base_url=c.base_url,
    )
