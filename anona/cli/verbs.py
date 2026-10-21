"""The resource verbs: spaces, memories, jobs, profile, model, user-profile, usage.

Each is a thin wrapper over one AnonaClient method, sharing everything else
with record/retrieve/reason (memory.py): the same credential (an API key, or
the temporary-profile token), the same `--json` contract (one document on
stdout, nothing there on failure), the same one-line error rendering and exit
codes, and the same sanitizers on every string the server sent.

Two verbs destroy something -- `memories delete` and `jobs cancel` -- and
both refuse to run without `--yes`. They never prompt: the caller is usually
an agent with no terminal to answer on, and a prompt it cannot see reads as a
hung command.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from anona import AnonaClient, AnonaError

from .credential import redact
from .memory import (
    EMPTY_INPUT_EXIT,
    _emit_json,
    _fail,
    _given,
    _printable,
    _printable_block,
    _retry_notice,
    _token_and_space,
)

# The verbs that address one space, and so need one named with an API key.
SPACE_VERBS = frozenset(
    {
        ("memories", "list"),
        ("memories", "edit"),
        ("memories", "delete"),
        ("memories", "history"),
        ("jobs", "get"),
        ("jobs", "cancel"),
        ("profile", None),
        ("model", None),
        ("user-profile", None),
    }
)

# Destructive: refused without --yes, before the credential is touched.
_DESTRUCTIVE = {
    ("memories", "delete"): ("delete memory", "memory_id"),
    ("jobs", "cancel"): ("cancel the queued parts of job", "job_id"),
}

COMMANDS = ("spaces", "memories", "jobs", "profile", "model", "user-profile", "usage")


def verb_of(args: argparse.Namespace) -> tuple[str, str | None]:
    return args.command, getattr(args, "action", None)


def _limit(low: int, high: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        n = int(value)
        if not low <= n <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return n

    return parse


def _offset(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return n


def add_parsers(sub, common: argparse.ArgumentParser, space_help: str, key_help: str, json_help: str) -> None:
    """Register every resource verb on the top-level subparsers."""

    def creds(parser: argparse.ArgumentParser, space_verb: str | None) -> None:
        if space_verb is not None:
            parser.add_argument("--space", default=None, help=space_help.format(verb=space_verb))
        parser.add_argument("--api-key", default=None, help=key_help)
        parser.add_argument("--json", action="store_true", help=json_help)

    def scope(parser: argparse.ArgumentParser) -> None:
        for name in ("user", "agent", "session"):
            parser.add_argument(f"--{name}-id", default=None, help=f"Only memories recorded with this {name}_id")

    sp = sub.add_parser("spaces", parents=[common], help="List, create or show spaces")
    spaces = sp.add_subparsers(dest="action", required=True)
    creds(spaces.add_parser("list", parents=[common], help="List the spaces you can use"), None)
    cr = spaces.add_parser("create", parents=[common], help="Create a space")
    cr.add_argument("name", help="The new space's name, which is also its id")
    cr.add_argument("--description", default=None, help="What the space is for")
    creds(cr, None)
    gt = spaces.add_parser("get", parents=[common], help="Show one space")
    gt.add_argument("space_id", help="The space's id (owner:name for one shared with you)")
    creds(gt, None)

    mp = sub.add_parser("memories", parents=[common], help="List, edit, delete or trace stored memories")
    mems = mp.add_subparsers(dest="action", required=True)
    ls = mems.add_parser("list", parents=[common], help="Page through a space's memories")
    creds(ls, "list")
    ls.add_argument("--limit", type=_limit(1, 200), default=None, help="At most this many (1-200, default 50)")
    ls.add_argument("--offset", type=_offset, default=None, help="Skip this many first (default 0)")
    ls.add_argument("--q", default=None, help="Only memories whose text contains this")
    ls.add_argument("--type", default=None, dest="memory_type", help="Only memories of this type")
    ls.add_argument(
        "--prefer-observations",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="List a synthesized note instead of the facts behind it (the server default); "
        "--no-prefer-observations lists both layers",
    )
    scope(ls)
    ed = mems.add_parser("edit", parents=[common], help="Correct one memory's text in place")
    ed.add_argument("memory_id", help="The memory to change")
    ed.add_argument("--text", required=True, help="The corrected text")
    ed.add_argument("--reason", default=None, help="Why, kept on the memory's history")
    creds(ed, "edit in")
    dl = mems.add_parser("delete", parents=[common], help="Delete one memory (needs --yes)")
    dl.add_argument("memory_id", help="The memory to delete")
    dl.add_argument("--yes", action="store_true", help="Confirm the deletion; without it nothing is sent")
    creds(dl, "delete from")
    hs = mems.add_parser("history", parents=[common], help="Show every earlier version of one memory")
    hs.add_argument("memory_id", help="The memory to trace")
    creds(hs, "read")

    jp = sub.add_parser("jobs", parents=[common], help="Check or cancel a background ingestion job")
    jobs = jp.add_subparsers(dest="action", required=True)
    jg = jobs.add_parser("get", parents=[common], help="Show a job's status and the memories it made")
    jg.add_argument("job_id", help="The job id a background write returned")
    creds(jg, "read")
    jc = jobs.add_parser("cancel", parents=[common], help="Cancel a job's queued parts (needs --yes)")
    jc.add_argument("job_id", help="The job to cancel")
    jc.add_argument("--yes", action="store_true", help="Confirm the cancel; without it nothing is sent")
    creds(jc, "cancel in")

    creds(sub.add_parser("profile", parents=[common], help="Show a space's profile: mission and disposition"), "read")
    md = sub.add_parser("model", parents=[common], help="Show one memory model and its current content")
    md.add_argument("model_id", help="The memory model's id")
    creds(md, "read")
    up = sub.add_parser("user-profile", parents=[common], help="Show what a space knows about one end user")
    up.add_argument("user_id", help="The user_id your writes are scoped with")
    up.add_argument("--limit", type=_limit(1, 200), default=None, help="At most this many memories (1-200)")
    up.add_argument("--offset", type=_offset, default=None, help="Skip this many memories first")
    creds(up, "read")
    creds(sub.add_parser("usage", parents=[common], help="Show your credits and rate limit"), None)


def precheck(args: argparse.Namespace) -> int | None:
    """Usage errors the server would certainly reject, caught before the
    credential is looked up, so they exit 2 and send nothing."""
    verb = verb_of(args)
    if verb == ("spaces", "create"):
        return _reject_blank("space name", args.name)
    if verb == ("memories", "edit"):
        return _reject_blank("memory text", args.text)
    return None


def _reject_blank(what: str, value: str) -> int | None:
    if value.strip():
        return None
    print(f"anona: the {what} is empty. Nothing was sent.", file=sys.stderr)
    return EMPTY_INPUT_EXIT


def refuse_unconfirmed(args: argparse.Namespace, space: str | None) -> int | None:
    """Exit 2, naming what would have gone, when a destructive verb lacks --yes."""
    verb = verb_of(args)
    if verb not in _DESTRUCTIVE or args.yes:
        return None
    what, attr = _DESTRUCTIVE[verb]
    print(
        f"anona: this would {what} {_p(getattr(args, attr))} in space "
        f"'{_p(space or 'default')}'. Pass --yes to do it. Nothing was sent.",
        file=sys.stderr,
    )
    return EMPTY_INPUT_EXIT


def run(args: argparse.Namespace, base_url: str, space: str | None, api_key: str | None) -> int:
    verb = verb_of(args)
    refused = refuse_unconfirmed(args, space)
    if refused is not None:
        return refused
    if api_key is not None:
        token = api_key
    else:
        got = _token_and_space(space)
        if isinstance(got, int):
            return got
        token, space = got
    try:
        with AnonaClient(api_key=token, base_url=base_url, on_retry=_retry_notice) as client:
            doc = _call(client, verb, args, space)
    except AnonaError as exc:
        return _fail(exc, base_url)
    if args.json:
        _emit_json(doc)
        return 0
    _RENDER[verb](doc, args, space)
    return 0


def _call(client: AnonaClient, verb: tuple[str, str | None], args: argparse.Namespace, space: str | None):
    if verb == ("spaces", "list"):
        # The whole response: list_spaces() returns only the rows and drops
        # `total`, and --json promises the API's document.
        return client._call("GET", "/v1/spaces/")
    if verb == ("spaces", "create"):
        return client.create_space(args.name, args.description)
    if verb == ("spaces", "get"):
        return client.get_space(args.space_id)
    if verb == ("usage", None):
        # /v1/usage/me, the key-scoped snapshot: /v1/usage is a dashboard
        # session route and refuses an API key.
        return client.get_usage()
    if space is None:
        raise ValueError(f"{verb} needs a space")
    if verb == ("memories", "list"):
        return client.list_memories(
            space,
            **_given(
                limit=args.limit,
                offset=args.offset,
                q=args.q,
                memory_type=args.memory_type,
                prefer_observations=args.prefer_observations,
                user_id=args.user_id,
                agent_id=args.agent_id,
                session_id=args.session_id,
            ),
        )
    if verb == ("memories", "edit"):
        return client.update_memory(space, args.memory_id, **_given(text=args.text, reason=args.reason))
    if verb == ("memories", "delete"):
        client.delete_memory(space, args.memory_id)
        # 204, no body: the document says what went, so a script has
        # something to read either way.
        return {"deleted": True, "memory_id": args.memory_id, "space_id": space}
    if verb == ("memories", "history"):
        return client.get_memory_history(space, args.memory_id)
    if verb == ("jobs", "get"):
        return client.get_job(space, args.job_id)
    if verb == ("jobs", "cancel"):
        return client.cancel_job(space, args.job_id)
    if verb == ("profile", None):
        return client.get_space_profile(space)
    if verb == ("model", None):
        return client.get_memory_model(space, args.model_id)
    if verb == ("user-profile", None):
        return client.get_user_profile(space, args.user_id, **_given(limit=args.limit, offset=args.offset))
    raise AssertionError(verb)


# --- plain-text output ------------------------------------------------------
#
# One line per item, ids in full (an agent copies them into the next
# command), every server string through _printable. Rows are read with
# .get(), so a field the server stops sending prints as missing rather than
# raising.


def _d(doc: object) -> dict:
    return doc if isinstance(doc, dict) else {}


def _rows(doc: object, key: str) -> list[dict]:
    rows = _d(doc).get(key)
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _p(value: object) -> str:
    # Redacted as well: an id argument echoed back may be a key pasted into
    # the wrong place.
    return "-" if value is None or value == "" else _printable(redact(str(value)))


def _space_line(s: dict) -> str:
    sid = s.get("qualified_id") or s.get("space_id")
    line = _p(sid)
    if s.get("shared_by"):
        line += f"  (shared by {_p(s['shared_by'])})"
    if s.get("description"):
        line += f"  {_p(s['description'])}"
    return line


def _memory_line(m: dict) -> str:
    content = m.get("content") or m.get("text") or ""
    return f"{_p(m.get('id') or m.get('memory_id'))}  {_p(m.get('type'))}  {_printable(content)}"


def _render_spaces_list(doc, args, space) -> None:
    rows = _rows(doc, "spaces")
    if not rows:
        print("No spaces.")
    for s in rows:
        print(_space_line(s))


def _render_space_created(doc, args, space) -> None:
    d = _d(doc)
    line = f"Created space '{_p(d.get('space_id') or args.name)}'."
    if d.get("space_type"):
        line += f" Type: {_p(d['space_type'])}."
    print(line)


def _render_space(doc, args, space) -> None:
    d = _d(doc)
    for key in ("space_id", "qualified_id", "name", "description", "shared_by", "created_at"):
        if d.get(key) is not None:
            print(f"{key}: {_p(d[key])}")


def _render_memories(doc, args, space) -> None:
    d = _d(doc)
    rows = _rows(d, "items")
    if not rows:
        print(f"No memories in space '{_p(space)}'.")
        return
    for m in rows:
        print(_memory_line(m))
    offset = d.get("offset") if isinstance(d.get("offset"), int) else 0
    total = d.get("total")
    print(f"Showing {offset + 1}-{offset + len(rows)} of {_p(total)}.")


def _render_edited(doc, args, space) -> None:
    print(f"Updated memory {_p(_d(doc).get('id') or args.memory_id)}.")


def _render_deleted(doc, args, space) -> None:
    print(f"Deleted memory {_p(args.memory_id)} from space '{_p(space)}'.")


def _render_history(doc, args, space) -> None:
    rows = _rows(doc, "history")
    if not rows:
        print(f"No earlier versions of memory {_p(args.memory_id)}.")
    for h in rows:
        line = f"{_p(h.get('changed_at'))}  was: {_p(h.get('previous_content'))}"
        if h.get("reason"):
            line += f"  (reason: {_p(h['reason'])})"
        print(line)


def _render_job(doc, args, space) -> None:
    d = _d(doc)
    line = f"Job {_p(d.get('job_id') or args.job_id)}: {_p(d.get('status'))}"
    if d.get("memory_count") is not None:
        line += f", {_p(d['memory_count'])} memories"
    if d.get("items_submitted") is not None:
        line += f" from {_p(d['items_submitted'])} items"
    print(line + ".")
    if d.get("error"):
        print(f"Error: {_p(d['error'])}")
    ids = d.get("memory_ids")
    if isinstance(ids, list) and ids:
        print("Memory ids: " + ", ".join(_p(i) for i in ids))


def _render_cancelled(doc, args, space) -> None:
    d = _d(doc)
    print(
        f"Cancelled {_p(d.get('cancelled_parts'))} queued part(s) of job "
        f"{_p(d.get('job_id') or args.job_id)}; {_p(d.get('running_parts'))} "
        "already running will finish."
    )


def _render_profile(doc, args, space) -> None:
    d = _d(doc)
    print(f"Space: {_p(d.get('space_id') or space)}")
    print(f"Mission: {_p(d.get('mission'))}")
    disp = d.get("disposition")
    if isinstance(disp, dict) and disp:
        print("Disposition: " + ", ".join(f"{_printable(k)}={_p(v)}" for k, v in disp.items()))


def _render_model(doc, args, space) -> None:
    d = _d(doc)
    print(f"{_p(d.get('name'))} ({_p(d.get('id') or args.model_id)})")
    if d.get("query"):
        print(f"Query: {_p(d['query'])}")
    content = d.get("content")
    if isinstance(content, str) and content.strip():
        # Markdown: paragraph breaks kept, as `reason` keeps them.
        print()
        print(_printable_block(content))
    else:
        print("No content yet: the first refresh has not finished, or it was cleared.")


def _render_user_profile(doc, args, space) -> None:
    d = _d(doc)
    print(
        f"User {_p(d.get('user_id') or args.user_id)} in space '{_p(d.get('space_id') or space)}': "
        f"{_p(d.get('memory_count'))} memories, first seen {_p(d.get('first_seen'))}, "
        f"last active {_p(d.get('last_active'))}."
    )
    for m in _rows(d, "memories"):
        print(_memory_line(m))


def _render_usage(doc, args, space) -> None:
    d = _d(doc)
    line = (
        f"Credits: {_p(d.get('credits_remaining'))} remaining of {_p(d.get('credits_limit'))} "
        f"({_p(d.get('credits_used'))} used"
    )
    if d.get("credits_banked"):
        line += f", {_p(d['credits_banked'])} banked"
    print(line + ").")
    print(f"Rate limit: {_p(d.get('rate_limit_per_min'))} requests a minute.")


_RENDER = {
    ("spaces", "list"): _render_spaces_list,
    ("spaces", "create"): _render_space_created,
    ("spaces", "get"): _render_space,
    ("memories", "list"): _render_memories,
    ("memories", "edit"): _render_edited,
    ("memories", "delete"): _render_deleted,
    ("memories", "history"): _render_history,
    ("jobs", "get"): _render_job,
    ("jobs", "cancel"): _render_cancelled,
    ("profile", None): _render_profile,
    ("model", None): _render_model,
    ("user-profile", None): _render_user_profile,
    ("usage", None): _render_usage,
}
