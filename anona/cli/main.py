"""The `anona` command: login, logout, status, mcp."""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import login, proxy, store, tokens


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
        description="Sign in to Anona Memory and connect MCP clients.",
        parents=[common],
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("login", parents=[common], help="Approve access in a browser and store a credential")
    sub.add_parser("logout", parents=[common], help="Delete the stored credential")
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
    if args.command == "login":
        base = base_url or _default_base_url()
        print(f"Signing in to {base}")
        stored = store.load()
        if stored and stored.base_url.rstrip("/") != base.rstrip("/"):
            print(f"Note: the stored credential is for {stored.base_url}; this replaces it.")
        return login.run_login(base, open_browser=not no_browser)
    if args.command == "logout":
        # Under the refresh lock, so a refresh finishing just after cannot
        # write the credential back.
        with tokens.credential_lock():
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
