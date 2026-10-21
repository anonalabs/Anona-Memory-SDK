"""`anona record`, `anona retrieve` and `anona reason`: the non-MCP path.

MCP is the primary surface and these are deliberately not documented as the
main way in. They exist for two cases: somebody who will not enable MCP at
all, and the session before a client restart has loaded the MCP tools.

`--space` is optional because a temporary profile's space is hardcoded to
"default" server-side.

These verbs are temporary-profile-only, and that is a property of the
credential rather than a choice: `anona login` mints an MCP-scoped OAuth token
and the data plane refuses it, so there is no account credential the CLI holds
that /v1/record would accept. Giving an account the same three verbs means
giving the CLI an API key, which is a server-side decision, not a flag.
"""

from __future__ import annotations

import json
import re
import sys
from urllib.parse import urlsplit

import httpx

from anona import AnonaClient, AnonaError
from anona._record_outcome import (
    nothing_extracted_message,
    stored_ids,
    stored_something,
)

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
    if not is_temp:
        # A login mints an MCP-scoped OAuth token (`typ=mcp_access`), and the
        # data plane rejects any token carrying a `typ` -- deliberately, so an
        # MCP credential cannot be replayed against the REST API
        # (`test_mcp_access_token_rejected_on_data_plane`). These verbs call
        # /v1/record, /v1/retrieve and /v1/reason directly, so on a signed-in
        # account they can only ever get `401 invalid_token`.
        #
        # The server's own message is good -- "This token cannot be used for
        # the memory API. Use an Anona API key, or connect over MCP at /mcp."
        # An earlier version of this comment claimed it said "Session token is
        # invalid or expired", which is the *other* branch, the one for a JWT
        # that fails to decode. So this guard is not rescuing a misleading
        # answer. What it buys is smaller and still worth it: no wasted round
        # trip, no `_fail` wrapping the envelope in a Python dict repr, and a
        # mention of `anona start`, which is the third option and the one the
        # server cannot know about.
        print(
            "`record`, `retrieve` and `reason` work on a temporary profile "
            "(`anona start`).\n"
            "A signed-in account reaches memory over MCP (`anona mcp`), or with "
            "an API key\nand the SDK -- the login credential is scoped to MCP "
            "and the REST API refuses it.",
            file=sys.stderr,
        )
        return None
    if space is None:
        space = "default"  # a temp profile's only space, fixed server-side
    return token, space


# An input the server is certain to reject is rejected here instead, and like
# any other usage mistake it exits 2 -- distinct from 1, which means the
# request was made and failed. The round trip is not free: it is a metered
# call, so sending a write that cannot succeed bills for the privilege of a
# 422.
EMPTY_INPUT_EXIT = 2


def _reject_empty(what: str, value: str) -> int | None:
    if value.strip():
        return None
    print(
        f"anona: the {what} is empty. Nothing was sent -- the server rejects "
        f"an empty {what}, and the request would still be billed.",
        file=sys.stderr,
    )
    return EMPTY_INPUT_EXIT


def _host_of(url: str) -> str:
    parts = urlsplit(url)
    return parts.netloc or url


def _retry_notice(attempt: int, delay: float, status: int, total: int) -> None:
    """Say that a wait is happening, while it happens.

    Without this the CLI sits silent for the whole backoff -- two rate-limit
    waits read as a hung command, and the obvious reaction is Ctrl-C, which
    throws away the attempt that was about to succeed. stderr, so a piped
    stdout still carries only the result.
    """
    reason = "rate limited" if status == 429 else f"server error {status}"
    print(
        f"anona: {reason}, retrying in {delay:.0f} s ({attempt}/{total})",
        file=sys.stderr,
    )


def _fail(exc: AnonaError, base_url: str) -> int:
    """Report a failed call in the terms of what actually went wrong.

    Three different things used to print as "the server answered HTTP N".

    A transport failure is not an HTTP answer at all: the SDK gives it an
    informational 408/503 so that one `except` catches everything, and
    reporting that verbatim told people a server had answered when nothing
    had, pointing them at the wrong end of the wire.

    An API error carries `code` -- the stable, documented handle -- and the
    old line buried it inside a Python dict `repr`, single quotes and all,
    which is neither the JSON the docs show nor something a script can read.
    """
    if getattr(exc, "transport", False):
        # The detail embeds the repr of the underlying httpx error; the useful
        # half is the last clause, and the whole thing is long and noisy.
        #
        # The host is sanitized too. `check_transport` constrains the scheme
        # and so most of the string, but it is still a value that reached us
        # from `--base-url` or ANONA_BASE_URL -- which is exactly the vector
        # target.py exists for, an agent following a URL out of a page it
        # read -- and _printable costs nothing.
        print(
            f"anona: could not reach {_printable(_host_of(base_url))}: "
            f"{_printable(_transport_reason(exc))}",
            file=sys.stderr,
        )
        return 1
    if exc.code:
        # `code` is server-supplied text on its way to a terminal, like the
        # message and the ids, so it goes through the same sanitizer. It is
        # read straight off `error.code` in the response body, and nothing
        # between there and here constrains it to the documented vocabulary.
        line = f"anona: {_printable(exc.code)}: {_printable(_message_of(exc))}"
        request_id = exc.request_id or _request_id_of(exc)
        if request_id:
            line += f" (request_id {_printable(request_id)})"
        print(line, file=sys.stderr)
        return 1
    # No code: not an envelope the API produced -- an edge page, a proxy, or a
    # body that did not parse. The status is all there is, and saying so is
    # honest.
    print(
        f"anona: the server answered HTTP {exc.status_code}: {_printable(exc.detail)}",
        file=sys.stderr,
    )
    return 1


def _transport_reason(exc: AnonaError) -> str:
    """The cause, in words, out of the SDK's transport message."""
    if exc.status_code == 408:
        return "the request timed out"
    detail = str(exc.detail)
    # "... : ConnectError('[Errno 111] Connection refused')" -> the inside.
    match = re.search(r"\w+\((?:'|\")(.*?)(?:'|\")\)", detail)
    if match:
        return match.group(1).replace("[Errno 111] ", "")
    return detail


def _request_id_of(exc: AnonaError) -> str | None:
    """The request id out of the envelope, when the header did not carry it.

    The SDK reads `X-Request-ID`, which is the right source; the envelope
    carries its own `request_id` too, and that is the one thing support needs
    to find the call in the logs, so losing it to a missing header would be a
    poor trade.
    """
    detail = exc.detail
    if isinstance(detail, dict):
        error = detail.get("error")
        if isinstance(error, dict):
            value = error.get("request_id")
            if isinstance(value, str) and value.strip():
                return value
    return None


def _message_of(exc: AnonaError) -> str:
    """The human half of an API error, however the SDK got hold of it."""
    detail = exc.detail
    if isinstance(detail, dict):
        error = detail.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return _with_field_errors(message, error)
        message = detail.get("message")
        if isinstance(message, str) and message.strip():
            return message
    return str(detail)


def _with_field_errors(message: str, error: dict) -> str:
    """Append the offending fields, which are the whole content of a 422.

    "Request validation failed" on its own does not say what is wrong with the
    request, and the fields that do were inside the dict nobody could read.
    """
    errors = error.get("errors")
    if not isinstance(errors, list):
        return message
    parts = []
    for item in errors[:3]:
        if not isinstance(item, dict):
            continue
        loc = item.get("loc")
        field = ".".join(str(p) for p in loc[1:]) if isinstance(loc, list) and len(loc) > 1 else None
        detail = item.get("msg")
        if isinstance(detail, str):
            parts.append(f"{field}: {detail}" if field else detail)
    return f"{message} -- {'; '.join(parts)}" if parts else message


# A write that stored nothing is not an error -- the server did what it was
# asked -- so it cannot share `_fail`'s exit code, and it must not share
# success's either. Its own code is what lets `anona record ... || handle` in a
# shell script tell the two apart.
NOTHING_EXTRACTED_EXIT = 3


def run_record(base_url: str, content: str, space: str | None) -> int:
    empty = _reject_empty("memory", content)
    if empty is not None:
        return empty
    got = _token_and_space(space)
    if got is None:
        return 1
    token, space = got
    try:
        with AnonaClient(api_key=token, base_url=base_url, on_retry=_retry_notice) as client:
            result = client.record(space_id=space, content=content)
    except AnonaError as exc:
        return _fail(exc, base_url)
    # A 201 means the write was accepted, not that anything was stored:
    # extraction runs over the content and short, context-free statements
    # often produce nothing. This verb printed "Stored" for all of them and
    # exited 0, so an agent recording a terse fact was told it had been kept
    # when it had not -- silent data loss reported as success.
    if not stored_something(result):
        # The server's own `message` where it sent one: it carries measured
        # advice about what to do, and showing it means that advice can be
        # improved server-side without a CLI release. Sanitized like any other
        # server text reaching a terminal.
        print(_printable(nothing_extracted_message(result, space)), file=sys.stderr)
        return NOTHING_EXTRACTED_EXIT
    ids = stored_ids(result)
    if not ids:
        # Accepted for background extraction: there is no outcome yet, and no
        # id to name. The CLI never asks for this today, but a server that
        # starts queueing a write on its own must not read as empty.
        print(f"Queued in space '{space}'. Extraction runs in the background.")
        return 0
    # The ids, because they are what `anona` cannot otherwise tell you and
    # what every other surface needs to edit or delete the memory. One item
    # usually extracts to more than one memory, so the count is not noise.
    noun = "memory" if len(ids) == 1 else "memories"
    print(f"Stored {len(ids)} {noun} in space '{space}': {', '.join(_printable(i) for i in ids)}")
    return 0


def _score(m: dict) -> str:
    # Three significant figures, not a fixed number of decimals: relevance is
    # close to two-valued (~1.1 for a near-verbatim match, ~1e-4 for a correct
    # paraphrase), and two decimals would print every paraphrase hit as 0.00.
    s = m.get("relevance_score")
    return "-" if not isinstance(s, (int, float)) else f"{s:.3g}"


def run_retrieve(
    base_url: str,
    query: str,
    space: str | None,
    *,
    as_json: bool = False,
    limit: int | None = None,
    min_score: float | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
) -> int:
    empty = _reject_empty("query", query)
    if empty is not None:
        return empty
    got = _token_and_space(space)
    if got is None:
        return 1
    token, space = got
    # Only what the caller named: an unset filter is omitted, never sent as a
    # default, so a bare `anona retrieve` sends exactly what it always has.
    kwargs = {
        k: v
        for k, v in (
            ("limit", limit),
            ("min_score", min_score),
            ("user_id", user_id),
            ("agent_id", agent_id),
            ("session_id", session_id),
        )
        if v is not None
    }
    try:
        with AnonaClient(api_key=token, base_url=base_url, on_retry=_retry_notice) as client:
            items = client.retrieve(space_id=space, query=query, **kwargs)
    except AnonaError as exc:
        return _fail(exc, base_url)
    if as_json:
        # The rows exactly as the API returned them. json.dumps escapes every
        # control character (ensure_ascii), so this is terminal-safe without
        # _printable, and the content still round-trips.
        doc: dict = {"results": list(items)}
        if items.searched is not None:
            doc["searched"] = items.searched
        print(json.dumps(doc))
        return 0
    if not items:
        print("No memories matched.")
        return 0
    for i, m in enumerate(items, 1):
        short = str(m.get("memory_id") or m.get("id") or "-")[:8]
        content = m.get("content") or m.get("text") or ""
        print(f"{i}. {_score(m)}  {_printable(short)}  {_printable(content)}")
    return 0


def run_reason(base_url: str, query: str, space: str | None) -> int:
    empty = _reject_empty("question", query)
    if empty is not None:
        return empty
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
        with AnonaClient(api_key=token, base_url=base_url, on_retry=_retry_notice) as client:
            answer = client.reason(space_id=space, query=query)
    except AnonaError as exc:
        return _fail(exc, base_url)
    if not answer or not answer.strip():
        # Distinct from an error and from "no memories matched": the call
        # succeeded and the space had nothing to build an answer from.
        print("No answer. The space may hold nothing on that topic yet.")
        return 0
    print(_printable_block(answer))
    return 0
