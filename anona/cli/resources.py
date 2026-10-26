"""The rest of the API as commands: memory models, rules, webhooks, the entity
graph, receipts, asking about one user, the proxy chat, exports, and the
account views (org, keys, members, invitations, billing, usage history).

verbs.py owns the machinery every resource verb shares -- the credential,
`--json`, the destructive-verb guard, the error rendering. This module is the
table of what each additional verb sends and prints, so a verb is one row
rather than one function.

Three families, told apart by who may call them:

* space verbs (models, rules, webhooks, graph, entities, ask-user, chat,
  exports, ...) work with an API key, the command-line login or a temporary
  profile, as the server decides;
* account verbs (org, orgs, projects, keys list, members, invitations,
  billing, usage history, exports) are dashboard-session routes that the
  command-line login is allowed to call. With an API key or a temporary
  profile the server answers `session_required` (exit 4); nothing is
  pre-refused here, so the server stays the single judge;
* dashboard-only verbs (creating or revoking a key, every billing change, org
  and member management) are never sent. They print one line naming the
  dashboard page and exit 4.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from anona import AnonaClient, AnonaError
from anona.client import _seg

from . import exits
from .memory import EMPTY_INPUT_EXIT, LINK_REFUSED, _given, _printable, _printable_block
from .verbs import _count, _d, _limit, _offset, _p, _render_model, _render_profile, _text_arg, non_empty

DASHBOARD = "https://memory.anonalabs.com"

# verb -> (what, dashboard path). `what` completes "anona: <what> is done in
# the dashboard: <URL>".
DASHBOARD_ONLY: dict[tuple[str, str | None], tuple[str, str]] = {
    ("keys", "create"): ("creating an API key", "/dashboard/keys"),
    ("keys", "revoke"): ("revoking an API key", "/dashboard/keys"),
    ("billing", "upgrade"): ("upgrading your plan", "/dashboard/billing"),
    ("billing", "checkout"): ("checking out a plan", "/dashboard/billing"),
    ("billing", "topup"): ("buying credits", "/dashboard/billing"),
    ("billing", "cancel"): ("cancelling your subscription", "/dashboard/billing"),
    ("billing", "portal"): ("managing payment details", "/dashboard/billing"),
    ("org", "rename"): ("renaming the organization", "/dashboard/profile"),
    ("org", "delete"): ("deleting the organization", "/dashboard/profile"),
    ("org", "transfer"): ("transferring the organization", "/dashboard/profile"),
    ("org", "leave"): ("leaving the organization", "/dashboard/profile"),
    ("members", "invite"): ("inviting a member", "/dashboard/members"),
    ("members", "remove"): ("removing a member", "/dashboard/members"),
    ("spaces", "members remove"): ("removing a space member", "/dashboard/spaces"),
}

# The top-level commands this module adds (routing-settings rides the three
# settings commands in verbs.py).
COMMANDS = (
    "models",
    "rules",
    "webhooks",
    "graph",
    "entities",
    "receipts",
    "ask-user",
    "space-types",
    "chat",
    "org",
    "orgs",
    "projects",
    "keys",
    "members",
    "invitations",
    "billing",
    "exports",
)

# Verbs that address one space, and so need one named with an API key.
SPACE_VERBS = frozenset(
    {("models", a) for a in ("list", "create", "get", "edit", "delete", "refresh", "clear", "history")}
    | {("profile", "set"), ("profile", "reset")}
    | {("rules", a) for a in ("list", "add", "edit", "delete")}
    | {("webhooks", a) for a in ("list", "create", "edit", "delete", "deliveries")}
    | {("graph", None), ("graph", "draw")}
    | {("entities", "list"), ("entities", "get")}
    | {("ask-user", None), ("chat", None)}
    | {("exports", a) for a in ("list", "create", "download")}
    | {("routing-settings", a) for a in ("get", "set", "reset")}
)


def is_dashboard_only(verb: tuple[str, str | None]) -> bool:
    return verb in DASHBOARD_ONLY


def refuse_dashboard(verb: tuple[str, str | None], args: argparse.Namespace | None = None) -> int:
    what, path = DASHBOARD_ONLY[verb]
    words = getattr(args, "ignored", None) or []
    if verb == ("spaces", "members remove") and words:
        # The space's own page, where its members are managed.
        path = f"{path}/{_seg(words[0])}"
    print(f"anona: {what} is done in the dashboard: {DASHBOARD}{path}", file=sys.stderr)
    return exits.AUTH


# --- parsers ------------------------------------------------------------------


def _csv(value: str) -> list[str]:
    items = [t.strip() for t in value.split(",")]
    if not all(items):
        raise argparse.ArgumentTypeError("must be a comma-separated list of non-empty values")
    return items


def _events(value: str) -> list[str]:
    return _csv(value)


MEMORY_TYPES = ("fact", "experience", "note")
WEBHOOK_EVENTS_HELP = "Comma-separated events: memory.created, memory.consolidated, security.policy_triggered"


def add_parsers(sub, common: argparse.ArgumentParser, creds: Callable) -> None:
    """Register the commands this module owns on the top-level subparsers."""

    def group(name: str, help_text: str):
        top = sub.add_parser(name, parents=[common], help=help_text)
        return top.add_subparsers(dest="action", required=True)

    def leaf(acts, name: str, help_text: str, space_verb: str | None):
        p = acts.add_parser(name, parents=[common], help=help_text)
        creds(p, space_verb)
        return p

    def yes(p) -> None:
        p.add_argument("--yes", action="store_true", help="Confirm; without it nothing is sent")

    # models ------------------------------------------------------------------
    acts = group("models", "List, create, edit, refresh or delete a space's memory models")
    p = leaf(acts, "list", "List a space's memory models", "list")
    p.add_argument("--limit", type=_limit(1, 200), default=None, help="At most this many (1-200, default 50)")
    p.add_argument("--offset", type=_offset, default=None, help="Skip this many first")
    p.add_argument("--tags", type=_csv, default=None, help="Only models scoped to these comma-separated tags")
    p = leaf(acts, "create", "Create a memory model: a question a space keeps answered", "create in")
    p.add_argument("name", metavar="NAME", help="The model's name")
    p.add_argument("--query", required=True, help="The question the model answers from the space's memories")
    _model_flags(p)
    p.add_argument("--model-id", dest="custom_id", default=None, help="A stable id: lowercase letters, digits and hyphens (default: generated)")
    p = leaf(acts, "get", "Show one memory model and its current content", "read")
    p.add_argument("model_id", metavar="ID", help="The memory model's id")
    p = leaf(acts, "edit", "Change a memory model's name, question or refresh behaviour", "edit in")
    p.add_argument("model_id", metavar="ID", help="The memory model to change")
    p.add_argument("--name", default=None, help="The new name")
    p.add_argument("--query", default=None, help="The new question")
    _model_flags(p)
    p = leaf(acts, "delete", "Delete a memory model (needs --yes)", "delete from")
    p.add_argument("model_id", metavar="ID", help="The memory model to delete")
    yes(p)
    p = leaf(acts, "refresh", "Rewrite a memory model's content now", "refresh in")
    p.add_argument("model_id", metavar="ID", help="The memory model to refresh")
    p = leaf(acts, "clear", "Empty a memory model's content and keep its definition (needs --yes)", "clear in")
    p.add_argument("model_id", metavar="ID", help="The memory model to clear")
    yes(p)
    p = leaf(acts, "history", "Show a memory model's earlier versions", "read")
    p.add_argument("model_id", metavar="ID", help="The memory model to trace")

    # rules -------------------------------------------------------------------
    acts = group("rules", "List, add, edit or delete the rules every answer from a space must follow")
    leaf(acts, "list", "List a space's rules", "list")
    p = leaf(acts, "add", "Add a rule", "add to")
    p.add_argument("text", metavar="TEXT", help="The rule, as an instruction (up to 2000 characters)")
    p.add_argument("--name", default=None, help="A short label (default: the first 60 characters of TEXT)")
    p.add_argument("--priority", type=_limit(0, 1000), default=None, help="Higher comes first in the prompt (0-1000, default 0)")
    p.add_argument("--active", action=argparse.BooleanOptionalAction, default=None, help="Apply the rule (--no-active stores it switched off)")
    p.add_argument("--tags", type=_csv, default=None, help="Comma-separated tags that scope the rule")
    p = leaf(acts, "edit", "Change a rule; only the flags given change", "edit in")
    p.add_argument("rule_id", metavar="ID", help="The rule to change")
    p.add_argument("--name", default=None, help="The new label")
    p.add_argument("--text", type=_text_arg, default=None, help="The new rule text; @FILE reads it")
    p.add_argument("--priority", type=_limit(0, 1000), default=None, help="The new priority (0-1000)")
    p.add_argument("--active", action=argparse.BooleanOptionalAction, default=None, help="Switch the rule on or off")
    p.add_argument("--tags", type=_csv, default=None, help="Replace the tags (comma-separated)")
    p = leaf(acts, "delete", "Delete a rule (needs --yes)", "delete from")
    p.add_argument("rule_id", metavar="ID", help="The rule to delete")
    yes(p)

    # webhooks ----------------------------------------------------------------
    acts = group("webhooks", "List, create, edit or delete a space's webhooks, and read their deliveries")
    leaf(acts, "list", "List a space's webhooks", "list")
    p = leaf(acts, "create", "Register an HTTPS endpoint to be called when something happens", "create in")
    p.add_argument("url", metavar="URL", help="The HTTPS endpoint to POST events to")
    p.add_argument("--events", type=_events, default=None, help=WEBHOOK_EVENTS_HELP + " (default: memory.created)")
    p.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=None, help="Deliver events (--no-enabled creates it paused)")
    p = leaf(acts, "edit", "Change a webhook; only the flags given change", "edit in")
    p.add_argument("webhook_id", metavar="ID", help="The webhook to change")
    p.add_argument("--url", default=None, help="The new endpoint")
    p.add_argument("--events", type=_events, default=None, help=WEBHOOK_EVENTS_HELP)
    p.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=None, help="Resume or pause deliveries")
    p = leaf(acts, "delete", "Delete a webhook (needs --yes)", "delete from")
    p.add_argument("webhook_id", metavar="ID", help="The webhook to delete")
    yes(p)
    p = leaf(acts, "deliveries", "Show a webhook's recent delivery attempts, newest first", "read")
    p.add_argument("webhook_id", metavar="ID", help="The webhook to inspect")
    p.add_argument("--limit", type=_limit(1, 200), default=None, help="At most this many (1-200, default 50)")
    p.add_argument("--cursor", default=None, help="Continue from a previous page's cursor")

    # graph -------------------------------------------------------------------
    gp = sub.add_parser("graph", parents=[common], help="Show a space's entity graph, or draw a chart from its memories")
    creds(gp, "read")
    gp.add_argument("action", nargs="?", choices=("draw",), default=None, help="`draw` asks for a chart instead of the graph")
    gp.add_argument("prompt", nargs="?", default=None, metavar="PROMPT", help="With draw: what to chart, in plain language")
    gp.add_argument("--limit", type=_limit(1, 2000), default=None, help="At most this many entities (1-2000, default 500)")
    gp.add_argument("--min-count", type=_limit(1, 1_000_000), default=None, help="Only entities mentioned at least this many times")
    gp.add_argument("--model", default=None, help="With draw: the LLM that draws it (see `anona llm-models`)")

    # entities ----------------------------------------------------------------
    acts = group("entities", "List the entities extracted from a space, or show one")
    p = leaf(acts, "list", "List entities, most mentioned first", "list")
    p.add_argument("--limit", type=_limit(1, 500), default=None, help="At most this many (1-500, default 100)")
    p.add_argument("--offset", type=_offset, default=None, help="Skip this many first")
    p = leaf(acts, "get", "Show one entity and what has been learned about it", "read")
    p.add_argument("entity_id", metavar="ID", help="The entity's id")

    # receipts ----------------------------------------------------------------
    acts = group("receipts", "Show what a call put in the prompt, and what it left out")
    p = leaf(acts, "get", "Show the context receipt of a request that asked for one", None)
    p.add_argument("request_id", metavar="REQUEST_ID", help="The request's id (the X-Request-ID of a retrieve or proxied call)")
    p.add_argument("--explain", action="store_true", help="Explain why one memory was or was not included")
    p.add_argument("--memory-id", default=None, help="With --explain: the memory to explain")

    # ask-user ----------------------------------------------------------------
    p = sub.add_parser("ask-user", parents=[common], help="Ask a question about one end user, answered from what the space knows of them")
    creds(p, "ask in")
    p.add_argument("user_id", metavar="USER", type=non_empty, help="The user_id your writes are scoped with")
    p.add_argument("question", metavar="QUESTION", help="The question")
    p.add_argument("--model", default=None, help="The LLM that answers (see `anona llm-models`; default: the space's)")
    p.add_argument("--depth", choices=("fast", "thorough"), default=None, help="How deep it reads")

    creds(sub.add_parser("space-types", parents=[common], help="List the space types a new space can start from"), None)

    # chat --------------------------------------------------------------------
    p = sub.add_parser("chat", parents=[common], help="Send one message through the memory-backed chat proxy and print the reply")
    creds(p, "chat in")
    p.add_argument("message", metavar="MESSAGE", help="What to say")
    p.add_argument("--model", default=None, help="The LLM to use (see `anona llm-models`; default: the space's)")
    p.add_argument("--no-memory", action="store_true", help="Neither recall memories into this turn nor record it")

    # account views -----------------------------------------------------------
    p = sub.add_parser("org", parents=[common], help="Show your organization")
    creds(p, None)
    p.add_argument("action", nargs="?", choices=("rename", "delete", "transfer", "leave"), default=None, help="These four are done in the dashboard; nothing is sent")
    _ignored(p)
    acts = group("orgs", "List the organizations you belong to")
    leaf(acts, "list", "List the organizations you belong to", None)

    acts = group("projects", "List your projects")
    leaf(acts, "list", "List your projects", None)

    acts = group("keys", "List API keys, or create or revoke one (in the dashboard)")
    leaf(acts, "list", "List your API keys (never their secrets)", None)
    _dashboard_leaf(acts, common, creds, "create", "Create an API key (done in the dashboard)")
    _dashboard_leaf(acts, common, creds, "revoke", "Revoke an API key (done in the dashboard)")

    acts = group("members", "List your organization's members and invitations")
    leaf(acts, "list", "List the members of your organization", None)
    leaf(acts, "me", "Show who you are signed in as", None)
    leaf(acts, "invitations", "List invitations your organization has sent", None)
    _dashboard_leaf(acts, common, creds, "invite", "Invite a member (done in the dashboard)")
    _dashboard_leaf(acts, common, creds, "remove", "Remove a member (done in the dashboard)")

    acts = group("invitations", "See, accept or decline space and organization invitations sent to you")
    leaf(acts, "pending", "List invitations waiting on you", None)
    p = leaf(acts, "accept", "Accept an invitation", None)
    p.add_argument("token", metavar="TOKEN", help="The invitation's token (see `invitations pending`)")
    p = leaf(acts, "decline", "Decline an invitation", None)
    p.add_argument("token", metavar="TOKEN", help="The invitation's token")

    p = sub.add_parser("billing", parents=[common], help="Show your plan and credits, or your top-up history")
    creds(p, None)
    p.add_argument(
        "action",
        nargs="?",
        choices=("topups", "upgrade", "checkout", "topup", "cancel", "portal"),
        default=None,
        help="`topups` lists purchases; the others are done in the dashboard and nothing is sent",
    )
    _ignored(p)

    acts = group("exports", "Export everything a space holds")
    leaf(acts, "list", "List a space's exports", "list exports of")
    leaf(acts, "create", "Start an export of a space", "export")
    p = leaf(acts, "download", "Download a finished export", "download from")
    p.add_argument("job_id", metavar="JOB_ID", help="The export's id (see `exports list`)")
    p.add_argument("--out", default=None, metavar="FILE", help="Where to save it (default: anona-export-JOB_ID.zip)")
    p.add_argument("--yes", action="store_true", help="Overwrite FILE if it exists")


def _model_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--tags", type=_csv, default=None, help="Comma-separated tags that scope the model to matching memories")
    p.add_argument("--mode", choices=("full", "delta"), default=None, help="How a refresh rewrites it: full regenerates, delta edits in place")
    p.add_argument(
        "--refresh-on-new-memories",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Refresh automatically when new memories are consolidated",
    )
    p.add_argument(
        "--memory-type",
        action="append",
        choices=MEMORY_TYPES,
        default=None,
        help="Build the model only from this memory type; repeatable",
    )
    p.add_argument("--max-tokens", type=_limit(256, 8192), default=None, help="Ceiling on the content length (256-8192)")


def _ignored(p: argparse.ArgumentParser) -> None:
    """The words and the `--yes` a dashboard-only verb accepts and ignores: a
    script that confirms a destructive verb out of habit still learns where it
    is done, rather than getting a usage error."""
    p.add_argument("ignored", nargs="*", metavar="ARG", help="Ignored")
    p.add_argument("--yes", action="store_true", help="Ignored")


def _dashboard_leaf(acts, common, creds, name: str, help_text: str) -> None:
    p = acts.add_parser(name, parents=[common], help=help_text)
    creds(p, None)
    _ignored(p)


def add_profile_actions(p: argparse.ArgumentParser) -> None:
    """`profile [set|reset]` and its flags, on the existing `profile` parser."""
    p.add_argument("action", nargs="?", choices=("set", "reset"), default=None, help="`set` changes the profile; `reset` returns it to the defaults (needs --yes)")
    p.add_argument("--mission", type=_text_arg, default=None, help="With set: what the space is for; @FILE reads it")
    for dial, low in (("skepticism", "takes statements at face value"), ("literalism", "reads intent loosely"), ("empathy", "ignores emotional context")):
        p.add_argument(f"--{dial}", type=_limit(1, 5), default=None, help=f"With set: 1 = {low}, 5 = the opposite")
    p.add_argument("--yes", action="store_true", help="With reset: confirm; without it nothing is sent")


def add_usage_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--history", action="store_true", help="Daily operation counts instead of the credit balance (needs `anona login`)")
    p.add_argument("--breakdown", action="store_true", help="Operation counts per space instead of the credit balance (needs `anona login`)")
    p.add_argument("--days", type=_limit(1, 365), default=None, help="With --history or --breakdown: how many days back (1-365, default 30)")


def add_space_members(acts, common: argparse.ArgumentParser, creds: Callable) -> None:
    """`spaces members list|remove`, under the existing `spaces` command."""
    mp = acts.add_parser("members", parents=[common], help="List the people a space is shared with")
    subs = mp.add_subparsers(dest="sub_action", required=True)
    p = subs.add_parser("list", parents=[common], help="List a space's members")
    p.add_argument("space_id", metavar="SPACE", help="The space (owner:name for one shared with you)")
    creds(p, None)
    _dashboard_leaf(subs, common, creds, "remove", "Remove a member from a space (done in the dashboard)")


# --- usage checks, before any credential is looked up -------------------------


def _no(message: str) -> int:
    print(f"anona: {message} Nothing was sent.", file=sys.stderr)
    return EMPTY_INPUT_EXIT


def _blank(what: str, value: str) -> int | None:
    return None if value.strip() else _no(f"the {what} is empty.")


def default_out(job_id: str) -> str:
    return f"anona-export-{job_id}.zip"


def precheck(verb: tuple[str, str | None], args: argparse.Namespace) -> int | None:
    cmd, act = verb
    if verb == ("models", "create"):
        return _blank("model name", args.name) or _blank("question", args.query)
    if verb == ("models", "edit"):
        if _model_body(args, create=False) == {}:
            return _no("name at least one field to change (see `anona models edit --help`).")
    if verb == ("rules", "add"):
        return _blank("rule text", args.text)
    if verb == ("rules", "edit"):
        if not _rule_edit_body(args):
            return _no("name at least one field to change (see `anona rules edit --help`).")
    if verb == ("webhooks", "edit"):
        if not _given(url=args.url, event_types=args.events, enabled=args.enabled):
            return _no("name at least one field to change (see `anona webhooks edit --help`).")
    if cmd == "profile":
        flags = [f for f, v in (("--mission", args.mission), ("--skepticism", args.skepticism), ("--literalism", args.literalism), ("--empathy", args.empathy)) if v is not None]
        if act != "set" and flags:
            return _no(f"{', '.join(flags)} needs `profile set`.")
        if act == "set" and not flags:
            return _no("name at least one field to change (see `anona profile --help`).")
    if cmd == "graph":
        if act == "draw":
            if args.prompt is None:
                return _no("graph draw needs a PROMPT.")
            return _blank("prompt", args.prompt)
        if args.prompt is not None or args.model is not None:
            return _no("a PROMPT or --model needs `graph draw`.")
    if cmd == "usage":
        if args.history and args.breakdown:
            return _no("give --history or --breakdown, not both.")
        if args.days is not None and not (args.history or args.breakdown):
            return _no("--days needs --history or --breakdown.")
    if verb == ("receipts", "get"):
        if args.memory_id is not None and not args.explain:
            return _no("--memory-id needs --explain.")
        if args.explain and args.memory_id is None:
            return _no("--explain needs --memory-id.")
    if verb == ("ask-user", None):
        return _blank("question", args.question)
    if verb == ("chat", None):
        return _blank("message", args.message)
    if verb == ("exports", "download"):
        out = args.out or default_out(args.job_id)
        if os.path.lexists(out) and not args.yes:
            print(f"anona: {_p(out)} already exists. Pass --yes to overwrite it. Nothing was sent.", file=sys.stderr)
            return EMPTY_INPUT_EXIT
    return None


def destroy_message(verb: tuple[str, str | None], args: argparse.Namespace, space: str | None) -> str | None:
    where = f"in space '{_p(space or 'default')}'"
    if verb == ("models", "delete"):
        return f"delete memory model {_p(args.model_id)} {where}"
    if verb == ("models", "clear"):
        return f"clear the content of memory model {_p(args.model_id)} {where}"
    if verb == ("profile", "reset"):
        return f"reset the profile of space '{_p(space or 'default')}' to the platform defaults"
    if verb == ("rules", "delete"):
        return f"delete rule {_p(args.rule_id)} {where}"
    if verb == ("webhooks", "delete"):
        return f"delete webhook {_p(args.webhook_id)} {where}"
    return None


# --- requests -----------------------------------------------------------------


def _trigger(args: argparse.Namespace) -> dict | None:
    trigger = _given(mode=args.mode, refresh_on_new_memories=args.refresh_on_new_memories, memory_type=args.memory_type)
    return trigger or None


def _model_body(args: argparse.Namespace, *, create: bool) -> dict:
    return _given(
        name=args.name,
        query=args.query,
        model_id=getattr(args, "custom_id", None) if create else None,
        tags=args.tags,
        max_tokens=args.max_tokens,
        trigger=_trigger(args),
    )


def _rule_edit_body(args: argparse.Namespace) -> dict:
    return _given(name=args.name, content=args.text, priority=args.priority, is_active=args.active, tags=args.tags)


def _rule_name(args: argparse.Namespace) -> str:
    return args.name if args.name is not None else " ".join(args.text.split())[:60]


def _sp(space: str | None) -> str:
    if space is None:
        raise ValueError("this command needs a space")
    return f"/v1/spaces/{_seg(space)}"


# verb -> (args, space) -> (method, path, params, body)
_Req = tuple[str, str, "dict | None", "dict | None"]
REQUESTS: dict[tuple[str, str | None], Callable[[argparse.Namespace, str | None], _Req]] = {
    ("models", "list"): lambda a, s: ("GET", f"{_sp(s)}/models", _given(limit=a.limit, offset=a.offset, tags=a.tags), None),
    ("models", "create"): lambda a, s: ("POST", f"{_sp(s)}/models", None, _model_body(a, create=True)),
    ("models", "get"): lambda a, s: ("GET", f"{_sp(s)}/models/{_seg(a.model_id)}", None, None),
    ("models", "edit"): lambda a, s: ("PATCH", f"{_sp(s)}/models/{_seg(a.model_id)}", None, _model_body(a, create=False)),
    ("models", "delete"): lambda a, s: ("DELETE", f"{_sp(s)}/models/{_seg(a.model_id)}", None, None),
    ("models", "refresh"): lambda a, s: ("POST", f"{_sp(s)}/models/{_seg(a.model_id)}/refresh", None, None),
    ("models", "clear"): lambda a, s: ("POST", f"{_sp(s)}/models/{_seg(a.model_id)}/clear", None, None),
    ("models", "history"): lambda a, s: ("GET", f"{_sp(s)}/models/{_seg(a.model_id)}/history", None, None),
    ("profile", "reset"): lambda a, s: ("DELETE", f"{_sp(s)}/profile", None, None),
    ("rules", "list"): lambda a, s: ("GET", f"{_sp(s)}/rules", None, None),
    ("rules", "add"): lambda a, s: (
        "POST",
        f"{_sp(s)}/rules",
        None,
        {"name": _rule_name(a), "content": a.text, **_given(priority=a.priority, is_active=a.active, tags=a.tags)},
    ),
    ("rules", "edit"): lambda a, s: ("PATCH", f"{_sp(s)}/rules/{_seg(a.rule_id)}", None, _rule_edit_body(a)),
    ("rules", "delete"): lambda a, s: ("DELETE", f"{_sp(s)}/rules/{_seg(a.rule_id)}", None, None),
    ("webhooks", "list"): lambda a, s: ("GET", f"{_sp(s)}/webhooks", None, None),
    ("webhooks", "create"): lambda a, s: (
        "POST",
        f"{_sp(s)}/webhooks",
        None,
        {"url": a.url, **_given(event_types=a.events, enabled=a.enabled)},
    ),
    ("webhooks", "edit"): lambda a, s: (
        "PATCH",
        f"{_sp(s)}/webhooks/{_seg(a.webhook_id)}",
        None,
        _given(url=a.url, event_types=a.events, enabled=a.enabled),
    ),
    ("webhooks", "delete"): lambda a, s: ("DELETE", f"{_sp(s)}/webhooks/{_seg(a.webhook_id)}", None, None),
    ("webhooks", "deliveries"): lambda a, s: (
        "GET",
        f"{_sp(s)}/webhooks/{_seg(a.webhook_id)}/deliveries",
        _given(limit=a.limit, cursor=a.cursor),
        None,
    ),
    ("graph", None): lambda a, s: ("GET", f"{_sp(s)}/graph", _given(limit=a.limit, min_count=a.min_count), None),
    ("graph", "draw"): lambda a, s: ("POST", f"{_sp(s)}/graph", None, {"request": a.prompt, **_given(model=a.model)}),
    ("entities", "list"): lambda a, s: ("GET", f"{_sp(s)}/entities", _given(limit=a.limit, offset=a.offset), None),
    ("entities", "get"): lambda a, s: ("GET", f"{_sp(s)}/entities/{_seg(a.entity_id)}", None, None),
    ("ask-user", None): lambda a, s: ("POST", f"{_sp(s)}/users/{_seg(a.user_id)}/ask", None, {"query": a.question, **_given(model=a.model, depth=a.depth)}),
    ("space-types", None): lambda a, s: ("GET", "/v1/space-types", None, None),
    ("chat", None): lambda a, s: (
        "POST",
        "/v1/chat/completions",
        None,
        {
            "messages": [{"role": "user", "content": a.message}],
            "space_id": s,
            **_given(model=a.model),
            # `memory` gates only recall; the turn is recorded unless
            # `auto_record` is false too.
            **({"memory": False, "auto_record": False} if a.no_memory else {}),
        },
    ),
    ("org", None): lambda a, s: ("GET", "/v1/orgs/me", None, None),
    ("orgs", "list"): lambda a, s: ("GET", "/v1/orgs", None, None),
    ("projects", "list"): lambda a, s: ("GET", "/v1/projects", None, None),
    ("keys", "list"): lambda a, s: ("GET", "/v1/api-keys", None, None),
    ("members", "list"): lambda a, s: ("GET", "/v1/members", None, None),
    ("members", "me"): lambda a, s: ("GET", "/v1/members/me", None, None),
    ("members", "invitations"): lambda a, s: ("GET", "/v1/members/invitations", None, None),
    ("invitations", "pending"): lambda a, s: ("GET", "/v1/invitations/pending", None, None),
    ("invitations", "accept"): lambda a, s: ("POST", "/v1/invitations/accept", None, {"token": a.token}),
    ("invitations", "decline"): lambda a, s: ("POST", "/v1/invitations/decline", None, {"token": a.token}),
    ("billing", None): lambda a, s: ("GET", "/v1/billing", None, None),
    ("billing", "topups"): lambda a, s: ("GET", "/v1/billing/topups", None, None),
    ("usage", "history"): lambda a, s: ("GET", "/v1/usage", _given(days=a.days), None),
    ("usage", "breakdown"): lambda a, s: ("GET", "/v1/usage/breakdown", _given(days=a.days), None),
    ("exports", "list"): lambda a, s: ("GET", f"{_sp(s)}/exports", None, None),
    ("exports", "create"): lambda a, s: ("POST", f"{_sp(s)}/exports", None, None),
    ("receipts", "get"): lambda a, s: (
        "GET",
        f"/v1/receipts/{_seg(a.request_id)}" + ("/explain" if a.explain else ""),
        _given(memory_id=a.memory_id) if a.explain else None,
        None,
    ),
    ("spaces", "members list"): lambda a, s: ("GET", f"/v1/spaces/{_seg(a.space_id)}/members", None, None),
}

# Routes that answer 204; the CLI writes the document `--json` prints.
NO_CONTENT: dict[tuple[str, str | None], Callable[[argparse.Namespace, str | None], dict]] = {
    ("models", "delete"): lambda a, s: {"deleted": True, "model_id": a.model_id, "space_id": s},
    ("profile", "reset"): lambda a, s: {"reset": True, "space_id": s, "settings": "profile"},
    ("rules", "delete"): lambda a, s: {"deleted": True, "rule_id": a.rule_id, "space_id": s},
    ("webhooks", "delete"): lambda a, s: {"deleted": True, "webhook_id": a.webhook_id, "space_id": s},
    ("invitations", "decline"): lambda a, s: {"declined": True},
}

SPECIAL = frozenset({("profile", "set"), ("exports", "download")})


def handles(verb: tuple[str, str | None]) -> bool:
    return verb in REQUESTS or verb in SPECIAL


def call(client: AnonaClient, verb: tuple[str, str | None], args: argparse.Namespace, space: str | None) -> Any:
    if verb == ("profile", "set"):
        return _profile_set(client, args, space)
    if verb == ("exports", "download"):
        return _download(client, args, space)
    if verb == ("models", "edit") and _trigger(args):
        return _models_edit(client, args, space)
    method, path, params, body = REQUESTS[verb](args, space)
    out = client._call(method, path, params=params, json=body, expect_no_content=verb in NO_CONTENT)
    if verb in NO_CONTENT:
        return NO_CONTENT[verb](args, space)
    return out


def _models_edit(client: AnonaClient, args: argparse.Namespace, space: str | None) -> Any:
    """A trigger write replaces the whole trigger, so read the model, lay the
    named trigger flags over its current trigger, and send all of it:
    `--no-refresh-on-new-memories` must not turn a delta model into a full one
    or drop its memory-type filter."""
    path = f"{_sp(space)}/models/{_seg(args.model_id)}"
    raw = _d(client._call("GET", path)).get("trigger")
    current = raw if isinstance(raw, dict) else {}
    merged = {k: current[k] for k in ("mode", "refresh_on_new_memories", "memory_type") if current.get(k) is not None}
    merged.update(_trigger(args) or {})
    body = _model_body(args, create=False)
    body["trigger"] = merged
    return client._call("PATCH", path, json=body)


def _profile_set(client: AnonaClient, args: argparse.Namespace, space: str | None) -> Any:
    """The PUT replaces the whole profile, so read it, change the named
    fields, and write every field back: `--empathy 4` must not clear the
    mission or the other two dials."""
    path = f"{_sp(space)}/profile"
    current = _d(client._call("GET", path))
    raw = current.get("disposition")
    disposition = raw if isinstance(raw, dict) else {}
    dials = {k: disposition.get(k) for k in ("skepticism", "literalism", "empathy")}
    dials.update(_given(skepticism=args.skepticism, literalism=args.literalism, empathy=args.empathy))
    mission = args.mission if args.mission is not None else (current.get("mission") or None)
    return client._call("PUT", path, json={"mission": mission, "disposition": dials})


def _save(out: str, chunks) -> int:
    """Write to a sibling file and move it into place, so a download that
    breaks half way never leaves a truncated bundle under the real name.

    The sibling is created exclusively under a random name (mkstemp opens with
    O_EXCL and never follows a symlink), so a file or link someone planted in
    the target's directory cannot be written through."""
    total = 0
    part = None
    try:
        fd, part = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(out)), prefix=".anona-export-", suffix=".part")
        with os.fdopen(fd, "wb") as fh:
            for chunk in chunks:
                fh.write(chunk)
                total += len(chunk)
        os.replace(part, out)
        part = None
    except OSError as exc:
        raise AnonaError(400, f"could not write {_printable(out)} ({type(exc).__name__}).", code="write_failed") from None
    finally:
        if part is not None:
            try:
                os.unlink(part)
            except OSError:
                pass
    return total


def _fetch(client: AnonaClient, url: str, out: str) -> int:
    """Fetch a download link. A fresh client, never the API one: the link is
    a presigned storage URL, and the API credential must not be sent to it."""
    if urlsplit(url).scheme not in ("http", "https"):
        raise AnonaError(502, "the download link is not an http(s) URL.", code="download_failed")
    try:
        with httpx.Client(timeout=client._timeout, follow_redirects=True) as hc:
            with hc.stream("GET", url) as resp:
                if not resp.is_success:
                    # The storage host answered, not the API: its status says
                    # nothing about the credential, so it is not mapped as if
                    # it did (a 403 here is an expired link, not exit 4).
                    raise AnonaError(resp.status_code, LINK_REFUSED, code=LINK_REFUSED, transport=True)
                return _save(out, resp.iter_bytes())
    except httpx.HTTPError as exc:
        raise AnonaError(503, f"{type(exc).__name__}", code="download_failed", transport=True) from None


def _download(client: AnonaClient, args: argparse.Namespace, space: str | None) -> Any:
    out = args.out or default_out(args.job_id)
    path = f"{_sp(space)}/exports/{_seg(args.job_id)}/download"
    resp = client._get_client().get(f"{client._base_url}{path}", follow_redirects=False)
    if 300 <= resp.status_code < 400 and resp.headers.get("location"):
        size = _fetch(client, urljoin(f"{client._base_url}{path}", resp.headers["location"]), out)
    else:
        client._raise(resp)
        if "json" in resp.headers.get("content-type", "").lower():
            doc = resp.json()
            url = doc.get("download_url") if isinstance(doc, dict) else None
            if not isinstance(url, str) or not url:
                raise AnonaError(502, "the server sent no download link.", code="download_failed")
            size = _fetch(client, urljoin(f"{client._base_url}{path}", url), out)
        else:
            size = _save(out, [resp.content])
    return {"job_id": args.job_id, "path": out, "bytes": size}


# --- plain-text output --------------------------------------------------------


def _items(doc: object, key: str) -> list[dict]:
    """Rows from a wrapped document or a bare list."""
    rows = doc if isinstance(doc, list) else _d(doc).get(key)
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _join(values: object) -> str:
    return ",".join(_p(v) for v in values) if isinstance(values, list) and values else "-"


def _r_models_list(doc, args, space) -> None:
    rows = _items(doc, "items")
    if not rows:
        print(f"No memory models in space '{_p(space)}'.")
    for m in rows:
        print(f"{_p(m.get('id'))}  {_p(m.get('name'))}  {_p(m.get('query'))}")


def _r_models_create(doc, args, space) -> None:
    d = _d(doc)
    print(f"Creating memory model {_p(d.get('model_id') or args.custom_id)} (job {_p(d.get('job_id'))}).")


def _r_models_refresh(doc, args, space) -> None:
    d = _d(doc)
    print(f"Refreshing {_p(d.get('model_id') or args.model_id)} (job {_p(d.get('job_id'))}).")


def _r_models_history(doc, args, space) -> None:
    rows = _items(doc, "entries")
    if not rows:
        print(f"No earlier versions of memory model {_p(args.model_id)}.")
    for h in rows:
        content = _printable(h.get("previous_content") or "")[:80]
        print(f"{_p(h.get('changed_at'))}  {content}")


def _r_rules_list(doc, args, space) -> None:
    rows = _items(doc, "items")
    if not rows:
        print(f"No rules in space '{_p(space)}'.")
    for r in rows:
        print(f"{_p(r.get('id'))}  {_p(r.get('content'))}")


def _r_webhooks_list(doc, args, space) -> None:
    rows = _items(doc, "items")
    if not rows:
        print(f"No webhooks in space '{_p(space)}'.")
    for w in rows:
        print(f"{_p(w.get('id'))}  {_p(w.get('url'))}  {_join(w.get('event_types'))}")


def _r_webhook_created(doc, args, space) -> None:
    d = _d(doc)
    print(f"Created webhook {_p(d.get('id'))}.")
    if d.get("secret"):
        print(f"Signing secret: {_p(d['secret'])}")
        print("Save this secret now; it is not shown again.")


def _r_deliveries(doc, args, space) -> None:
    d = _d(doc)
    rows = _items(d, "items")
    if not rows:
        print(f"No deliveries for webhook {_p(args.webhook_id)}.")
    for r in rows:
        when = r.get("delivered_at") or r.get("last_attempt_at") or r.get("created_at")
        print(f"{_p(when)}  {_p(r.get('status'))}  {_p(r.get('event_type'))}")
    if d.get("next_cursor"):
        print(f"Next cursor: {_p(d['next_cursor'])}")


def _r_graph(doc, args, space) -> None:
    d = _d(doc)
    print(f"{_p(d.get('total_entities'))} entities, {_p(d.get('total_edges'))} links")
    for n in _items(d, "nodes")[:50]:
        print(f"{_p(n.get('label'))} ({_p(n.get('mention_count'))})")


def _r_chart(doc, args, space) -> None:
    d = _d(doc)
    if d.get("text"):
        print(_printable_block(d["text"]))
    source = d.get("mermaid") or d.get("diagram")
    if source:
        if d.get("text"):
            print()
        print(_printable_block(source))
    if not d.get("text") and not source:
        print("No chart was drawn.")


def _r_entities(doc, args, space) -> None:
    rows = _items(doc, "items")
    if not rows:
        print(f"No entities in space '{_p(space)}'.")
    for e in rows:
        print(f"{_p(e.get('id'))}  {_p(e.get('name'))}  {_p(e.get('mention_count'))}")


def _r_entity(doc, args, space) -> None:
    d = _d(doc)
    print(f"{_p(d.get('name'))} ({_p(d.get('id') or args.entity_id)})")
    print(f"Mentions: {_p(d.get('mention_count'))}")
    rows = _items(d, "observations")
    if not rows:
        print("No observations yet.")
    for o in rows:
        print(f"{_p(o.get('mentioned_at'))}  {_p(o.get('text'))}")


def _r_receipt(doc, args, space) -> None:
    d = _d(doc)
    if args.explain:
        print(f"Memory {_p(d.get('memory_id') or args.memory_id)}: {_p(d.get('outcome'))}")
        if d.get("stage"):
            print(f"Stage: {_p(d['stage'])}")
        if d.get("detail"):
            print(f"Detail: {_p(d['detail'])}")
        return
    print(f"Receipt {_p(d.get('request_id') or args.request_id)} for space '{_p(d.get('space_id'))}'")
    print(f"Included: {len(_items(d, 'included'))}, excluded: {len(_items(d, 'excluded'))}")
    acct = d.get("token_accounting")
    if isinstance(acct, dict):
        shown = [f"{_printable(k)}={_p(v)}" for k, v in acct.items() if not isinstance(v, (dict, list))]
        if shown:
            print("Tokens: " + ", ".join(shown))


def _r_ask(doc, args, space) -> None:
    text = _d(doc).get("insights")
    print(_printable_block(text) if isinstance(text, str) and text.strip() else "No answer.")


def _r_space_types(doc, args, space) -> None:
    for t in _items(doc, "items"):
        print(f"{_p(t.get('space_type'))}  {_p(t.get('name'))}")


def _r_chat(doc, args, space) -> None:
    choices = _d(doc).get("choices")
    first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    content = _d(first.get("message")).get("content")
    print(_printable_block(content) if isinstance(content, str) else "No reply.")


def _r_org(doc, args, space) -> None:
    d = _d(doc)
    print(f"{_p(d.get('org_id'))}  {_p(d.get('name'))}  {_p(d.get('slug'))}")


def _r_orgs(doc, args, space) -> None:
    for o in _items(doc, "orgs"):
        print(f"{_p(o.get('org_id') or o.get('id'))}  {_p(o.get('name'))}  {_p(o.get('role'))}")


def _r_projects(doc, args, space) -> None:
    for p in _items(doc, "projects"):
        print(f"{_p(p.get('project_id') or p.get('id'))}  {_p(p.get('name'))}")


def _r_keys(doc, args, space) -> None:
    rows = _items(doc, "keys")
    if not rows:
        print("No API keys.")
    for k in rows:
        # The public prefix, not redacted like an id: redaction would turn it
        # into "anona_live_…" and the ellipsis below would double. Capped at the
        # column's width, so nothing longer than a prefix can be printed here.
        prefix = _printable(k.get("key_prefix") or "-")[:20]
        print(f"{prefix}…  {_p(k.get('name'))}  {_p(k.get('created_at'))}  {_p(k.get('last_used_at'))}")


def _member_row(m: dict) -> str:
    return f"{_p(m.get('member_id') or m.get('id'))}  {_p(m.get('email'))}  {_p(m.get('role'))}"


def _r_members(doc, args, space) -> None:
    for m in _items(doc, "members"):
        print(_member_row(m))


def _r_member(doc, args, space) -> None:
    print(_member_row(_d(doc)))


def _r_member_invitations(doc, args, space) -> None:
    rows = _items(doc, "invitations")
    if not rows:
        print("No pending invitations.")
    for i in rows:
        print(f"{_p(i.get('invitation_id'))}  {_p(i.get('email'))}  {_p(i.get('role'))}")


def _r_pending(doc, args, space) -> None:
    rows = _items(doc, "invitations")
    if not rows:
        print("No invitations are waiting on you.")
    for i in rows:
        target = i.get("space_id") or i.get("org_name")
        print(f"{_p(i.get('token'))}  {_p(i.get('kind'))}  {_p(target)}  {_p(i.get('role'))}")


def _r_accept(doc, args, space) -> None:
    d = _d(doc)
    print(f"Joined {_p(d.get('org_name') or d.get('space_id') or d.get('org_id'))}.")


def _r_decline(doc, args, space) -> None:
    print("Declined.")


def _r_billing(doc, args, space) -> None:
    d = _d(doc)
    print(f"Plan: {_p(d.get('plan_name') or d.get('plan_id'))}")
    line = f"Credits: {_p(d.get('credits_remaining'))} remaining of {_p(d.get('credits_limit'))} ({_p(d.get('credits_used'))} used"
    if d.get("credits_banked"):
        line += f", {_p(d['credits_banked'])} banked"
    print(line + ").")
    print(f"Period ends: {_p(d.get('credits_reset_at'))}")


def _r_topups(doc, args, space) -> None:
    rows = _items(doc, "items")
    if not rows:
        print("No top-ups.")
    for t in rows:
        print(f"{_p(t.get('id'))}  {_p(t.get('purchased_at'))}  {_p(t.get('pack'))}  {_p(t.get('credits'))} credits  {_p(t.get('status'))}")


def _r_history(doc, args, space) -> None:
    d = _d(doc)
    rows = _items(d, "series")
    if not rows:
        print(f"No usage in the last {_p(d.get('period_days'))} days.")
    for r in rows:
        print(f"{_p(r.get('date'))}  {_p(r.get('ops'))} ops")


def _r_breakdown(doc, args, space) -> None:
    d = _d(doc)
    rows = _items(d, "breakdown")
    if not rows:
        print(f"No usage in the last {_p(d.get('period_days'))} days.")
    for r in rows:
        print(f"{_p(r.get('space_id'))}  {_p(r.get('memories_added'))} added  {_p(r.get('searches'))} searches  {_p(r.get('insights'))} insights")


def _r_exports(doc, args, space) -> None:
    rows = _items(doc, "items")
    if not rows:
        print(f"No exports of space '{_p(space)}'.")
    for j in rows:
        print(f"{_p(j.get('job_id'))}  {_p(j.get('status'))}  {_p(j.get('created_at'))}")


def _r_export_started(doc, args, space) -> None:
    print(f"Export {_p(_d(doc).get('job_id'))} started.")


def _r_downloaded(doc, args, space) -> None:
    d = _d(doc)
    print(f"Saved {_count(d.get('bytes'))} bytes to {_p(d.get('path'))}.")


def _r_space_members(doc, args, space) -> None:
    rows = _items(doc, "members")
    if not rows:
        print(f"No members in space '{_p(args.space_id)}'.")
    for m in rows:
        print(_member_row(m))


def _say(template: str, attr: str) -> Callable:
    def render(doc, args, space) -> None:
        print(template.format(_p(getattr(args, attr))))

    return render


RENDER: dict[tuple[str, str | None], Callable[[Any, argparse.Namespace, str | None], None]] = {
    ("models", "list"): _r_models_list,
    ("models", "create"): _r_models_create,
    ("models", "get"): _render_model,
    ("models", "edit"): _say("Updated memory model {}.", "model_id"),
    ("models", "delete"): _say("Deleted memory model {}.", "model_id"),
    ("models", "refresh"): _r_models_refresh,
    ("models", "clear"): _say("Cleared {}; its definition is kept.", "model_id"),
    ("models", "history"): _r_models_history,
    ("profile", "set"): _render_profile,
    ("profile", "reset"): lambda doc, args, space: print(f"Reset the profile of space '{_p(space)}'."),
    ("rules", "list"): _r_rules_list,
    ("rules", "add"): lambda doc, args, space: print(f"Added rule {_p(_d(doc).get('id'))}."),
    ("rules", "edit"): _say("Updated rule {}.", "rule_id"),
    ("rules", "delete"): _say("Deleted rule {}.", "rule_id"),
    ("webhooks", "list"): _r_webhooks_list,
    ("webhooks", "create"): _r_webhook_created,
    ("webhooks", "edit"): _say("Updated webhook {}.", "webhook_id"),
    ("webhooks", "delete"): _say("Deleted webhook {}.", "webhook_id"),
    ("webhooks", "deliveries"): _r_deliveries,
    ("graph", None): _r_graph,
    ("graph", "draw"): _r_chart,
    ("entities", "list"): _r_entities,
    ("entities", "get"): _r_entity,
    ("receipts", "get"): _r_receipt,
    ("ask-user", None): _r_ask,
    ("space-types", None): _r_space_types,
    ("chat", None): _r_chat,
    ("org", None): _r_org,
    ("orgs", "list"): _r_orgs,
    ("projects", "list"): _r_projects,
    ("keys", "list"): _r_keys,
    ("members", "list"): _r_members,
    ("members", "me"): _r_member,
    ("members", "invitations"): _r_member_invitations,
    ("invitations", "pending"): _r_pending,
    ("invitations", "accept"): _r_accept,
    ("invitations", "decline"): _r_decline,
    ("billing", None): _r_billing,
    ("billing", "topups"): _r_topups,
    ("usage", "history"): _r_history,
    ("usage", "breakdown"): _r_breakdown,
    ("exports", "list"): _r_exports,
    ("exports", "create"): _r_export_started,
    ("exports", "download"): _r_downloaded,
    ("spaces", "members list"): _r_space_members,
}
