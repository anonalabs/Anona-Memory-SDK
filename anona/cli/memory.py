"""`anona record`, `anona retrieve` and `anona reason`: the non-MCP path.

MCP is the primary surface and these are deliberately not documented as the
main way in. They exist for two cases: somebody who will not enable MCP at
all, and the session before a client restart has loaded the MCP tools.

`--space` is optional on a temporary profile because the server hardcodes its
space to "default". For a signed-in account it is required: an account may
hold several spaces, and writing to the wrong one looks exactly like success.
"""

from __future__ import annotations

import re
import sys

import httpx

from anona import AnonaClient, AnonaError

from .proxy import _bearer
from .store import load
from .tokens import NotLoggedIn


# Memory content and server error text are attacker-influenceable (an agent
# records what it read on a web page; a shared-space member writes the rest;
# a space's id is a user-chosen name). Raw, an ESC sequence could erase and
# rewrite earlier terminal output. Every control character becomes a space --
# newlines and tabs included, so one memory stays one line -- plus C1, the
# Unicode line separators and the bidi overrides. oauth._printable only
# collapses whitespace and does NOT cover this.
_UNSAFE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def _printable(text: object) -> str:
    return _UNSAFE.sub(" ", str(text))


# `reason` answers in prose, often several paragraphs of markdown, so the
# one-line rule above is wrong for it: collapsing the newlines would run every
# paragraph together. This keeps \n and \t and strips everything else the
# line form strips -- carriage return included, which is the one people forget:
# a bare \r returns the cursor to column 0 and lets later text overwrite the
# line already printed, which is most of what an escape sequence would buy.
_UNSAFE_BLOCK = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def _printable_block(text: object) -> str:
    return _UNSAFE_BLOCK.sub(" ", str(text))


def _token_and_space(space: str | None) -> tuple[str, str] | None:
    """(token, space), or None after printing why it cannot proceed.

    Token precedence comes from `proxy._bearer`, which already decides that a
    signed-in OAuth credential beats a temp token. Re-deriving that rule here
    is how the proxy and these verbs would disagree about which credential a
    write used.
    """
    if load() is None:
        print(
            "Not signed in. Run `anona start` for a temporary profile with no "
            "account, or `anona login` to use an existing one.",
            file=sys.stderr,
        )
        return None
    with httpx.Client(timeout=30.0) as hc:
        try:
            token, is_temp = _bearer(hc)
        except NotLoggedIn as exc:
            print(f"anona: {exc}", file=sys.stderr)
            return None
        except httpx.HTTPError as exc:
            # _bearer refreshes with a raw httpx.Client, outside the SDK's
            # transport-error translation, so this one can really fire.
            print(f"anona: could not refresh the login ({type(exc).__name__}).", file=sys.stderr)
            return None
    if space is None:
        if not is_temp:
            print(
                "This account may hold several spaces, so --space is required.",
                file=sys.stderr,
            )
            return None
        space = "default"  # a temp profile's only space, fixed server-side
    return token, space


def _fail(exc: AnonaError) -> int:
    # The SDK translates transport failures into AnonaError too (408/503).
    print(
        f"anona: the server answered HTTP {exc.status_code}: {_printable(exc.detail)}",
        file=sys.stderr,
    )
    return 1


def run_record(base_url: str, content: str, space: str | None) -> int:
    got = _token_and_space(space)
    if got is None:
        return 1
    token, space = got
    try:
        with AnonaClient(api_key=token, base_url=base_url) as client:
            client.record(space_id=space, content=content)
    except AnonaError as exc:
        return _fail(exc)
    print(f"Stored in space '{space}'.")
    return 0


def run_retrieve(base_url: str, query: str, space: str | None) -> int:
    got = _token_and_space(space)
    if got is None:
        return 1
    token, space = got
    try:
        with AnonaClient(api_key=token, base_url=base_url) as client:
            items = client.retrieve(space_id=space, query=query)
    except AnonaError as exc:
        return _fail(exc)
    if not items:
        print("No memories matched.")
        return 0
    for i, m in enumerate(items, 1):
        print(f"{i}. {_printable(m.get('content') or m.get('text') or '')}")
    return 0


def run_reason(base_url: str, query: str, space: str | None) -> int:
    got = _token_and_space(space)
    if got is None:
        return 1
    token, space = got
    # A synthesis is an agent loop over the space, not a lookup: it runs tens
    # of seconds and sometimes past a minute. Without this the command looks
    # hung, and the obvious reaction -- Ctrl-C and retry -- costs a second run
    # of the same work. stderr, so `anona reason ... > answer.md` still gets
    # only the answer.
    print(f"Reasoning over space '{space}'. This usually takes under a minute.", file=sys.stderr)
    try:
        with AnonaClient(api_key=token, base_url=base_url) as client:
            answer = client.reason(space_id=space, query=query)
    except AnonaError as exc:
        return _fail(exc)
    if not answer or not answer.strip():
        # Distinct from an error and from "no memories matched": the call
        # succeeded and the space had nothing to build an answer from.
        print("No answer. The space may hold nothing on that topic yet.")
        return 0
    print(_printable_block(answer))
    return 0
