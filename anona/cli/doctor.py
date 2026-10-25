"""`anona doctor`: what this install would do, and whether it would work.

Answers, in order: which Python and which `anona` are running, which profile
and directory, which credential a data command would use and why the others
lost, which server it talks to, whether that server's /health answers, and
whether the space a data command would use is reachable with that credential,
and how many credits that credential's organization has left. Reads spend
credits as well as writes, so a balance of zero is a problem worth naming: a
reachable space is no use when every call to it is refused.

Never prints a secret. A key is shown only as `ApiKey.display()` (its prefix
and four characters); a login token is never shown at all. The /health probe
carries no credential: it is a public route, and a doctor that sent a key to
check reachability would send it to whatever host a mistyped --base-url names.
"""

from __future__ import annotations

import argparse
import platform
import sys
import time

import httpx

from anona import AnonaClient, AnonaError

from . import config_env, credential, exits, store, target
from .memory import _client_kwargs, _emit_json, _printable, _token_and_space


def _cred(args: argparse.Namespace, base_url_flag: str | None, requested: str | None, config: dict) -> dict:
    """Which credential wins, mirroring main._data_credential, with no side effects."""
    key = credential.find_api_key(args.api_key, config)
    cli = store.load(store.cli_path())
    held = store.load()
    others: list[str] = []
    if cli is not None and cli.signed_in:
        others.append("command-line login (`anona login --cli`)")
    if held is not None and held.live_temp_token():
        others.append("temporary profile (`anona start`)")
    if held is not None and held.signed_in:
        others.append("MCP login (used only by `anona mcp`)")
    if key is not None:
        url, url_source = credential.find_base_url(base_url_flag, config)
        info = {
            "kind": "api_key",
            "display": key.display(),
            "source": target.SOURCE_NAMES[key.source],
            "why": "an API key outranks every stored login (--api-key, then ANONA_API_KEY, then config.env)",
            "base_url_source": {"flag": "--base-url", "env": "ANONA_BASE_URL", "config.env": "config.env"}.get(
                url_source or "", "default for an API key"
            ),
            "shadowed": others,
        }
        try:
            info["base_url"] = target.for_api_key(url, url_source, key.source)
        except target.TargetError as exc:
            info["base_url"] = None
            info["error"] = str(exc)
        return info
    if cli is not None and cli.signed_in:
        info = {
            "kind": "cli_login",
            "display": "command-line login",
            "source": str(store.cli_path()),
            "why": "no API key is set, and the command-line login comes next",
            "expires_in_s": int(cli.expires_at - time.time()),
            "base_url_source": "the deployment that issued the login",
            "shadowed": [o for o in others if not o.startswith("command-line")],
        }
        try:
            info["base_url"] = target.resolve(requested, cli.base_url)
        except target.TargetError as exc:
            info["base_url"] = None
            info["error"] = str(exc)
        return info
    if held is not None and held.live_temp_token():
        info = {
            "kind": "temp_profile",
            "display": "temporary profile",
            "source": str(store.path()),
            "why": "no API key and no command-line login; the temporary profile is last",
            "base_url_source": "the deployment that created the profile",
            "shadowed": [o for o in others if o.startswith("MCP")],
        }
        try:
            info["base_url"] = target.resolve(requested, held.base_url)
        except target.TargetError as exc:
            info["base_url"] = None
            info["error"] = str(exc)
        return info
    why = "nothing usable is stored"
    if held is not None and held.signed_in:
        why = "only the MCP login is stored, and the REST API refuses it"
    elif held is not None and held.temp_token:
        why = "the temporary profile has expired"
    return {
        "kind": None,
        "display": "none",
        "source": None,
        "why": why,
        "base_url": None,
        "error": "no credential: run `anona login --cli`, set ANONA_API_KEY, or `anona start`",
        "shadowed": others,
    }


def _health(base_url: str) -> dict:
    """GET /health with no credential. Reachability only."""
    try:
        target.check_transport(base_url)
    except target.TargetError as exc:
        return {"ok": False, "status": None, "error": str(exc)}
    timeout = _client_kwargs().get("timeout", 10.0)
    started = time.monotonic()
    try:
        resp = httpx.get(base_url.rstrip("/") + "/health", timeout=timeout)
    except httpx.HTTPError as exc:
        return {"ok": False, "status": None, "error": type(exc).__name__}
    elapsed = round(time.monotonic() - started, 3)
    body_ok = False
    try:
        body = resp.json()
        body_ok = isinstance(body, dict) and body.get("status") == "ok"
    except ValueError:
        pass
    return {"ok": resp.status_code == 200 and body_ok, "status": resp.status_code, "seconds": elapsed, "error": None}


def _space(cred: dict, args: argparse.Namespace, config: dict, api_key: str | None) -> tuple[dict, str | None]:
    """Which space a data command would use, whether it answers, and the
    bearer token the check used (None when there was none to use)."""
    if cred["kind"] == "temp_profile":
        space, source = (args.space, "--space") if args.space else ("default", "a temporary profile's only space")
    else:
        space = credential.find_space(args.space, config)
        source = "--space" if args.space else ("ANONA_SPACE_ID" if space else None)
    info: dict = {"id": space, "source": source, "accessible": None, "error": None}
    if space is None:
        info["error"] = "no space named: pass --space or set ANONA_SPACE_ID"
        return info, api_key if cred.get("base_url") is not None and cred["kind"] is not None else None
    if cred.get("base_url") is None or cred["kind"] is None:
        return info, None
    if api_key is not None:
        token = api_key
    else:
        got = _token_and_space(space)
        if isinstance(got, int):
            info["error"] = "the stored credential could not be used (see above)"
            info["code"] = got
            return info, None
        token = got.token
    try:
        with AnonaClient(api_key=token, base_url=cred["base_url"], **_client_kwargs()) as client:
            client.get_space(space)
        info["accessible"] = True
    except AnonaError as exc:
        info["accessible"] = False
        info["status"] = None if getattr(exc, "transport", False) else exc.status_code
        info["error"] = exc.code or ("unreachable" if getattr(exc, "transport", False) else f"HTTP {exc.status_code}")
        info["code"] = exits.for_status(exc.status_code, transport=getattr(exc, "transport", False))
    return info, token


def _credits(base_url: str, token: str) -> dict:
    """The credit balance `anona usage` reports, read the same way.

    Never fails doctor: a usage call that does not answer is reported as an
    unknown balance, which says nothing either way about the other checks.
    """
    info: dict = {"remaining": None, "limit": None, "used": None, "exhausted": None, "error": None}
    try:
        with AnonaClient(api_key=token, base_url=base_url, **_client_kwargs()) as client:
            doc = client.get_usage()
    except AnonaError as exc:
        transport = getattr(exc, "transport", False)
        info["error"] = exc.code or ("unreachable" if transport else f"HTTP {exc.status_code}")
        return info
    d = doc if isinstance(doc, dict) else {}
    for field, key in (("remaining", "credits_remaining"), ("limit", "credits_limit"), ("used", "credits_used")):
        value = d.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            info[field] = value
    if info["remaining"] is None:
        info["error"] = "the usage answer carried no balance"
        return info
    info["exhausted"] = info["remaining"] <= 0
    return info


def run(args: argparse.Namespace, base_url_flag: str | None, requested: str | None, *, version: str) -> int:
    from anona import cli as _cli_pkg

    config = config_env.load()
    cred = _cred(args, base_url_flag, requested, config)
    key = credential.find_api_key(args.api_key, config) if cred["kind"] == "api_key" else None
    report: dict = {
        "anona_version": version,
        "python": platform.python_version(),
        "executable": sys.executable,
        "package_path": str(getattr(_cli_pkg, "__path__", [""])[0]),
        "profile": store.profile() or "default",
        "profile_dir": str(store.anona_dir()),
        "credential": cred,
    }
    health = _health(cred["base_url"]) if cred.get("base_url") else None
    report["health"] = health
    space, token = _space(cred, args, config, key.key if key is not None else None)
    report["space"] = space
    # Only against a server that answered /health, with a credential the space
    # check could use: anything else is already a reported problem, and a
    # second call into it would only repeat the wait.
    credits = _credits(cred["base_url"], token) if token is not None and health is not None and health["ok"] else None
    report["credits"] = credits

    rc = exits.OK
    if cred["kind"] is None or cred.get("error"):
        rc = exits.AUTH
    elif health is not None and not health["ok"]:
        rc = exits.UNAVAILABLE
    elif space.get("code"):
        rc = space["code"]
    elif space["id"] is None:
        rc = exits.USAGE
    elif credits is not None and credits["exhausted"]:
        rc = exits.RATE_LIMITED
    report["ok"] = rc == exits.OK

    if args.json:
        _emit_json(report)
        return rc
    p = _printable
    print(f"anona {p(version)} on Python {p(report['python'])} ({p(report['executable'])})")
    print(f"Profile: {p(report['profile'])} ({p(report['profile_dir'])})")
    if cred["kind"] is None:
        print(f"Credential: none -- {p(cred['why'])}.")
    else:
        print(f"Credential: {p(cred['display'])} from {p(cred['source'])}.")
        print(f"  Why: {p(cred['why'])}.")
    for other in cred.get("shadowed") or []:
        print(f"  Also present, not used here: {p(other)}.")
    if cred.get("error"):
        print(f"  Problem: {p(credential.redact(cred['error']))}")
    if cred.get("base_url"):
        print(f"Server: {p(cred['base_url'])} ({p(cred.get('base_url_source') or '')}).")
    if health is not None:
        if health["ok"]:
            print(f"Health: ok (HTTP 200 in {health['seconds']} s).")
        elif health["status"] is not None:
            print(f"Health: HTTP {health['status']} -- the server answered but is not healthy.")
        else:
            print(f"Health: unreachable ({p(credential.redact(str(health['error'])))}).")
    if space["id"] is None:
        print(f"Space: none -- {p(space['error'])}.")
    else:
        line = f"Space: {p(space['id'])} (from {p(space['source'])})"
        if space["accessible"] is True:
            line += ": accessible."
        elif space["accessible"] is False:
            line += f": NOT accessible ({p(space['error'])})."
        else:
            line += "."
        print(line)
    if credits is not None:
        limit = "-" if credits["limit"] is None else p(credits["limit"])
        if credits["remaining"] is None:
            print(f"Credits: unknown ({p(credits['error'])}).")
        elif credits["exhausted"]:
            print(
                f"Credits: {p(credits['remaining'])} remaining of {limit}."
                "\n  Problem: credits exhausted; writes and reads will be refused with exit 6."
            )
        else:
            print(f"Credits: {p(credits['remaining'])} remaining of {limit}.")
    print("All good." if rc == exits.OK else f"Problems found (exit {rc}; see `anona guide` for exit codes).")
    return rc
