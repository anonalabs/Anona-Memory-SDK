"""The `anona` command: start, login, logout, status, mcp, record, retrieve,
reason, doctor, guide, completion, and the resource verbs in verbs.py (spaces,
memories, documents, jobs, profile, model, user-profile, usage, llm-models and
the space settings)."""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import sys
import time
from datetime import datetime

import httpx

from . import config_env, credential, exits, login, oauth, proxy, store, target, temp, tokens, verbs


def _limit(value: str) -> int:
    # The API's own bound, checked here so a bad value is a usage error (exit 2)
    # rather than a 422 that reads like a server failure.
    n = int(value)
    if not 1 <= n <= 100:
        raise argparse.ArgumentTypeError("must be between 1 and 100")
    return n


def _min_score(value: str) -> float:
    x = float(value)
    if not (x >= 0 and math.isfinite(x)):
        raise argparse.ArgumentTypeError("must be a number, 0 or more")
    return x


def _profile_name(value: str) -> str:
    try:
        return store.check_profile(value)
    except store.BadProfile as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _timeout(value: str) -> float:
    try:
        x = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number of seconds") from None
    if not (x > 0 and math.isfinite(x)):
        raise argparse.ArgumentTypeError("must be more than 0")
    return x


def _retries(value: str) -> int:
    n = int(value)
    if not 0 <= n <= 10:
        raise argparse.ArgumentTypeError("must be between 0 and 10")
    return n


def _positive(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def _iso8601(value: str) -> str:
    # Checked here so a typo is a usage error (exit 2) that sends nothing,
    # rather than a metered round trip that comes back 422. The string is sent
    # exactly as typed: parsing is only the check.
    text = value.strip()
    try:
        datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "must be an ISO 8601 date or date-time, e.g. 2026-06-01 or 2026-06-01T09:00:00Z"
        ) from None
    return text


def _tags(value: str) -> list[str]:
    # Comma-separated, and the flag may be repeated: the lists add up. An empty
    # entry (`a,,b`, a trailing comma) is a typo, never a tag.
    tags = [t.strip() for t in value.split(",")]
    if not all(tags):
        raise argparse.ArgumentTypeError("must be a comma-separated list of non-empty tags")
    return tags


def _metadata(value: str) -> dict:
    try:
        parsed = json.loads(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a JSON object, e.g. '{\"source\": \"cli\"}'") from None
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("must be a JSON object, not a list, string or number")
    return parsed


_JSON_HELP = "Print the API response as one JSON document on stdout"
_TAGS_MATCH = ("any", "all", "any_strict", "all_strict", "exact")


def _scope_flags(parser: argparse.ArgumentParser, lead: str) -> None:
    for scope in ("user", "agent", "session"):
        parser.add_argument(f"--{scope}-id", type=verbs.non_empty, default=None, help=f"{lead} this {scope}_id")


def _flat(lists: list[list[str]] | None) -> list[str] | None:
    """`--tags a,b --tags c` -> ["a", "b", "c"]; unset stays None (omitted)."""
    return None if lists is None else [t for group in lists for t in group]


# An account -- reached with an API key or the command-line login -- can hold
# many spaces and the server implies none, so both need one named. A temporary
# profile has exactly one space. The MCP login is refused by these verbs
# outright (memory._token_and_space).
_SPACE_HELP = (
    "Space to {verb}. Required with an API key or `anona login` unless "
    "ANONA_SPACE_ID is set; a temporary profile uses `default`"
)
_API_KEY_HELP = (
    "API key for this command (else $ANONA_API_KEY, else ~/.anona/config.env). "
    "Visible to other local users in the process list; prefer the variable"
)


class _Parser(argparse.ArgumentParser):
    """argparse echoes an unrecognised argument verbatim, which for
    `anona start --api-key <key>` is the key. Errors are redacted first."""

    def error(self, message):  # type: ignore[override]
        super().error(credential.redact(message))

    def _check_value(self, action, value):  # type: ignore[override]
        # Python 3.12.8 started quoting the choices in this message and earlier
        # releases do not, so the same command printed two different errors
        # depending on the interpreter. Pin the quoted form, which the npm CLI
        # prints too.
        if action.choices is not None and value not in action.choices:
            choices = ", ".join(repr(c) for c in action.choices)
            message = f"invalid choice: {value!r} (choose from {choices})"
            if action.dest == "command":
                # The usual cause is a copy older than the docs that named the
                # command, and the list alone never says so.
                from anona import __version__

                message += (
                    f". If the docs name `{value}`, this copy (anona {__version__}) is "
                    "older than they are: pip install -U anona"
                )
            raise argparse.ArgumentError(action, message)


# Claiming has no command of its own -- it is what `login` does when a
# temporary profile is stored -- so `login --help` is where it must be said.
_LOGIN_DESCRIPTION = (
    "Approve access in a browser: the command line, then MCP. "
    "With a temporary profile stored (from `anona start`), approving also claims it: the account you sign in with, which needs an email not yet used for Anona, takes over the profile and everything in it."
)


def _parser() -> argparse.ArgumentParser:
    # Both flags are accepted before OR after the command, like the Node CLI.
    # SUPPRESS matters: with a real default on the subparser's copy, argparse
    # overwrites a value given before the command with that default.
    common = _Parser(add_help=False)
    # No default here, so `mcp` can tell "the user said so" from "nobody said
    # anything" to prefer the stored deployment.
    common.add_argument(
        "--base-url",
        default=argparse.SUPPRESS,
        help=(
            "Anona server. Default: $ANONA_BASE_URL; otherwise "
            f"{target.DEFAULT_API_BASE_URL} for a command using an API key, and "
            f"{store.DEFAULT_BASE_URL} for login and start. A stored login or "
            "temporary profile always uses the server that issued it"
        ),
    )
    # Global, as in Node. Only `login` has a browser to skip; elsewhere it is ignored.
    common.add_argument(
        "--no-browser",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Used by `anona login` only: print the approval URL instead of opening a browser",
    )
    common.add_argument(
        "--profile",
        type=_profile_name,
        default=argparse.SUPPRESS,
        help="Use a named profile: its own credentials and config.env under "
        "~/.anona/profiles/NAME (default: $ANONA_PROFILE, else ~/.anona)",
    )
    common.add_argument(
        "--timeout",
        type=_timeout,
        default=argparse.SUPPRESS,
        metavar="SECONDS",
        help="Give up on one HTTP request after this long (default 120)",
    )
    common.add_argument(
        "--retries",
        type=_retries,
        default=argparse.SUPPRESS,
        metavar="N",
        help="Retry a 429, or a 5xx on a request that is safe to repeat, up to N times "
        "(default 2, honouring Retry-After). A write is never re-sent after a 5xx or a timeout",
    )
    common.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        default=argparse.SUPPRESS,
        help="No progress or retry notices on stderr; results and errors still print",
    )
    p = _Parser(
        prog="anona",
        description=(
            "Anona Memory: sign in, connect MCP clients, record, retrieve or reason over "
            "memories, and manage spaces, memories and jobs."
        ),
        parents=[common],
        epilog=_guide_epilog(),
    )
    # The docs tell people to check they have 0.20.0 or later, and `pip
    # install` over an older copy reports success while leaving it in place --
    # so "which version am I running" is the first question a broken install
    # asks, and argparse answered it with a usage error on exit 2.
    p.add_argument(
        "--version",
        action="version",
        version=f"anona {_version()}",
        help="print the installed version and exit",
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("start", parents=[common], help="Create a temporary profile with no account")
    rec = sub.add_parser("record", parents=[common], help="Store a memory (no MCP needed)")
    rec.add_argument(
        "content",
        nargs="?",
        default=None,
        help="The fact to store, as a complete sentence; `-` reads it from stdin",
    )
    rec.add_argument("--file", default=None, metavar="PATH", help="Read the content from this file instead")
    rec.add_argument("--space", type=verbs.non_empty, default=None, help=_SPACE_HELP.format(verb="write to"))
    rec.add_argument("--api-key", default=None, help=_API_KEY_HELP)
    rec.add_argument("--json", action="store_true", help=_JSON_HELP)
    rec.add_argument(
        "--tags",
        type=_tags,
        action="append",
        default=None,
        help="Comma-separated tags to file the memory under; repeating the flag adds more",
    )
    rec.add_argument(
        "--timestamp",
        type=_iso8601,
        default=None,
        help="When the event happened (ISO 8601), not when it is recorded",
    )
    rec.add_argument("--metadata", type=_metadata, default=None, help="A JSON object stored with the memory")
    rec.add_argument(
        "--async",
        action="store_true",
        dest="background",
        help="Queue the write and return a job id at once instead of waiting for extraction",
    )
    rec.add_argument(
        "--batch",
        default=None,
        metavar="FILE",
        help="Record up to 100 memories from a JSON Lines file (`-` for stdin): one "
        '{"content": ...} object or JSON string per line. Always queued',
    )
    verbs.wait_flags(rec)
    _scope_flags(rec, "Record under")
    ret = sub.add_parser("retrieve", parents=[common], help="Search your memories (no MCP needed)")
    ret.add_argument("query", help="What to search for")
    ret.add_argument("--space", type=verbs.non_empty, default=None, help=_SPACE_HELP.format(verb="search"))
    ret.add_argument("--api-key", default=None, help=_API_KEY_HELP)
    ret.add_argument("--json", action="store_true", help=_JSON_HELP)
    ret.add_argument("--limit", type=_limit, default=None, help="At most this many memories (1-100, default 10)")
    ret.add_argument(
        "--min-score",
        type=_min_score,
        default=None,
        help="Drop memories scoring below this. Scores span orders of magnitude and a correct match can score far below 0.01; see the docs before setting one",
    )
    _scope_flags(ret, "Only memories recorded with")
    ret.add_argument(
        "--tags",
        type=_tags,
        action="append",
        default=None,
        help="Only memories carrying these comma-separated tags; repeating the flag adds more",
    )
    ret.add_argument(
        "--tags-match",
        choices=_TAGS_MATCH,
        default=None,
        help="How --tags must match (default: any). any and all also return memories with no tags at all; "
        "any_strict, all_strict and exact exclude untagged memories",
    )
    ret.add_argument(
        "--as-of",
        type=_iso8601,
        default=None,
        help="Only memories recorded at or before this instant (ISO 8601)",
    )
    ret.add_argument(
        "--occurred-after",
        type=_iso8601,
        default=None,
        help="Only memories about events at or after this (ISO 8601)",
    )
    ret.add_argument(
        "--occurred-before",
        type=_iso8601,
        default=None,
        help="Only memories about events at or before this (ISO 8601)",
    )
    ret.add_argument(
        "--tag-groups",
        type=verbs.tag_groups,
        default=None,
        help="Compound tag filter: a JSON array of {tags, match} / {and} / {or} / {not}, "
        "AND-ed together and with any scope; @FILE reads it",
    )
    ret.add_argument(
        "--member-id",
        default=None,
        help="Only memories this member wrote; `me` for your own (in a personal space every memory is yours, so `me` matches them all)",
    )
    ret.add_argument(
        "--prefer-observations",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Return a synthesized note instead of the facts behind it (the server default); "
        "--no-prefer-observations returns both layers",
    )
    ret.add_argument(
        "--format",
        choices=("results", "block"),
        default="results",
        help="results: one line per memory (default); block: the prompt-ready context string",
    )
    ret.add_argument(
        "--max-tokens",
        type=_positive,
        default=None,
        help="With --format block, cap the block at about this many tokens",
    )
    rsn = sub.add_parser("reason", parents=[common], help="Ask a question and get a synthesized answer")
    rsn.add_argument("query", help="The question to answer from your memories")
    rsn.add_argument("--space", type=verbs.non_empty, default=None, help=_SPACE_HELP.format(verb="reason over"))
    rsn.add_argument("--api-key", default=None, help=_API_KEY_HELP)
    rsn.add_argument("--json", action="store_true", help=_JSON_HELP)
    rsn.add_argument(
        "--depth",
        choices=("fast", "thorough"),
        default=None,
        help="fast lets a current memory model answer alone; thorough always reads the memories under it",
    )
    _scope_flags(rsn, "Reason only over memories recorded with")
    verbs.add_parsers(sub, common, _SPACE_HELP, _API_KEY_HELP, _JSON_HELP)
    li = sub.add_parser(
        "login",
        parents=[common],
        help="Approve access in a browser: the command line, then MCP",
        description=_LOGIN_DESCRIPTION,
    )
    _which(li, "approve")
    lo = sub.add_parser("logout", parents=[common], help="Delete the stored credentials")
    _which(lo, "remove")
    lo.add_argument(
        "--force",
        action="store_true",
        help="also abandon an unclaimed temporary profile (it cannot be recovered)",
    )
    st = sub.add_parser("status", parents=[common], help="Show whether you are signed in")
    st.add_argument("--api-key", default=None, help="Describe this key's source instead of the usual lookup")
    sub.add_parser("mcp", parents=[common], help="Run the stdio MCP proxy for a client to launch")
    dr = sub.add_parser(
        "doctor",
        parents=[common],
        help="Check the install, which credential is used and why, and whether the server and space answer",
    )
    dr.add_argument("--space", type=verbs.non_empty, default=None, help="Space to check access to (default: the one a data command would use)")
    dr.add_argument("--api-key", default=None, help=_API_KEY_HELP)
    dr.add_argument("--json", action="store_true", help=_JSON_HELP)
    gd = sub.add_parser(
        "guide",
        parents=[common],
        help="Print a compact reference for agents: every command, flag, exit code and output shape",
    )
    gd.add_argument("--json", action="store_true", help="The command tree as JSON, generated from this parser")
    hp = sub.add_parser(
        "help",
        parents=[common],
        help="Show help for anona or one command (`help spaces delete`); `help --llm` prints the agent reference",
    )
    hp.add_argument("topic", nargs="*", metavar="COMMAND", help="The command to describe, e.g. `record` or `spaces delete`")
    hp.add_argument("--llm", action="store_true", help="Print `anona guide` instead")
    hp.add_argument("--json", action="store_true", help="With --llm, the command tree as JSON")
    cp = sub.add_parser("completion", parents=[common], help="Print a shell completion script")
    cp.add_argument("shell", choices=("bash", "zsh", "fish"), help="Which shell")
    return p


def _which(parser: argparse.ArgumentParser, verb: str) -> None:
    """`--cli` / `--mcp` on login and logout. Neither means both."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--cli",
        action="store_true",
        help=f"only {verb} the command-line credential (record, retrieve, reason and the other data commands)",
    )
    group.add_argument(
        "--mcp",
        action="store_true",
        help=f"only {verb} the MCP credential (`anona mcp`)",
    )


def _version() -> str:
    """The version of the code that is actually running.

    `anona.__version__`, NOT `importlib.metadata.version("anona")`. The
    metadata answers for the installed *distribution*, which is a different
    question and can give a different answer: run from a source tree that is
    ahead of (or simply unrelated to) an installed copy and the metadata
    reports the installed one. Measured here during ANO-428 -- a checkout at
    0.20.4 printed `anona 0.13.1`, which is the worst possible answer for a
    flag whose entire job is to tell someone which code they have.
    `__version__` is pinned to pyproject.toml by a test, so it cannot drift
    from the release either.
    """
    from anona import __version__

    return __version__


def _default_base_url() -> str:
    return os.environ.get("ANONA_BASE_URL") or store.DEFAULT_BASE_URL


def _refuse(exc: target.TargetError) -> int:
    print(f"anona: {exc}", file=sys.stderr)
    return 1


# record/retrieve/reason: a credential that is missing, refused or refused a
# host exits 4, not 1, so a script can tell "fix the credential" from "the
# request failed". Every code is in exits.py.
AUTH_EXIT = exits.AUTH
USAGE_EXIT = exits.USAGE


def _safe(text: object) -> str:
    from .memory import _printable

    return _printable(credential.redact(str(text)))


def _refuse_key(exc: target.TargetError) -> int:
    # The URL in the message came from a flag or a variable, which is the
    # vector this refusal exists for, so it is sanitized like server text.
    print(f"anona: {_safe(exc)}", file=sys.stderr)
    return AUTH_EXIT


def _data_credential(
    args: argparse.Namespace,
    base_url: str | None,
    requested: str | None,
    *,
    need_space: bool,
) -> tuple[str, str | None, str | None] | int:
    """(base URL, space, API key or None) for a data-plane verb, or the exit code.

    Local only: config.env, the environment and the stored file are read, and
    nothing is sent. With an API key the host is bound by target.for_api_key
    and, when the verb addresses a space, one must be named; without one the
    stored temporary profile's deployment is used and its space defaults later
    (memory._token_and_space), once the token is read.
    """
    config = config_env.load()
    key = credential.find_api_key(args.api_key, config)
    space = getattr(args, "space", None)
    if key is None:
        cli = store.load(store.cli_path())
        if cli is not None and cli.signed_in:
            # The command-line login. Pinned to the deployment that
            # issued it, like every stored credential.
            try:
                base = target.resolve(requested, cli.base_url)
            except target.TargetError as exc:
                return _refuse_key(exc)
            if need_space:
                # The same rule as an API key, and for the same reason: this
                # is an account, which can hold many spaces, and the server
                # implies none (/v1/record needs `space_id` or `route`).
                # `default` is only a temporary profile's, where it is the one
                # space there is.
                space = credential.find_space(space, config)
                if space is None:
                    print(
                        "anona: name a space. Pass --space, or set ANONA_SPACE_ID "
                        "(in the environment or ~/.anona/config.env); "
                        "`anona spaces list` shows yours. Nothing was sent.",
                        file=sys.stderr,
                    )
                    return USAGE_EXIT
            return base, space, None
        held = store.load()
        try:
            base = target.resolve(requested, held.base_url if held else None)
        except target.TargetError as exc:
            # A credential refused a host is an auth failure here.
            return _refuse_key(exc)
        return base, space, None
    if need_space:
        # An account can hold many spaces and none is implied: picking one
        # for the caller is how a write lands somewhere nobody looks.
        space = credential.find_space(space, config)
        if space is None:
            print(
                "anona: an API key needs a space. Pass --space, or set "
                "ANONA_SPACE_ID (in the environment or ~/.anona/config.env). "
                "Nothing was sent.",
                file=sys.stderr,
            )
            return USAGE_EXIT
    url, url_source = credential.find_base_url(base_url, config)
    try:
        base = target.for_api_key(url, url_source, key.source)
    except target.TargetError as exc:
        return _refuse_key(exc)
    return base, space, key.key


# An expired profile is gone and so is everything in it. It read as success,
# which is how a scripted `anona status && anona record ...` walked straight
# into a write with no credential behind it.
EXPIRED_EXIT = 1


def _status(
    requested: str | None = None,
    *,
    base_url_flag: str | None = None,
    api_key_flag: str | None = None,
) -> int:
    config = config_env.load()
    key = credential.find_api_key(api_key_flag, config)
    if key is None:
        cli_rc = _cli_status(requested, shadowed=False)
        if cli_rc is None:
            # No command-line login: exactly the output this command always had.
            return _store_status(requested)
        if cli_rc != 0:
            return cli_rc
        if store.load() is None:
            print("No MCP credential is stored. Run `anona login --mcp` to connect MCP clients.")
            return 0
        # A working command-line login decides the exit code; the MCP part is
        # described for what it is, whatever it says.
        _store_status(requested)
        return 0
    # The data verbs' credential. The source and a prefix only: this output
    # ends up in pastes and logs.
    print(
        f"API key for record/retrieve/reason: {key.display()} "
        f"(from {target.SOURCE_NAMES[key.source]}). Not checked here; "
        "`anona doctor` asks the server whether it works."
    )
    url, url_source = credential.find_base_url(base_url_flag, config)
    try:
        base = target.for_api_key(url, url_source, key.source)
    except target.TargetError as exc:
        return _refuse_key(exc)
    print(f"It is sent to {_safe(base)}.")
    space = credential.find_space(None, config)
    print(f"Space: {_safe(space)}." if space else "Space: none set; pass --space or set ANONA_SPACE_ID.")
    _cli_status(None, shadowed=True)
    if store.load() is None:
        print("No `anona login` credential is stored for MCP (that one is for `anona mcp`).")
        return 0
    # The stored credential is still worth describing: it is what `anona mcp`
    # uses. Its exit code does not decide this one -- the key works either way.
    # Introduced as such, or its "Temporary profile ..." line reads as the
    # credential this command just said the key had replaced.
    print("Also stored, for `anona mcp` (the data commands use the API key above):")
    _store_status(None)
    return 0


def _cli_status(requested: str | None, *, shadowed: bool) -> int | None:
    """Describe the command-line login, or None when there is none.

    Only what the file says -- never a token, never the client id, and no
    network call, which would rotate the refresh token just to look at it.
    """
    c = store.load(store.cli_path())
    if c is None or not c.signed_in:
        return None
    if requested is not None:
        try:
            target.resolve(requested, c.base_url)
        except target.TargetError as exc:
            return _refuse(exc)
    use = (
        "not used while an API key is set"
        if shadowed
        else "used by record, retrieve, reason and the other data commands"
    )
    print(f"Command-line login for {c.base_url} ({use}).")
    remaining = c.expires_at - time.time()
    if remaining > 0:
        print(f"Its access token expires in {int(remaining // 60)} minutes and is renewed automatically.")
    else:
        print("Its access token has expired; a refresh is attempted on the next command.")
    print("If a command reports an authorization failure, run `anona login --cli`.")
    return 0


def _store_status(requested: str | None = None) -> int:
    c = store.load()
    if c is None:
        print("Not logged in. Run `anona login`.")
        return 1
    # `--base-url` was read and then ignored here, so `anona status --base-url
    # https://other` described the stored deployment while naming another --
    # the one shape of wrong answer a status command must never give. Every
    # other credential-holding command already refuses a mismatch; this is the
    # same rule, not a new one.
    if requested is not None:
        try:
            target.resolve(requested, c.base_url)
        except target.TargetError as exc:
            return _refuse(exc)
    if c.temp_token and c.temp_expires_at is not None:
        # The only warning an unclaimed profile gets: it has no email address.
        print(f"Temporary profile for {c.base_url}: {temp.describe_deadline(c.temp_expires_at)}.")
        if c.temp_expires_at > time.time():
            print("Run `anona login` to claim it before then.")
        elif not c.signed_in:
            # Expired, and nothing else to fall back on.
            return EXPIRED_EXIT
        if not c.signed_in:
            return 0
    elif not c.signed_in:
        print("Not logged in. Run `anona login`.")
        return 1
    # Never the tokens or the client id: this output ends up in pastes and logs.
    # And only what the file can tell us: it cannot say whether the refresh
    # token is still good, and checking would mean a network call that rotates it.
    print(f"Credential found for {c.base_url}.")
    remaining = c.expires_at - time.time()
    if remaining > 0:
        # The minutes alone read as a deadline to act on; the token renews itself.
        print(
            f"Its access token expires in {int(remaining // 60)} minutes and is renewed "
            "automatically when `anona mcp` uses it."
        )
    else:
        print("Its access token has expired; `anona mcp` renews it on its next call.")
    print(
        "Run `anona login` again only if `anona mcp` reports an authorization failure "
        "(the login was revoked, or went unused for 30 days)."
    )
    return 0


def _logout(*, cli: bool, mcp: bool, force: bool) -> int:
    """Delete the stored credential(s), then revoke each at the server.

    Local first and unconditionally: a logout must work offline, and a
    revocation that fails (no network, an older server) leaves a refresh
    token that dies on its own after 30 unused days. Each file is removed
    under its own refresh lock, so a refresh finishing just after cannot write
    it back.
    """
    revoke: list[store.Credentials] = []
    # Whether a file was there to remove: "logged out" when nothing was stored
    # reads as though a credential existed and is now gone.
    removed = False
    if mcp:
        with tokens.credential_lock():
            held = store.load()
            if held is not None and held.live_temp_token() and not force:
                # The token is never printed, so this file is its only copy:
                # deleting it strands the profile and everything in it until
                # the server deletes it too. Checked before anything is
                # deleted, so a refused logout removes nothing at all.
                print(
                    f"anona logout: this would abandon an unclaimed temporary profile "
                    f"(it {temp.describe_deadline(held.temp_expires_at)}). "
                    "Run `anona login` to claim it, or `anona logout --force` to "
                    "discard it. `anona logout --cli` removes only the "
                    "command-line credential.",
                    file=sys.stderr,
                )
                return 1
    if cli:
        with tokens.credential_lock(store.cli_path()):
            held = store.load(store.cli_path())
            if held is not None and held.signed_in:
                revoke.append(held)
            removed = store.cli_path().exists() or removed
            store.clear(store.cli_path())
    if mcp:
        with tokens.credential_lock():
            held = store.load()
            if held is not None and held.signed_in:
                revoke.append(held)
            removed = store.path().exists() or removed
            store.clear()
    unrevoked = [c.base_url for c in revoke if not _revoke(c)]
    if unrevoked:
        print(
            "anona logout: removed locally, but the server could not be told to "
            "revoke it; it expires after 30 days unused, or revoke it now under "
            "Integrations -> MCP -> Connected apps in the dashboard.",
            file=sys.stderr,
        )
    if not removed:
        if cli and mcp:
            print("No login was stored; nothing to remove.")
        else:
            print(f"No {'command-line' if cli else 'MCP'} login was stored; nothing to remove.")
    elif cli and mcp:
        print("Logged out.")
    else:
        print("Logged out of the command line." if cli else "Logged out of MCP.")
    return 0


def _revoke(c: store.Credentials) -> bool:
    """Best effort. Pinned like any stored credential: the refresh token goes
    only to the deployment that issued it, over https except to loopback."""
    try:
        target.check_transport(c.base_url)
        with httpx.Client() as client:
            meta = oauth.discover(c.base_url, client)
            return oauth.revoke(meta, c.refresh_token, client)
    except Exception:
        return False


def _apply_globals(args: argparse.Namespace) -> int | None:
    """--profile, --timeout, --retries and --quiet, before anything reads a file."""
    from . import memory

    name = getattr(args, "profile", None)
    if name is not None:
        # Every path in store.py and config_env.py reads this, so one
        # assignment moves the credentials, the command-line login, config.env
        # and their locks together.
        os.environ[store.PROFILE_ENV] = name
    try:
        store.profile()
    except store.BadProfile as exc:
        print(f"anona: ANONA_PROFILE: {_safe(exc)}", file=sys.stderr)
        return USAGE_EXIT
    memory.OPTIONS["timeout"] = getattr(args, "timeout", None)
    memory.OPTIONS["max_retries"] = getattr(args, "retries", None)
    memory.OPTIONS["quiet"] = bool(getattr(args, "quiet", False))
    return None


def _record_usage(args: argparse.Namespace) -> int | None:
    """record's flag combinations that cannot work: exit 2, nothing sent."""

    def no(message: str) -> int:
        print(f"anona: {message} Nothing was sent.", file=sys.stderr)
        return USAGE_EXIT

    if args.batch is not None:
        if args.content is not None or args.file is not None:
            return no("give --batch, or the content / --file, not both.")
        if args.background:
            return no("--batch is always queued; drop --async.")
        given = [f for f, v in (("--tags", args.tags), ("--timestamp", args.timestamp), ("--metadata", args.metadata)) if v is not None]
        if given:
            return no(f"{', '.join(given)} cannot be combined with --batch; put them on each line of the file.")
    if args.wait and not (args.background or args.batch is not None):
        return no("--wait needs --async or --batch; a plain record already waits.")
    if args.wait_timeout is not None and not args.wait:
        return no("--wait-timeout needs --wait.")
    return None


def _help_topic(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """`anona help spaces delete`: that command's own --help, on stdout, exit 0."""
    if args.llm or args.json:
        print("anona: help COMMAND takes no --llm or --json. Nothing was printed.", file=sys.stderr)
        return USAGE_EXIT
    from .introspect import _subparsers

    current = parser
    for word in args.topic:
        sub = _subparsers(current)
        if sub is None or word not in sub.choices:
            known = ", ".join(sub.choices) if sub is not None else "none"
            print(
                f"anona: no command `{_safe(' '.join(args.topic))}`. "
                f"Commands here: {known}.",
                file=sys.stderr,
            )
            return USAGE_EXIT
        current = sub.choices[word]
    current.print_help()
    return 0


# What a process killed by SIGPIPE reports (128 + 13), as `git` does. Not in
# exits.py's table: it is what `anona guide | head` exits with, never an
# outcome a script branches on.
BROKEN_PIPE_EXIT = 141


def main(argv: list[str] | None = None) -> int:
    """Run one command. A reader that goes away (`anona guide | head`) ends it
    quietly: no traceback, and no "Exception ignored" at interpreter exit."""
    _tolerate_unencodable_stdout()
    try:
        try:
            return _main(argv)
        finally:
            # Also on SystemExit: argparse prints --help and exits inside
            # parse_args, and its output is still buffered here. A failed
            # flush replaces the SystemExit and is handled below.
            sys.stdout.flush()
    except BrokenPipeError:
        # Point stdout at /dev/null so the interpreter's own final flush has
        # nowhere to fail; the documented recipe for this.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError, AttributeError, io.UnsupportedOperation):
            pass
        return BROKEN_PIPE_EXIT


def _tolerate_unencodable_stdout() -> None:
    """A stdout that cannot hold what a memory says (Windows redirects to
    cp1252, a legacy locale) escapes the character instead of raising, the
    way Python's stderr already does. Content that encodes is unchanged."""
    try:
        sys.stdout.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    except (AttributeError, ValueError, io.UnsupportedOperation):
        pass


def _readable_argv(argv: list[str]) -> list[str]:
    """Bytes in argv that are not UTF-8 (a latin-1 terminal) reach Python as
    lone surrogates, which no JSON encoder will send. Replace them with U+FFFD
    so the damage is visible in what is stored, rather than a traceback. A
    word that names an existing path is left alone: open() accepts it as is."""
    out = []
    for a in argv:
        if any("\udc80" <= ch <= "\udcff" for ch in a) and not os.path.exists(a):
            a = a.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
        out.append(a)
    return out


def _guide_epilog() -> str:
    from .guide import EPILOG

    return EPILOG


def _main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    argv = _readable_argv(argv)
    if not argv:
        # Bare `anona` is how a person or an agent first meets the command:
        # orient them rather than fail with argparse's "arguments are required".
        # Only the truly empty line -- `anona --quiet` with no command is still
        # a usage error, since a script wrote it.
        from .guide import ORIENTATION

        sys.stdout.write(ORIENTATION)
        return 0
    parser = _parser()
    args = parser.parse_args(argv)
    bad = _apply_globals(args)
    if bad is not None:
        return bad
    if args.command == "help" and args.topic:
        return _help_topic(parser, args)
    if args.command == "guide" or (args.command == "help" and args.llm):
        from . import guide

        return guide.run(parser, as_json=args.json)
    if args.command == "help":
        if args.json:
            print("anona: --json needs --llm. Nothing was printed.", file=sys.stderr)
            return USAGE_EXIT
        parser.print_help()
        return 0
    if args.command == "completion":
        from . import completion

        print(completion.script(parser, args.shell))
        return 0
    base_url = getattr(args, "base_url", None)
    no_browser = getattr(args, "no_browser", False)
    # What the user named, if anything. Only `start` and `login` may follow it
    # to a new deployment; everything else holds a credential and is pinned to
    # the deployment that issued it (target.resolve).
    requested = base_url or os.environ.get("ANONA_BASE_URL") or None
    if args.command == "start":
        base = base_url or _default_base_url()
        try:
            target.check_transport(base)
        except target.TargetError as exc:
            return _refuse(exc)
        return temp.run_start(base)
    if args.command in ("record", "retrieve", "reason"):
        from . import memory

        # Usage first: a command that is wrong exits 2 before a credential is
        # looked up, and so before anything could be sent.
        content = ""
        items: list[dict] = []
        if args.command == "record":
            bad = _record_usage(args)
            if bad is not None:
                return bad
            if args.batch is not None:
                got_items = memory.read_batch(args.batch)
                if isinstance(got_items, int):
                    return got_items
                items = got_items
            else:
                got = memory.read_content(args.content, args.file)
                if isinstance(got, int):
                    return got
                content = got
        if args.command == "retrieve" and args.tags_match is not None and args.tags is None:
            print(
                "anona: --tags-match needs --tags; there is nothing for it to match. Nothing was sent.",
                file=sys.stderr,
            )
            return USAGE_EXIT
        if args.command == "retrieve" and args.max_tokens is not None and args.format != "block":
            print("anona: --max-tokens needs --format block. Nothing was sent.", file=sys.stderr)
            return USAGE_EXIT
        got = _data_credential(args, base_url, requested, need_space=True)
        if isinstance(got, int):
            return got
        base, space, api_key = got
        scope = {"user_id": args.user_id, "agent_id": args.agent_id, "session_id": args.session_id}
        if args.command == "record":
            wait = None
            if args.wait:
                wait = args.wait_timeout if args.wait_timeout is not None else verbs.DEFAULT_WAIT
            if args.batch is not None:
                return memory.run_record_batch(
                    base, items, space, api_key=api_key, as_json=args.json, wait=wait, **scope
                )
            return memory.run_record(
                base,
                content,
                space,
                api_key=api_key,
                as_json=args.json,
                tags=_flat(args.tags),
                timestamp=args.timestamp,
                metadata=args.metadata,
                background=args.background,
                wait=wait,
                **scope,
            )
        if args.command == "reason":
            return memory.run_reason(
                base, args.query, space, api_key=api_key, as_json=args.json, depth=args.depth, **scope
            )
        return memory.run_retrieve(
            base,
            args.query,
            space,
            as_json=args.json,
            limit=args.limit,
            min_score=args.min_score,
            tags=_flat(args.tags),
            tags_match=args.tags_match,
            as_of=args.as_of,
            occurred_after=args.occurred_after,
            occurred_before=args.occurred_before,
            api_key=api_key,
            tag_groups=args.tag_groups,
            member_id=args.member_id,
            prefer_observations=args.prefer_observations,
            block=args.format == "block",
            max_tokens=args.max_tokens,
            **scope,
        )
    if args.command in verbs.COMMANDS:
        # Usage first, as above: nothing is looked up or sent for a command
        # that is wrong.
        from . import resources

        if resources.is_dashboard_only(verbs.verb_of(args)):
            # Said before any credential is read: it needs none, and a script
            # that cannot do this from here should learn so on any machine.
            return resources.refuse_dashboard(verbs.verb_of(args), args)
        bad = verbs.precheck(args)
        if bad is not None:
            return bad
        got = _data_credential(
            args, base_url, requested, need_space=verbs.needs_space(verbs.verb_of(args))
        )
        if isinstance(got, int):
            return got
        base, space, api_key = got
        return verbs.run(args, base, space, api_key)
    if args.command == "login":
        base = base_url or _default_base_url()
        try:
            target.check_transport(base)
        except target.TargetError as exc:
            return _refuse(exc)
        print(f"Signing in to {base}")
        # Neither flag means both approvals; argparse keeps the two exclusive.
        want_cli = not args.mcp
        want_mcp = not args.cli
        held_cli = store.load(store.cli_path()) if want_cli else None
        if held_cli and held_cli.signed_in and held_cli.base_url.rstrip("/") != base.rstrip("/"):
            print(
                f"Note: the stored command-line credential is for {held_cli.base_url}; "
                "this replaces it."
            )
        stored = store.load()
        if want_mcp and stored and stored.base_url.rstrip("/") != base.rstrip("/"):
            print(f"Note: the stored credential is for {stored.base_url}; this replaces it.")
            # A profile belongs to the deployment that minted it, so its token
            # cannot be carried onto this one — `_live_temp_token` matches on
            # base_url, and retagging it would present one host's token to
            # another. But the token is never printed, so this file is its
            # only copy and replacing it is the same loss `anona logout`
            # refuses to do silently. Say so while they can still cancel.
            if stored.live_temp_token():
                print(
                    f"Note: that includes an unclaimed temporary profile "
                    f"(it {temp.describe_deadline(stored.temp_expires_at)}), "
                    f"which will be abandoned. Claim it first with "
                    f"`anona login --base-url {stored.base_url}`."
                )
        if stored and stored.live_temp_token() and stored.base_url.rstrip("/") == base.rstrip("/"):
            print(
                f"Signing in will try to claim your temporary profile, which "
                f"{temp.describe_deadline(stored.temp_expires_at)}."
            )
        if want_cli and want_mcp:
            return login.run_logins(base, open_browser=not no_browser)
        if want_cli:
            return login.run_cli_login(base, open_browser=not no_browser)
        return login.run_login(base, open_browser=not no_browser)
    if args.command == "logout":
        return _logout(cli=not args.mcp, mcp=not args.cli, force=getattr(args, "force", False))
    if args.command == "status":
        return _status(requested, base_url_flag=base_url, api_key_flag=args.api_key)
    if args.command == "doctor":
        from . import doctor

        return doctor.run(args, base_url, requested, version=_version())
    # mcp: a credential belongs to the deployment that issued it. Naming
    # another is refused on stderr; stdout is the client's JSON-RPC stream.
    stored = store.load()
    try:
        base = target.resolve(requested, stored.base_url if stored else None)
    except target.TargetError as exc:
        return _refuse(exc)
    return proxy.run_proxy(base, sys.stdin, sys.stdout, sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
