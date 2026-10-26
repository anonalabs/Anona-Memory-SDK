"""The resource verbs: spaces, memories, documents, jobs, profile, model,
user-profile, usage, llm-models, and the three space settings
(chat-settings, reason-settings, extraction-settings).

Each is a thin wrapper over one AnonaClient method, sharing everything else
with record/retrieve/reason (memory.py): the same credential (an API key, or
the temporary-profile token), the same `--json` contract (one document on
stdout, nothing there on failure), the same one-line error rendering and exit
codes, and the same sanitizers on every string the server sent.

Every verb that destroys something -- `memories delete`, `jobs cancel`,
`spaces delete`, `documents delete` and the three `... reset` -- refuses to
run without `--yes`. They never prompt: the caller is usually
an agent with no terminal to answer on, and a prompt it cannot see reads as a
hung command.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable

from anona import AnonaClient, AnonaError
from anona.client import _seg

from . import exits
from .credential import redact
from .memory import (
    EMPTY_INPUT_EXIT,
    Bearer,
    _after_queued,
    _invoke,
    _emit_json,
    _given,
    _job_ids,
    _printable,
    _printable_block,
    _token_and_space,
    exit_code,
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
        ("documents", "upload"),
        ("documents", "list"),
        ("documents", "get"),
        ("documents", "delete"),
        ("chat-settings", "get"),
        ("chat-settings", "set"),
        ("chat-settings", "reset"),
        ("reason-settings", "get"),
        ("reason-settings", "set"),
        ("reason-settings", "reset"),
        ("extraction-settings", "get"),
        ("extraction-settings", "set"),
        ("extraction-settings", "reset"),
    }
)

# The three per-space settings. Each PUT is a FULL REPLACE on the server: a
# field the body leaves out is cleared. So `set` reads the stored record,
# changes only the fields named on the command line, and writes every field
# back -- otherwise `reason-settings set --model X` would silently clear the
# space's depth. The field lists are the request models' fields exactly (a
# test reads them off the gateway's pydantic models): the API forbids unknown
# keys, and the GET response carries `space_id`, which must not be echoed.
SETTINGS_FIELDS: dict[str, tuple[str, ...]] = {
    "chat-settings": ("memory_limit", "memory_token_budget", "auto_record", "memory", "chat_model"),
    "reason-settings": ("model", "depth"),
    "extraction-settings": ("mode", "guidance", "custom_prompt", "labels", "free_form_entities"),
}


def _destroy_message(verb: tuple[str, str | None], args: argparse.Namespace, space: str | None) -> str | None:
    """What a destructive verb would do, or None when the verb destroys nothing."""
    where = f"in space '{_p(space or 'default')}'"
    if verb == ("memories", "delete"):
        return f"delete memory {_p(args.memory_id)} {where}"
    if verb == ("jobs", "cancel"):
        return f"cancel the queued parts of job {_p(args.job_id)} {where}"
    if verb == ("spaces", "delete"):
        return f"delete space '{_p(args.space_id)}' and every memory in it, permanently"
    if verb == ("documents", "delete"):
        return f"delete document {_p(args.document_id)} and the memories extracted from it {where}"
    if verb[0] in SETTINGS_FIELDS and verb[1] == "reset":
        return f"reset the {verb[0]} of space '{_p(space or 'default')}' to the platform defaults"
    return None


COMMANDS = (
    "spaces",
    "memories",
    "documents",
    "jobs",
    "profile",
    "model",
    "user-profile",
    "usage",
    "llm-models",
    "chat-settings",
    "reason-settings",
    "extraction-settings",
)


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


def non_empty(value: str) -> str:
    """A space or scope value. Empty or blank is a usage error (exit 2), never
    sent: an empty scope is almost certainly a variable that was not set, and
    an empty space reaches the server as no space at all."""
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return value


def _read_arg_file(value: str, flag: str) -> str:
    """`@path` reads the value from a file; anything else is the value itself."""
    if not value.startswith("@"):
        return value
    try:
        with open(value[1:], encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise argparse.ArgumentTypeError(
            f"could not read {_printable(redact(value[1:]))} ({type(exc).__name__})"
        ) from None


def tag_groups(value: str) -> list:
    """`--tag-groups`: a JSON array of tag-group expressions (one object is
    taken as a one-element array), or `@path` to read it from a file.

    Only the shape is checked here; the grammar and the limits are the
    server's, so a change there needs no CLI release.
    """
    text = _read_arg_file(value, "--tag-groups")
    try:
        parsed = json.loads(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            'must be JSON, e.g. \'[{"tags": ["project:alpha"], "match": "any"}]\''
        ) from None
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list) or not parsed or not all(isinstance(g, dict) for g in parsed):
        raise argparse.ArgumentTypeError("must be a JSON array of one or more objects")
    return parsed


def _text_arg(value: str) -> str:
    return _read_arg_file(value, "text")


def _labels(value: str) -> list:
    text = _read_arg_file(value if value.startswith("@") else "@" + value, "--labels-file")
    try:
        parsed = json.loads(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must hold a JSON array of label groups") from None
    if not isinstance(parsed, list) or not all(isinstance(g, dict) for g in parsed):
        raise argparse.ArgumentTypeError("must hold a JSON array of label-group objects")
    return parsed


def add_parsers(sub, common: argparse.ArgumentParser, space_help: str, key_help: str, json_help: str) -> None:
    """Register every resource verb on the top-level subparsers."""

    def creds(parser: argparse.ArgumentParser, space_verb: str | None) -> None:
        if space_verb is not None:
            parser.add_argument("--space", type=non_empty, default=None, help=space_help.format(verb=space_verb))
        parser.add_argument("--api-key", default=None, help=key_help)
        parser.add_argument("--json", action="store_true", help=json_help)

    def scope(parser: argparse.ArgumentParser) -> None:
        for name in ("user", "agent", "session"):
            parser.add_argument(
                f"--{name}-id", type=non_empty, default=None, help=f"Only memories recorded with this {name}_id"
            )

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
    sd = spaces.add_parser("delete", parents=[common], help="Delete a space and every memory in it (needs --yes)")
    sd.add_argument("space_id", help="The space to delete")
    sd.add_argument("--yes", action="store_true", help="Confirm the deletion; without it nothing is sent")
    creds(sd, None)

    mp = sub.add_parser("memories", parents=[common], help="List, edit, delete or trace stored memories")
    mems = mp.add_subparsers(dest="action", required=True)
    ls = mems.add_parser("list", parents=[common], help="Page through a space's memories")
    creds(ls, "list")
    ls.add_argument("--limit", type=_limit(1, 200), default=None, help="At most this many (1-200, default 50)")
    ls.add_argument("--offset", type=_offset, default=None, help="Skip this many first (default 0)")
    ls.add_argument("--q", default=None, help="Only memories whose text or context contains this (case-insensitive)")
    ls.add_argument("--type", default=None, dest="memory_type", help="Only memories of this type: fact, experience, note or summary (facts a note covers are hidden unless --no-prefer-observations)")
    ls.add_argument(
        "--prefer-observations",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="List a synthesized note instead of the facts behind it (the server default); "
        "--no-prefer-observations lists both layers",
    )
    ls.add_argument(
        "--member-id",
        default=None,
        help="Only memories this member wrote; `me` for your own (in a personal space every memory is yours, so `me` matches them all)",
    )
    ls.add_argument(
        "--tag-groups",
        type=tag_groups,
        default=None,
        help="Compound tag filter: a JSON array of {tags, match} / {and} / {or} / {not}; @FILE reads it",
    )
    scope(ls)
    ed = mems.add_parser("edit", parents=[common], help="Correct a fact's text in place (a note is rebuilt from its facts and cannot be edited)")
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

    dp = sub.add_parser("documents", parents=[common], help="Upload files into a space, list, show or delete them")
    docs = dp.add_subparsers(dest="action", required=True)
    du = docs.add_parser("upload", parents=[common], help="Upload files to be read into memories")
    du.add_argument("paths", nargs="+", metavar="PATH", help="The files to upload (at most 20 per call)")
    creds(du, "upload to")
    du.add_argument("--tags", default=None, help="Comma-separated tags for every uploaded file")
    du.add_argument("--strategy", default=None, help="A named ingestion strategy (default: the space's)")
    for name in ("user", "agent", "session"):
        du.add_argument(f"--{name}-id", type=non_empty, default=None, help=f"Upload under this {name}_id")
    wait_flags(du)
    dl_ = docs.add_parser("list", parents=[common], help="List a space's documents, newest first")
    creds(dl_, "list")
    dl_.add_argument("--limit", type=_limit(1, 500), default=None, help="At most this many (1-500, default 100)")
    dl_.add_argument("--offset", type=_offset, default=None, help="Skip this many first")
    dl_.add_argument(
        "--source",
        choices=("file", "custom", "memory"),
        default=None,
        help="Only this kind: file (uploaded), custom (your document_id), memory (implicit)",
    )
    dg = docs.add_parser("get", parents=[common], help="Show one document")
    dg.add_argument("document_id", help="The document's id")
    creds(dg, "read")
    dd = docs.add_parser("delete", parents=[common], help="Delete a document and its memories (needs --yes)")
    dd.add_argument("document_id", help="The document to delete")
    dd.add_argument("--yes", action="store_true", help="Confirm the deletion; without it nothing is sent")
    creds(dd, "delete from")

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
    up.add_argument("user_id", type=non_empty, help="The user_id your writes are scoped with")
    up.add_argument("--limit", type=_limit(1, 200), default=None, help="At most this many memories (1-200)")
    up.add_argument("--offset", type=_offset, default=None, help="Skip this many memories first")
    creds(up, "read")
    creds(sub.add_parser("usage", parents=[common], help="Show your credits and rate limit"), None)
    creds(
        sub.add_parser(
            "llm-models",
            parents=[common],
            help="List the LLMs you can choose and what each costs (not the memory models: see `model`)",
        ),
        None,
    )
    _settings_parsers(sub, common, creds)


def wait_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Poll the background job(s) until they finish; the exit code reports the outcome",
    )
    parser.add_argument(
        "--wait-timeout",
        type=_seconds,
        default=None,
        metavar="SECONDS",
        help="With --wait, give up after this long and exit 8 (default 300)",
    )


def _seconds(value: str) -> float:
    try:
        x = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number of seconds") from None
    if not (x >= 0 and x < float("inf")):
        raise argparse.ArgumentTypeError("must be 0 or more")
    return x


def _bounded(low: int, high: int) -> Callable[[str], int]:
    return _limit(low, high)


def _settings_parsers(sub, common, creds) -> None:
    """chat-settings / reason-settings / extraction-settings: get, set, reset."""
    blurbs = {
        "chat-settings": "Show or change a space's defaults for the drop-in LLM proxy",
        "reason-settings": "Show or change which model and depth `reason` uses in a space",
        "extraction-settings": "Show or change how a space turns text into memories",
    }
    for kind, fields in SETTINGS_FIELDS.items():
        top = sub.add_parser(kind, parents=[common], help=blurbs[kind])
        acts = top.add_subparsers(dest="action", required=True)
        creds(acts.add_parser("get", parents=[common], help=f"Show the space's {kind}"), "read")
        st = acts.add_parser(
            "set",
            parents=[common],
            help="Change the named fields and keep the rest (read, modify, write back)",
        )
        creds(st, "configure")
        if kind == "reason-settings":
            st.add_argument("--model", default=None, help="A model id or tier name from `anona llm-models`")
            st.add_argument("--depth", choices=("fast", "thorough"), default=None, help="How deep `reason` reads")
        elif kind == "chat-settings":
            st.add_argument("--memory-limit", type=_bounded(1, 20), default=None, help="Memories injected per turn (1-20)")
            st.add_argument(
                "--memory-token-budget",
                type=_bounded(1, 100_000),
                default=None,
                help="Token cap on the injected block (1-100000)",
            )
            st.add_argument(
                "--auto-record",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="Record each proxied turn",
            )
            st.add_argument(
                "--memory",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="Inject memories into proxied calls",
            )
            st.add_argument("--chat-model", default=None, help="The LLM proxied calls use (see `anona llm-models`)")
        else:
            st.add_argument(
                "--mode",
                choices=("concise", "verbose", "verbatim", "custom"),
                default=None,
                help="How much of the text becomes memories",
            )
            st.add_argument("--guidance", type=_text_arg, default=None, help="Added to the extraction rules; @FILE reads it")
            st.add_argument(
                "--custom-prompt",
                type=_text_arg,
                default=None,
                help="Replaces the rules (needs --mode custom); @FILE reads it",
            )
            st.add_argument(
                "--labels-file",
                type=_labels,
                default=None,
                dest="labels",
                metavar="PATH",
                help="A JSON array of label groups to classify memories by, e.g. "
                '[{"key":"team","type":"value","values":[{"value":"billing"}]}]; '
                'each value is an object with "value", not a plain string',
            )
            st.add_argument(
                "--free-form-entities",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="Keep entities outside the label groups (--no-... needs labels)",
            )
        st.add_argument(
            "--clear",
            action="append",
            choices=fields,
            default=None,
            metavar="FIELD",
            help=f"Unset a field so it follows the platform default; repeatable. One of: {', '.join(fields)}",
        )
        rs = acts.add_parser("reset", parents=[common], help=f"Clear every {kind} override (needs --yes)")
        rs.add_argument("--yes", action="store_true", help="Confirm the reset; without it nothing is sent")
        creds(rs, "reset")


def precheck(args: argparse.Namespace) -> int | None:
    """Usage errors the server would certainly reject, caught before the
    credential is looked up, so they exit 2 and send nothing."""
    verb = verb_of(args)
    if verb == ("spaces", "create"):
        return _reject_blank("space name", args.name)
    if verb == ("memories", "edit"):
        return _reject_blank("memory text", args.text)
    if verb == ("documents", "upload"):
        for path in args.paths:
            if not os.path.isfile(path):
                print(
                    f"anona: {_p(path)} is not a readable file. Nothing was sent.",
                    file=sys.stderr,
                )
                return EMPTY_INPUT_EXIT
        return _reject_wait_timeout(args)
    if verb[0] in SETTINGS_FIELDS and verb[1] == "set":
        changes = settings_changes(args)
        if isinstance(changes, int):
            return changes
        if not changes:
            print(
                f"anona: name at least one field to change (see `anona {verb[0]} set --help`). "
                "Nothing was sent.",
                file=sys.stderr,
            )
            return EMPTY_INPUT_EXIT
    return None


def _reject_wait_timeout(args: argparse.Namespace) -> int | None:
    if getattr(args, "wait_timeout", None) is not None and not getattr(args, "wait", False):
        print("anona: --wait-timeout needs --wait. Nothing was sent.", file=sys.stderr)
        return EMPTY_INPUT_EXIT
    return None


def settings_changes(args: argparse.Namespace) -> dict | int:
    """{field: new value} from a `... set` command line; None means clear it."""
    kind = args.command
    changes: dict = {}
    for field in SETTINGS_FIELDS[kind]:
        value = getattr(args, field, None)
        if value is not None:
            changes[field] = value
    for field in args.clear or ():
        if field in changes:
            print(
                f"anona: {field} is both set and cleared. Nothing was sent.",
                file=sys.stderr,
            )
            return EMPTY_INPUT_EXIT
        changes[field] = None
    return changes


def _reject_blank(what: str, value: str) -> int | None:
    if value.strip():
        return None
    print(f"anona: the {what} is empty. Nothing was sent.", file=sys.stderr)
    return EMPTY_INPUT_EXIT


def refuse_unconfirmed(args: argparse.Namespace, space: str | None) -> int | None:
    """Exit 2, naming what would have gone, when a destructive verb lacks --yes."""
    what = _destroy_message(verb_of(args), args, space)
    if what is None or args.yes:
        return None
    print(f"anona: this would {what}. Pass --yes to do it. Nothing was sent.", file=sys.stderr)
    return EMPTY_INPUT_EXIT


def run(args: argparse.Namespace, base_url: str, space: str | None, api_key: str | None) -> int:
    verb = verb_of(args)
    refused = refuse_unconfirmed(args, space)
    if refused is not None:
        return refused
    if api_key is not None:
        bearer = Bearer(api_key, space)
    else:
        got = _token_and_space(space)
        if isinstance(got, int):
            return got
        bearer = got
        space = bearer.space
    if verb == ("documents", "upload"):
        return _run_upload(args, base_url, bearer, space)
    done, doc = _invoke(base_url, bearer, lambda client: _call(client, verb, args, space))
    if not done:
        return exit_code(doc)  # already reported
    if verb == ("jobs", "get"):
        from . import jobs

        if jobs.is_not_found(doc):
            # HTTP 200 on the wire, but a not-found outcome: report it the way
            # a 404 is reported, nothing on stdout, --json or not.
            print(jobs.NOT_FOUND_LINE, file=sys.stderr)
            return exits.NOT_FOUND
    if args.json:
        _emit_json(doc)
        return 0
    _RENDER[verb](doc, args, space)
    return 0


def _run_upload(args: argparse.Namespace, base_url: str, bearer: Bearer, space: str | None) -> int:
    """`documents upload`: every file in one multipart request.

    Never retried by the SDK (a multipart body cannot be re-sent safely), and
    a 413 or 415 comes back as its code -- `upload_too_large`,
    `file_too_large`, `unsupported_file_type`, `file_type_mismatch`,
    `file_expands_too_much` -- with the server's message saying the limit.
    """
    if space is None:
        raise ValueError("documents upload needs a space")
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None
    files: list = []
    data: dict = {}
    try:
        for path in args.paths:
            part, data = AnonaClient._upload_parts(
                path, None, args.strategy, tags, args.user_id, args.agent_id, args.session_id
            )
            files.extend(part)
    except AnonaError as exc:
        # The SDK's own per-file ceiling, checked before anything is sent.
        print(f"anona: {_p(exc.detail)} Nothing was sent.", file=sys.stderr)
        return exits.USAGE
    except OSError as exc:
        print(f"anona: could not read a file ({type(exc).__name__}). Nothing was sent.", file=sys.stderr)
        return exits.USAGE

    def send(client: AnonaClient):
        resp = client._get_client().post(
            f"{client._base_url}/v1/spaces/{_seg(space)}/documents", files=files, data=data
        )
        client._raise(resp)
        return resp.json()

    done, result = _invoke(base_url, bearer, send)
    if not done:
        return exit_code(result)
    wait = None
    if args.wait:
        wait = args.wait_timeout if args.wait_timeout is not None else DEFAULT_WAIT
    # An upload's job has no item count to judge "nothing stored" by.
    return _after_queued(
        base_url,
        bearer,
        space,
        result,
        _job_ids(result),
        as_json=args.json,
        wait=wait,
        nothing_stored_is_an_outcome=False,
    )


DEFAULT_WAIT = 300.0


def _settings_set(client: AnonaClient, kind: str, space: str, changes: dict) -> dict:
    """Read the stored record, apply `changes`, write every field back.

    The PUT is a full replace, so sending only the changed fields would clear
    the others. Only the request model's fields are sent: the GET carries
    `space_id`, and the API rejects unknown keys.
    """
    path = f"/v1/spaces/{_seg(space)}/{kind}"
    current = client._call("GET", path) or {}
    body = {field: current.get(field) for field in SETTINGS_FIELDS[kind]}
    body.update(changes)
    return client._call("PUT", path, json=body) or {}


def _call(client: AnonaClient, verb: tuple[str, str | None], args: argparse.Namespace, space: str | None):
    if verb == ("spaces", "list"):
        # The whole response: list_spaces() returns only the rows and drops
        # `total`, and --json promises the API's document.
        return client._call("GET", "/v1/spaces/")
    if verb == ("spaces", "create"):
        return client.create_space(args.name, args.description)
    if verb == ("spaces", "get"):
        return client.get_space(args.space_id)
    if verb == ("spaces", "delete"):
        client.delete_space(args.space_id)
        return {"deleted": True, "space_id": args.space_id}
    if verb == ("usage", None):
        # /v1/usage/me, the key-scoped snapshot: /v1/usage is a dashboard
        # session route and refuses an API key.
        return client.get_usage()
    if verb == ("llm-models", None):
        return client.list_catalog_models()
    if space is None:
        raise ValueError(f"{verb} needs a space")
    if verb == ("memories", "list"):
        params = _given(
            limit=50 if args.limit is None else args.limit,
            offset=0 if args.offset is None else args.offset,
            q=args.q,
            type=args.memory_type,
            prefer_observations=args.prefer_observations,
            user_id=args.user_id,
            agent_id=args.agent_id,
            session_id=args.session_id,
            member_id=args.member_id,
            # A GET, so the expression travels as a JSON string.
            tag_groups=None if args.tag_groups is None else json.dumps(args.tag_groups, separators=(",", ":")),
        )
        return client._call("GET", f"/v1/spaces/{_seg(space)}/memories", params=params)
    if verb == ("documents", "list"):
        params = _given(
            limit=100 if args.limit is None else args.limit,
            offset=0 if args.offset is None else args.offset,
            source=args.source,
        )
        return client._call("GET", f"/v1/spaces/{_seg(space)}/documents", params=params)
    if verb == ("documents", "get"):
        return client.get_document(space, args.document_id)
    if verb == ("documents", "delete"):
        client.delete_document(space, args.document_id)
        return {"deleted": True, "document_id": args.document_id, "space_id": space}
    if verb[0] in SETTINGS_FIELDS:
        path = f"/v1/spaces/{_seg(space)}/{verb[0]}"
        if verb[1] == "get":
            return client._call("GET", path)
        if verb[1] == "set":
            changes = settings_changes(args)
            assert isinstance(changes, dict)  # precheck already refused the rest
            return _settings_set(client, verb[0], space, changes)
        client._call("DELETE", path, expect_no_content=True)
        return {"reset": True, "space_id": space, "settings": verb[0]}
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


def _count(value: object) -> int:
    """A non-negative integer from the server, or 0. Not a bool: True is an
    int in Python and would print as an offset of 1."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


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
    offset = _count(d.get("offset"))
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
    from . import jobs

    jobs.render(_d(doc), args.job_id)


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


def _render_space_deleted(doc, args, space) -> None:
    print(f"Deleted space '{_p(args.space_id)}'.")


def _render_documents(doc, args, space) -> None:
    d = _d(doc)
    rows = _rows(d, "documents")
    if not rows:
        print(f"No documents in space '{_p(space)}'.")
        return
    for r in rows:
        line = f"{_p(r.get('document_id'))}  {_p(r.get('source'))}"
        if r.get("memory_count") is not None:
            line += f"  {_p(r['memory_count'])} memories"
        if r.get("created_at"):
            line += f"  {_p(r['created_at'])}"
        print(line)
    offset = _count(d.get("offset"))
    print(f"Showing {offset + 1}-{offset + len(rows)} of {_p(d.get('total'))}.")


def _render_document(doc, args, space) -> None:
    d = _d(doc)
    print(f"Document {_p(d.get('id') or d.get('document_id') or args.document_id)}")
    for key, value in d.items():
        if key in ("id", "document_id") or isinstance(value, (dict, list)):
            continue
        print(f"{_printable(key)}: {_p(value)}")


def _render_document_deleted(doc, args, space) -> None:
    print(f"Deleted document {_p(args.document_id)} from space '{_p(space)}'.")


def _render_llm_models(doc, args, space) -> None:
    rows = _rows(doc, "data")
    if not rows:
        print("No models listed.")
    for m in rows:
        line = f"{_p(m.get('id'))}  {_p(m.get('display_name'))}"
        line += f"  {_p(m.get('credits_per_1k_input'))}/{_p(m.get('credits_per_1k_output'))} credits per 1K in/out"
        flags = []
        if m.get("default"):
            flags.append("default")
        if m.get("alias"):
            flags.append(f"alias {_p(m['alias'])}")
        if m.get("supports_memory_operations") is False:
            flags.append("chat proxy only")
        if flags:
            line += f"  ({', '.join(flags)})"
        print(line)


def _render_settings(doc, args, space) -> None:
    d = _d(doc)
    print(f"{_printable(args.command)} for space '{_p(d.get('space_id') or space)}':")
    for field in SETTINGS_FIELDS[args.command]:
        value = d.get(field)
        if value is None:
            shown = "(platform default)"
        elif isinstance(value, (dict, list)):
            shown = _printable(json.dumps(value, ensure_ascii=True))
        else:
            shown = _p(value)
        print(f"  {field}: {shown}")


def _render_settings_reset(doc, args, space) -> None:
    print(f"Reset the {_printable(args.command)} of space '{_p(space)}' to the platform defaults.")


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
    ("spaces", "delete"): _render_space_deleted,
    ("documents", "list"): _render_documents,
    ("documents", "get"): _render_document,
    ("documents", "delete"): _render_document_deleted,
    ("llm-models", None): _render_llm_models,
}
for _kind in SETTINGS_FIELDS:
    _RENDER[(_kind, "get")] = _render_settings
    _RENDER[(_kind, "set")] = _render_settings
    _RENDER[(_kind, "reset")] = _render_settings_reset
