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
    """Printed as `anona: <message>`; the command exits without a request
    (1, or 4 from record/retrieve/reason, where a refused host is an auth
    failure)."""


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


# --- an API key ---------------------------------------------------
#
# A key is not tied to a deployment the way a stored login is -- nothing in it
# says which server minted it -- so the rule above cannot apply. The risk is
# the same one, though: a key sitting in the environment or in config.env is
# picked up by every `anona` invocation, so a `--base-url` that an agent was
# talked into typing would carry it to whoever runs that host. So a key only
# goes to the Anona hosts, unless the server was named in the SAME place the
# key came from: both on the command line, both in the environment, or both
# in config.env. Naming the two together is a decision about where that key
# goes; naming only the host is not.

# The SDK's own default (AnonaClient's `base_url`), not store.DEFAULT_BASE_URL:
# an API key is the SDK's credential, and api. skips the dashboard edge hop.
DEFAULT_API_BASE_URL = "https://api.anonalabs.com"
ANONA_HOSTS = frozenset({"api.anonalabs.com", "memory.anonalabs.com"})

# Where a value came from, in the words a person would type.
SOURCE_NAMES = {
    "flag": "--api-key",
    "env": "ANONA_API_KEY",
    "config.env": "~/.anona/config.env",
}
_URL_SOURCE_NAMES = {
    "flag": "--base-url",
    "env": "ANONA_BASE_URL",
    "config.env": "ANONA_BASE_URL in ~/.anona/config.env",
}


def is_anona_host(base_url: str) -> bool:
    try:
        parts = urlsplit(base_url)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname in ANONA_HOSTS
        and port in (None, 443)
        and parts.username is None
        and parts.password is None
    )


def for_api_key(requested: str | None, url_source: str | None, key_source: str) -> str:
    """The deployment an API key may be sent to, or TargetError.

    `requested` is the named base URL, if any, and `url_source` where it came
    from ("flag", "env" or "config.env"); `key_source` is where the key came
    from, in the same terms.
    """
    base = requested or DEFAULT_API_BASE_URL
    check_transport(base)
    if is_anona_host(base) or url_source == key_source:
        return base
    if key_source == "flag":
        fix = (
            "A key given with --api-key goes to another server only when that "
            "server is given with --base-url on the same command."
        )
    else:
        fix = (
            f"A key from {SOURCE_NAMES[key_source]} goes only to "
            "api.anonalabs.com or memory.anonalabs.com, unless ANONA_BASE_URL "
            "is set in the same place. To use another server, pass the key "
            "with --api-key on the same command line."
        )
    raise TargetError(
        f"refusing to send the API key from {SOURCE_NAMES[key_source]} to "
        f"{base} (named by {_URL_SOURCE_NAMES.get(url_source or '', 'default')}). {fix}"
    )
