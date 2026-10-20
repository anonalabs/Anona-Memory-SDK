"""Which deployment a command talks to, and whether a credential may travel there.

A stored credential belongs to the deployment that issued it. `--base-url` and
ANONA_BASE_URL used to re-target it at any host, over plain HTTP, with no
warning, so one flag typed by an agent -- or injected into its instructions by
a page it read -- sent the token to whoever ran that server. A temp
token has full data-plane access and cannot be rotated.

So: naming a different deployment than the stored credential's is refused
rather than obeyed (`anona login --base-url` is how you switch), and a request
carrying or receiving a credential must be https://, except to loopback.
The login path already pins the issuer to the host it asked (oauth.discover);
this is the same rule for everything else.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from . import store

# Hostnames as urlsplit reports them: lowercased, IPv6 without brackets.
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


class TargetError(Exception):
    """Printed as `anona: <message>`; the command exits 1 without a request."""


def _same(a: str, b: str) -> bool:
    return a.rstrip("/") == b.rstrip("/")


def check_transport(base_url: str) -> None:
    try:
        parts = urlsplit(base_url)
        host = parts.hostname
    except ValueError:
        host = None
        parts = None
    if parts is not None and host:
        if parts.scheme == "https":
            return
        if parts.scheme == "http" and host in _LOOPBACK:
            return
    raise TargetError(
        f"refusing to use {base_url}: a credential travels with every request, "
        "so the server must be https:// (plain http:// is allowed only for "
        "localhost and 127.0.0.1)."
    )


def resolve(requested: str | None, stored_base_url: str | None) -> str:
    """The deployment to talk to, or TargetError.

    `requested` is what the user named -- the flag, else ANONA_BASE_URL -- or
    None. With a stored credential the answer is always its own deployment;
    naming another is refused, never followed.
    """
    if stored_base_url is not None:
        if requested is not None and not _same(requested, stored_base_url):
            raise TargetError(
                f"the stored credential is for {stored_base_url}, not {requested}. "
                f"Run `anona login --base-url {requested}` to switch."
            )
        base = stored_base_url
    else:
        base = requested or store.DEFAULT_BASE_URL
    check_transport(base)
    return base
