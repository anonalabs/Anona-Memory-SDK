"""`anona record`, `anona retrieve` and `anona reason`: the non-MCP path.

MCP is the primary surface for an agent. These exist for a script, a quick
check from a terminal, somebody who will not enable MCP, and the session
before a client restart has loaded the MCP tools.

Two credentials work here. An API key -- from `--api-key`,
ANONA_API_KEY or ~/.anona/config.env, resolved in credential.py and bound to a
host in target.for_api_key -- needs a space named, because an account can hold
many and none is implied. Without a key, the stored temporary-profile token is
used, and `--space` defaults to the profile's one space, "default".

The `anona login` token is deliberately NOT one of them: it is MCP-scoped and
the data plane refuses it, so that an MCP credential cannot be replayed
against the REST API. MCP and these verbs are decoupled on purpose.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict
from urllib.parse import urlsplit

import httpx

from anona import AnonaClient, AnonaError
from anona._record_outcome import (
    nothing_extracted_message,
    stored_ids,
    stored_something,
)

from .credential import redact
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


# A missing, refused or rejected credential: no credential at all, an MCP
# login with no API key beside it, a host the credential may not go to, or a
# 401. Distinct from 1 (the request was made and failed) so a script can tell
# "fix the credential" from "try again".
AUTH_EXIT = 4

_KEY_ROUTE = (
    "an API key: pass --api-key, set ANONA_API_KEY, or put ANONA_API_KEY in "
    "~/.anona/config.env (create a key in the dashboard under API keys)"
)


def _token_and_space(space: str | None) -> tuple[str, str] | int:
    """(token, space) off the stored temp profile, or the exit code after
    printing why it cannot proceed.

    Token precedence comes from `proxy._bearer`, which already decides that a
    signed-in OAuth credential beats a temp token. Re-deriving that rule here
    is how the proxy and these verbs would disagree about which credential a
    write used.
    """
    if load() is None:
        print(
            f"anona: no credential. Use {_KEY_ROUTE};\n"
            "or run `anona start` for a temporary profile with no account.",
            file=sys.stderr,
        )
        return AUTH_EXIT
    with httpx.Client(timeout=30.0) as hc:
        try:
            token, is_temp = _bearer(hc)
        except NotLoggedIn as exc:
            print(f"anona: {exc}", file=sys.stderr)
            return AUTH_EXIT
        except httpx.HTTPError as exc:
            # _bearer refreshes with a raw httpx.Client, outside the SDK's
            # transport-error translation, so this one can really fire.
            print(f"anona: could not refresh the login ({type(exc).__name__}).", file=sys.stderr)
            return 1
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
            "anona: the `anona login` credential is for MCP (`anona mcp`) only; "
            "the REST API refuses it.\n"
            f"`record`, `retrieve` and `reason` need {_KEY_ROUTE};\n"
            "or a temporary profile (`anona start`).",
            file=sys.stderr,
        )
        return AUTH_EXIT
    if space is None:
        space = "default"  # a temp profile's only space, fixed server-side
    return token, space


def _credential(space: str | None, api_key: str | None) -> tuple[str, str] | int:
    if api_key is not None:
        # The caller (main) resolved the key, bound its host and required the
        # space before getting here.
        if space is None:
            raise ValueError("an API key needs a space")
        return api_key, space
    return _token_and_space(space)


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


def read_content(positional: str | None, path: str | None) -> str | int:
    """The text `record` stores: the argument, stdin for `-`, or `--file`.

    Exactly one source. Naming two is ambiguous and naming none leaves nothing
    to store, and both are usage errors that send nothing. An empty result is
    left to run_record's own check, so all three sources share one message.
    """
    if positional is not None and path is not None:
        print("anona: give the content or --file, not both. Nothing was sent.", file=sys.stderr)
        return EMPTY_INPUT_EXIT
    if path is not None:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            print(
                # Redacted as well as sanitized: a key pasted into the wrong
                # flag would otherwise be echoed straight back.
                f"anona: could not read {_printable(redact(path))}: {type(exc).__name__}. "
                "Nothing was sent.",
                file=sys.stderr,
            )
            return EMPTY_INPUT_EXIT
    if positional is None:
        print(
            "anona: nothing to record. Pass the content, `-` to read it from stdin, "
            "or --file PATH. Nothing was sent.",
            file=sys.stderr,
        )
        return EMPTY_INPUT_EXIT
    if positional == "-":
        return sys.stdin.read()
    return positional


def _emit_json(doc: object) -> None:
    """One JSON document on stdout, and nothing else there.

    Only ever called after the call succeeded, so a failure leaves stdout empty
    and its one-line rendering on stderr. ensure_ascii escapes every control
    character and every non-ASCII one, so this is terminal-safe without
    _printable and the content still round-trips exactly.
    """
    print(json.dumps(doc, ensure_ascii=True))


def _given(**values: object) -> dict:
    """Only what the caller named: an unset flag is omitted, never sent as a
    default, so a bare command sends exactly what it always has."""
    return {k: v for k, v in values.items() if v is not None}


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
    rc = AUTH_EXIT if exc.status_code == 401 else 1
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
        return rc
    # No code: not an envelope the API produced -- an edge page, a proxy, or a
    # body that did not parse. The status is all there is, and saying so is
    # honest.
    print(
        f"anona: the server answered HTTP {exc.status_code}: {_printable(exc.detail)}",
        file=sys.stderr,
    )
    return rc


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


def run_record(
    base_url: str,
    content: str,
    space: str | None,
    *,
    api_key: str | None = None,
    as_json: bool = False,
    tags: list[str] | None = None,
    timestamp: str | None = None,
    metadata: dict | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
) -> int:
    empty = _reject_empty("memory", content)
    if empty is not None:
        return empty
    got = _credential(space, api_key)
    if isinstance(got, int):
        return got
    token, space = got
    kwargs = _given(
        tags=tags,
        timestamp=timestamp,
        metadata=metadata,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
    )
    try:
        with AnonaClient(api_key=token, base_url=base_url, on_retry=_retry_notice) as client:
            result = client.record(space_id=space, content=content, **kwargs)
    except AnonaError as exc:
        return _fail(exc, base_url)
    if as_json:
        # The response as the API returned it. The exit code still says what
        # happened: a write that stored nothing exits 3 with its document on
        # stdout and the server's advice on stderr, exactly as without --json.
        _emit_json(result)
        if not stored_something(result):
            print(_printable(nothing_extracted_message(result, space)), file=sys.stderr)
            return NOTHING_EXTRACTED_EXIT
        return 0
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
    tags: list[str] | None = None,
    tags_match: str | None = None,
    as_of: str | None = None,
    occurred_after: str | None = None,
    occurred_before: str | None = None,
    api_key: str | None = None,
) -> int:
    empty = _reject_empty("query", query)
    if empty is not None:
        return empty
    got = _credential(space, api_key)
    if isinstance(got, int):
        return got
    token, space = got
    kwargs = _given(
        limit=limit,
        min_score=min_score,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        tags=tags,
        tags_match=tags_match,
        as_of=as_of,
        occurred_after=occurred_after,
        occurred_before=occurred_before,
    )
    try:
        with AnonaClient(api_key=token, base_url=base_url, on_retry=_retry_notice) as client:
            items = client.retrieve(space_id=space, query=query, **kwargs)
    except AnonaError as exc:
        return _fail(exc, base_url)
    if as_json:
        # The rows exactly as the API returned them; an empty result is
        # {"results": []} with exit 0.
        doc: dict = {"results": list(items)}
        if items.searched is not None:
            doc["searched"] = items.searched
        _emit_json(doc)
        return 0
    if not items:
        print("No memories matched.")
        return 0
    for i, m in enumerate(items, 1):
        short = str(m.get("memory_id") or m.get("id") or "-")[:8]
        content = m.get("content") or m.get("text") or ""
        print(f"{i}. {_score(m)}  {_printable(short)}  {_printable(content)}")
    return 0


def run_reason(
    base_url: str,
    query: str,
    space: str | None,
    *,
    api_key: str | None = None,
    as_json: bool = False,
    depth: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
) -> int:
    empty = _reject_empty("question", query)
    if empty is not None:
        return empty
    got = _credential(space, api_key)
    if isinstance(got, int):
        return got
    token, space = got
    # A synthesis is an agent loop over the space, not a lookup: it runs tens
    # of seconds and sometimes past a minute. Without this the command looks
    # hung, and the obvious reaction -- Ctrl-C and retry -- costs a second run
    # of the same work. stderr, so `anona reason ... > answer.md` still gets
    # only the answer.
    print(f"Reasoning over space '{space}'. This usually takes under a minute.", file=sys.stderr)
    kwargs = _given(depth=depth, user_id=user_id, agent_id=agent_id, session_id=session_id)
    try:
        with AnonaClient(api_key=token, base_url=base_url, on_retry=_retry_notice) as client:
            # The receipt form: same request, same answer, plus what it was
            # built from (`sources`), which --json is the only way to see.
            receipt = client.reason_receipt(space_id=space, query=query, **kwargs)
    except AnonaError as exc:
        return _fail(exc, base_url)
    if as_json:
        _emit_json(asdict(receipt))
        return 0
    answer = receipt.insights
    if not answer or not answer.strip():
        # Distinct from an error and from "no memories matched": the call
        # succeeded and the space had nothing to build an answer from.
        print("No answer. The space may hold nothing on that topic yet.")
        return 0
    print(_printable_block(answer))
    return 0
