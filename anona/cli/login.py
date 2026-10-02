"""`anona login`: open a browser, catch the redirect on loopback, store a token.

RFC 8252 native-app flow. Nothing in here prints or logs a token, an
authorization code or the PKCE verifier; the authorization URL is the one
thing that must be printed.
"""

from __future__ import annotations

import re
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import httpx

from . import oauth, store, tokens

TIMEOUT_SECONDS = 300
CALLBACK_PATH = "/cb"

_PAGE = (
    "<!doctype html><meta charset=utf-8><title>Anona</title>"
    "<body style='font-family:system-ui;max-width:32rem;margin:4rem auto'>"
    "<h1>{title}</h1><p>{body}</p></body>"
)
# Static text only. No query parameter is ever interpolated into a page.
_OK_PAGE = _PAGE.format(
    title="You are signed in", body="You can close this tab and return to your terminal."
)
_DENIED_PAGE = _PAGE.format(
    title="Sign-in was not completed", body="Return to your terminal for details."
)
_STATE_PAGE = _PAGE.format(
    title="Request refused",
    body="The state value did not match this login, so it was ignored.",
)
_BAD_PAGE = _PAGE.format(title="Request refused", body="This request carried no authorization result.")


class Abort(Exception):
    """A clean, message-only exit."""


class _Callback(BaseHTTPRequestHandler):
    # Per-connection read timeout. server.timeout covers only the select, so
    # without this a peer that connects (browsers preconnect) and sends nothing
    # holds handle_request, and the overall deadline, forever.
    timeout = 5

    def _reply(self, status: int, page: str) -> None:
        data = page.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path != CALLBACK_PATH:
            self._reply(404, _BAD_PAGE)
            return
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        server = self.server
        expected = getattr(server, "expected_state", None)
        got = q.get("state", "")
        # Constant-time, and on the error path too: otherwise any local process
        # can abort the login with a forged denial.
        if not expected or not secrets.compare_digest(got.encode(), expected.encode()):
            self._reply(400, _STATE_PAGE)
            return
        if "error" in q:
            server.error = q["error"]
            self._reply(200, _DENIED_PAGE)
        elif q.get("code"):
            server.code = q["code"]
            self._reply(200, _OK_PAGE)
        else:
            self._reply(400, _BAD_PAGE)

    def log_message(self, format: str, *args: object) -> None:
        # The default writes the request line, which contains the code.
        pass


def _make_server() -> HTTPServer:
    # 127.0.0.1, never 0.0.0.0: the code must not be reachable from the network.
    # Port 0 lets the OS pick, which is what the gateway's loopback rule expects.
    server = HTTPServer(("127.0.0.1", 0), _Callback)
    server.expected_state = None
    server.code = None
    server.error = None
    return server


def _await_callback(server: HTTPServer, timeout: float = TIMEOUT_SECONDS) -> str:
    """Serve requests until a verified code or error arrives, or time runs out."""
    deadline = time.monotonic() + timeout
    while server.code is None and server.error is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Abort(
                f"Timed out after {int(timeout)}s waiting for the browser. Run `anona login` again."
            )
        server.timeout = remaining
        server.handle_request()
    if server.error is not None:
        if server.error == "access_denied":
            raise Abort("Access was denied in the browser. Nothing was saved.")
        safe = re.sub(r"[^A-Za-z0-9_.-]", "?", server.error)[:64]
        raise Abort(f"The authorization server returned an error: {safe}. Nothing was saved.")
    return server.code


def _exchange(meta: dict, client_id: str, redirect_uri: str, code: str, verifier: str) -> store.Credentials:
    try:
        resp = httpx.post(
            meta["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
            },
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise Abort(f"Could not reach the token endpoint: {type(exc).__name__}.") from exc
    if resp.status_code != 200:
        raise Abort(f"The token endpoint refused the code ({resp.status_code}). Run `anona login` again.")
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise Abort("The token endpoint returned something that is not a JSON object.")
    access, refresh, ttl = body.get("access_token"), body.get("refresh_token"), body.get("expires_in")
    if (
        not isinstance(access, str)
        or not access
        or not isinstance(refresh, str)
        or not refresh
        or isinstance(ttl, bool)
        or not isinstance(ttl, (int, float))
        or ttl <= 0
    ):
        raise Abort("The token response was incomplete, so nothing was saved.")
    return store.Credentials(
        access_token=access,
        refresh_token=refresh,
        expires_at=time.time() + ttl,
        client_id=client_id,
        base_url="",  # filled in by the caller
    )


def run_login(base_url: str, open_browser: bool = True) -> int:
    server = None
    try:
        with httpx.Client() as client:
            meta = oauth.discover(base_url, client)
            server = _make_server()
            redirect_uri = f"http://127.0.0.1:{server.server_address[1]}{CALLBACK_PATH}"
            client_id = oauth.register_client(meta, redirect_uri, client)
        verifier, challenge = oauth.new_pkce()
        server.expected_state = secrets.token_urlsafe(32)
        url = oauth.authorize_url(meta, client_id, redirect_uri, challenge, server.expected_state)

        # Printed unconditionally: webbrowser.open is False over SSH and in
        # containers, and True is not proof a window appeared.
        print("Open this URL in a browser to sign in to Anona:")
        print(f"\n  {url}\n")
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        print("Waiting for approval...", flush=True)

        code = _await_callback(server, TIMEOUT_SECONDS)
        creds = _exchange(meta, client_id, redirect_uri, code, verifier)
        creds.base_url = base_url
        try:
            # Under the refresh lock: a proxy that finishes rotating just after
            # this must not overwrite the credential the user just approved
            # with the old family's rotated pair.
            with tokens.credential_lock():
                store.save(creds)
        except OSError as exc:
            raise Abort(
                f"Approval succeeded, but the credential could not be saved to "
                f"{store.path()}: {exc.strerror or type(exc).__name__}. "
                f"Fix that and run `anona login` again."
            ) from exc
    except (Abort, oauth.OAuthError) as exc:
        print(f"anona login: {exc}", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.server_close()
    print("Signed in. MCP clients can now connect without an API key.")
    return 0
