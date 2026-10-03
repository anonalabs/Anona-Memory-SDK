"""`anona mcp`: a stdio MCP server that forwards to the remote one.

Any MCP client can launch a stdio server, including the ones with no OAuth
support, so this is what makes login work everywhere. The proxy holds the
tokens and refreshes them; nothing secret ever sits in a client's config file.

The remote is Streamable HTTP in stateless JSON mode: every POST returns one
complete JSON response and there is no session. So this is a forwarder, not a
transport implementation, and the remote's tool list (including tools added
later) appears here with no schema maintained on this side.

Three rules, each pinned by a test:
  * stdout is the transport and carries nothing but JSON-RPC. Diagnostics go to
    stderr.
  * A 401 refreshes once and retries once, per message. Never a loop.
  * A failure becomes a JSON-RPC error carrying the request's id, not a
    traceback: a crashed proxy takes the client's whole memory capability with
    it, a failed call does not.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from . import store, tokens

# `reason` is an agent loop and can legitimately run for a minute and a half.
_TIMEOUT = 120

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_SERVER_ERROR = -32000


def _error(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


class _Failure(Exception):
    """A failure whose message is safe to show the client and the user."""

    def __init__(self, message: str, code: int = _SERVER_ERROR):
        super().__init__(message)
        self.code = code


def _post(client: httpx.Client, url: str, token: str, msg: dict | list) -> httpx.Response:
    return client.post(
        url,
        json=msg,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=_TIMEOUT,
    )


def _bearer(client: httpx.Client) -> tuple[str, bool]:
    """(token, is_temp). A signed-in OAuth credential always wins.

    The temp token is the fallback only when there is no login: once someone
    has logged in, their session is the credential and a leftover temp token
    must not be sent. It lives here rather than in `tokens.access_token`
    because that function's whole contract is the refresh-and-rotate path,
    which a temp token has no part in.
    """
    c = store.load()
    if c is not None and c.signed_in:
        return tokens.access_token(client), False
    temp = c.live_temp_token() if c is not None else None
    if temp is not None:
        return temp, True
    if c is not None and c.temp_token:
        raise tokens.NotLoggedIn(
            "The temporary profile has expired. Run `anona start` for a new one, "
            "or `anona login`."
        )
    return tokens.access_token(client), False  # raises NotLoggedIn


def _forward(client: httpx.Client, url: str, msg: dict | list) -> str | None:
    """The response line to write, or None when there is nothing to say."""
    token, is_temp = _bearer(client)
    resp = _post(client, url, token, msg)
    if resp.status_code == 401 and is_temp:
        # Opaque and fixed: there is nothing to refresh, only expired or claimed.
        raise _Failure(
            "The server rejected the temporary profile (expired, or already claimed). "
            "Run `anona login`."
        )
    if resp.status_code == 401:
        token = tokens.access_token(client, force_refresh=True, rejected=token)
        resp = _post(client, url, token, msg)
        if resp.status_code == 401:
            raise _Failure("The server rejected the stored login. Run `anona login` again.")
    if resp.status_code >= 400:
        raise _Failure(f"The Anona server answered HTTP {resp.status_code}.")
    if not resp.content.strip():
        # 202 for a notification is normal. For a request it would leave the
        # client waiting forever, so the caller turns it into an error.
        return None
    try:
        body = json.loads(resp.content)
    except ValueError:
        raise _Failure("The Anona server returned something that is not JSON.") from None
    # Re-serialise: framing is one message per line, and a pretty-printed body
    # would arrive as several malformed ones. Also guarantees JSON on stdout.
    return json.dumps(body, separators=(",", ":"))


def _is_request(m: Any) -> bool:
    return not (isinstance(m, dict) and "id" not in m)


def _handle(client: httpx.Client, url: str, line: str, stderr) -> str | None:
    try:
        msg = json.loads(line)
    except ValueError:
        return json.dumps(_error(None, _PARSE_ERROR, "Parse error: not valid JSON."))
    # JSON-RPC batches are forwarded: the gateway answers them (checked against
    # the live /mcp: an array in, an array out), so rejecting one here would
    # block traffic the server accepts. Anything else that is not an object is
    # not a message at all.
    if not isinstance(msg, (dict, list)):
        return json.dumps(_error(None, _INVALID_REQUEST, "Invalid request: expected a JSON object."))

    # Ids that are owed a reply. A message with no "id" is a notification.
    owed = [m.get("id") if isinstance(m, dict) else None for m in (msg if isinstance(msg, list) else [msg]) if _is_request(m)]
    batch = isinstance(msg, list)
    try:
        out = _forward(client, url, msg)
        if out is None and owed:
            raise _Failure("The Anona server sent no response to a request.")
        return out
    except Exception as exc:  # noqa: BLE001 - one failed call must not end the session
        if isinstance(exc, (tokens.NotLoggedIn, _Failure)):
            text = str(exc)
        else:
            text = f"Request failed: {type(exc).__name__}: {exc}"
        print(f"anona mcp: {text}", file=stderr, flush=True)
        if not owed:
            # An error with a null id would be an unsolicited response.
            return None
        code = exc.code if isinstance(exc, _Failure) else _SERVER_ERROR
        errs = [_error(i, code, text) for i in owed]
        return json.dumps(errs if batch else errs[0])


def run_proxy(base_url: str, stdin, stdout, stderr) -> int:
    url = base_url.rstrip("/") + "/mcp"
    with httpx.Client() as client:
        for raw in stdin:
            line = raw.strip()
            if not line:
                continue
            out = _handle(client, url, line, stderr)
            if out is not None:
                stdout.write(out + "\n")
                stdout.flush()
    return 0
