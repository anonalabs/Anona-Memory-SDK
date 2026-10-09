"""The `anona` command: start, login, logout, status, mcp, record, retrieve, reason."""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import login, proxy, store, temp, tokens


def _parser() -> argparse.ArgumentParser:
    # Both flags are accepted before OR after the command, like the Node CLI.
    # SUPPRESS matters: with a real default on the subparser's copy, argparse
    # overwrites a value given before the command with that default.
    common = argparse.ArgumentParser(add_help=False)
    # No default here, so `mcp` can tell "the user said so" from "nobody said
    # anything" to prefer the stored deployment.
    common.add_argument(
        "--base-url",
        default=argparse.SUPPRESS,
        help=f"Anona server (default: $ANONA_BASE_URL, else {store.DEFAULT_BASE_URL})",
    )
    # Global, as in Node. Only `login` has a browser to skip; elsewhere it is ignored.
    common.add_argument(
        "--no-browser",
        action="store_true",
        default=argparse.SUPPRESS,
        help="login: only print the URL; do not try to open it",
    )
    p = argparse.ArgumentParser(
        prog="anona",
        description="Anona Memory: sign in, connect MCP clients, and record, retrieve or reason over memories.",
        parents=[common],
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("start", parents=[common], help="Create a temporary profile with no account")
    rec = sub.add_parser("record", parents=[common], help="Store a memory (no MCP needed)")
    rec.add_argument("content", help="The fact to store, as a complete sentence")
    rec.add_argument("--space", default=None, help="Space to write to (required when signed in)")
    ret = sub.add_parser("retrieve", parents=[common], help="Search your memories (no MCP needed)")
    ret.add_argument("query", help="What to search for")
    ret.add_argument("--space", default=None, help="Space to search (required when signed in)")
    rsn = sub.add_parser("reason", parents=[common], help="Ask a question and get a synthesized answer")
    rsn.add_argument("query", help="The question to answer from your memories")
    rsn.add_argument("--space", default=None, help="Space to reason over (required when signed in)")
    sub.add_parser("login", parents=[common], help="Approve access in a browser and store a credential")
    lo = sub.add_parser("logout", parents=[common], help="Delete the stored credential")
    lo.add_argument(
        "--force",
        action="store_true",
        help="also abandon an unclaimed temporary profile (it cannot be recovered)",
    )
    sub.add_parser("status", parents=[common], help="Show whether you are signed in")
    sub.add_parser("mcp", parents=[common], help="Run the stdio MCP proxy for a client to launch")
    return p


def _default_base_url() -> str:
    return os.environ.get("ANONA_BASE_URL") or store.DEFAULT_BASE_URL


def _status() -> int:
    c = store.load()
    if c is None:
        print("Not logged in. Run `anona login`.")
        return 1
    if c.temp_token and c.temp_expires_at is not None:
        # The only warning an unclaimed profile gets: it has no email address.
        print(f"Temporary profile for {c.base_url}: {temp.describe_deadline(c.temp_expires_at)}.")
        if c.temp_expires_at > time.time():
            print("Run `anona login` to claim it before then.")
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
        print(f"Stored access token expires in {int(remaining // 60)} minutes.")
    else:
        print("Stored access token has expired; a refresh is attempted on the next MCP call.")
    print("The server may still reject it (revoked, or unused for 30 days).")
    print("If `anona mcp` reports an authorization failure, run `anona login`.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    base_url = getattr(args, "base_url", None)
    no_browser = getattr(args, "no_browser", False)
    if args.command == "start":
        return temp.run_start(base_url or _default_base_url())
    if args.command in ("record", "retrieve", "reason"):
        from . import memory

        # Like `mcp`: a credential belongs to the deployment that issued it.
        held = store.load()
        base = base_url or (held.base_url if held else None) or _default_base_url()
        if args.command == "record":
            return memory.run_record(base, args.content, args.space)
        if args.command == "reason":
            return memory.run_reason(base, args.query, args.space)
        return memory.run_retrieve(base, args.query, args.space)
    if args.command == "login":
        base = base_url or _default_base_url()
        print(f"Signing in to {base}")
        stored = store.load()
        if stored and stored.base_url.rstrip("/") != base.rstrip("/"):
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
        return login.run_login(base, open_browser=not no_browser)
    if args.command == "logout":
        # Under the refresh lock, so a refresh finishing just after cannot
        # write the credential back.
        with tokens.credential_lock():
            held = store.load()
            if held is not None and held.live_temp_token() and not getattr(args, "force", False):
                # The token is never printed, so this file is its only copy:
                # deleting it strands the profile and everything in it until
                # the server deletes it too.
                print(
                    f"anona logout: this would abandon an unclaimed temporary profile "
                    f"(it {temp.describe_deadline(held.temp_expires_at)}). "
                    "Run `anona login` to claim it, or `anona logout --force` to "
                    "discard it.",
                    file=sys.stderr,
                )
                return 1
            store.clear()
        print("Logged out.")
        return 0
    if args.command == "status":
        return _status()
    # mcp: a credential belongs to the deployment that issued it, so talk to
    # that one unless the user explicitly named another.
    stored = store.load()
    base = base_url or (stored.base_url if stored else None) or _default_base_url()
    return proxy.run_proxy(base, sys.stdin, sys.stdout, sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
