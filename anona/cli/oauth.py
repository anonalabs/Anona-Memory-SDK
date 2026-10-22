"""The OAuth 2.1 native-app flow, minus the browser and the listener.

Split out so the parts with no I/O and one HTTP call each can be tested
without opening anything. Task 3 owns the browser and the loopback socket.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from urllib.parse import urlencode, urlsplit

import httpx

WELL_KNOWN = "/.well-known/oauth-authorization-server"


class OAuthError(Exception):
    """Anything that should stop the login with a message the human can act on."""


class RegistrationRejected(OAuthError):
    """The authorization server refused this registration.

    Carries the server's own status and RFC 6749 `error` code so the caller can
    decide whether the refusal is one it can recover from. `register_client`
    raises this instead of a bare `OAuthError` for every non-2xx, because the
    one thing that distinguishes a recoverable refusal from a fatal one is the
    server's answer, and discarding it is what made the stranded-token case
    unrecoverable.
    """

    def __init__(self, message: str, *, status: int, error: str | None) -> None:
        super().__init__(message)
        self.status = status
        self.error = error


def _server_error(
    resp: httpx.Response, redact: str | None = None
) -> tuple[str | None, str | None]:
    """The server's own `(error, error_description)`, through either envelope.

    RFC 6749 puts the pair at the top level, which is what a third-party
    authorization server sends. Our own gateway wraps every structured detail
    in its `{"error": {...}}` envelope, so the same pair arrives one level
    deeper. Read both rather than pick one: this function's only job is to find
    a sentence worth printing, and guessing the deployment wrong turns an
    actionable message back into silence.

    `redact` is a secret this request sent. A server that echoes it back must
    not get it onto the terminal, so it is removed from both fields here, before
    `_printable` truncates: cutting first would let a token straddling the cap
    survive as an unmatched prefix.
    """
    try:
        body = resp.json()
    except ValueError:
        return None, None
    if not isinstance(body, dict):
        return None, None
    inner = body.get("error")
    if isinstance(inner, dict):
        body = inner
    error = body.get("error")
    description = body.get("error_description") or body.get("message")
    if redact:
        if isinstance(error, str):
            error = error.replace(redact, "[redacted]")
        if isinstance(description, str):
            description = description.replace(redact, "[redacted]")
    return (
        error if isinstance(error, str) else None,
        _printable(description) if isinstance(description, str) else None,
    )


def _printable(text: str, limit: int = 300) -> str | None:
    """Server text, made safe to print to a terminal.

    The body is remote input. Collapsing whitespace drops embedded newlines and
    carriage returns, which otherwise let a response forge extra lines of CLI
    output, and the cap stops a long body burying the rest of the message.
    """
    cleaned = " ".join(text.split())
    if not cleaned:
        return None
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def new_pkce() -> tuple[str, str]:
    """A fresh (verifier, challenge) pair, S256.

    64 random bytes base64url-encoded lands inside RFC 7636's 43-128 range and
    leaves no padding to strip later.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


def discover(base_url: str, client: httpx.Client) -> dict:
    url = base_url.rstrip("/") + WELL_KNOWN
    try:
        resp = client.get(url, timeout=15)
    except httpx.HTTPError as exc:
        raise OAuthError(f"Could not reach {url}: {exc}") from exc
    if resp.status_code != 200:
        raise OAuthError(f"{url} returned {resp.status_code}, expected 200.")
    try:
        meta = resp.json()
    except ValueError as exc:
        raise OAuthError(f"{url} returned something that is not JSON.") from exc
    if not isinstance(meta, dict):
        raise OAuthError(f"{url} returned JSON that is not an object.")

    # The issuer must be the host we chose to talk to. If a compromised or
    # misconfigured deployment can name a different issuer here, it chooses
    # where the browser goes and who mints the token, and the user sees a
    # plausible consent screen on someone else's domain.
    # Scheme counts as well as host: http://host must not satisfy https://host.
    want = urlsplit(base_url)
    got = urlsplit(meta.get("issuer", ""))
    if (got.scheme, got.netloc) != (want.scheme, want.netloc):
        raise OAuthError(
            f"Authorization server metadata names issuer "
            f"'{got.scheme}://{got.netloc}', but we asked "
            f"'{want.scheme}://{want.netloc}'. Refusing to continue."
        )
    # Every endpoint we will send the browser or a credential to must live on
    # the issuer's origin, so a mismatched document cannot redirect us.
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        value = meta.get(key)
        if not value:
            raise OAuthError(f"Authorization server metadata is missing {key}.")
        ep = urlsplit(value)
        if (ep.scheme, ep.netloc) != (got.scheme, got.netloc):
            raise OAuthError(
                f"Authorization server metadata {key} '{value}' is not on the "
                f"issuer's origin '{got.scheme}://{got.netloc}'. Refusing to continue."
            )
    return meta


def register_client(
    meta: dict, redirect_uri: str, client: httpx.Client, temp_token: str | None = None
) -> str:
    """Register this machine as an OAuth client (RFC 7591) and return its id.

    Registration is open and unauthenticated by design, so there is nothing to
    present here. A new id per login is fine and is what keeps the loopback
    port, which changes every run, matching the registered redirect_uri.
    """
    payload = {
        "client_name": "Anona CLI",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    if temp_token:
        # In the request body, never the authorize URL: that URL is printed to
        # the terminal and lands in browser history.
        payload["temp_token"] = temp_token
    try:
        resp = client.post(
            meta["registration_endpoint"],
            json=payload,
            timeout=15,
        )
    except httpx.HTTPError as exc:
        raise OAuthError(f"Could not register with the authorization server: {exc}") from exc
    if resp.status_code not in (200, 201):
        error, description = _server_error(resp, redact=temp_token)
        # The server's sentence, not just its number. This is the one call site
        # where the refusal is actionable — "this profile is already claimed,
        # expired or unknown" tells the human what happened; "failed with 400"
        # sends them to a log they do not have.
        suffix = f": {description}" if description else "."
        raise RegistrationRejected(
            f"Client registration failed with {resp.status_code}{suffix}",
            status=resp.status_code,
            error=error,
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise OAuthError("Client registration returned something that is not JSON.") from exc
    if not isinstance(body, dict):
        raise OAuthError("Client registration returned JSON that is not an object.")
    client_id = body.get("client_id")
    if not client_id:
        raise OAuthError("Client registration returned no client_id.")
    return client_id


# The two grants `anona login` asks for. `mcp` mints `mcp_access`,
# which only /mcp accepts; exactly `cli` mints `cli_access`, which only the REST
# data plane accepts, and is granted only to a client whose every redirect URI
# is loopback -- which is what `run_login` registers.
MCP_SCOPE = "mcp"
CLI_SCOPE = "cli"
CLI_ACCESS_TYP = "cli_access"


def authorize_url(
    meta: dict,
    client_id: str,
    redirect_uri: str,
    challenge: str,
    state: str,
    scope: str = MCP_SCOPE,
) -> str:
    methods = meta.get("code_challenge_methods_supported") or []
    # A string would turn the membership test into a substring test.
    if not isinstance(methods, list) or "S256" not in methods:
        # Never fall back to "plain": the verifier would add nothing, and the
        # code could be redeemed by anyone who intercepted it.
        raise OAuthError(
            "The authorization server does not advertise S256 PKCE. Refusing to continue."
        )
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "scope": scope,
        }
    )
    return f"{meta['authorization_endpoint']}?{query}"


def scope_refusal(url: str, client: httpx.Client) -> str | None:
    """The server's sentence when it refuses this authorize request's scope.

    `/oauth/authorize` answers a scope it will not grant with `400
    invalid_scope` -- to the BROWSER, which shows a JSON error page and never
    comes back to the loopback listener, so the CLI would sit out its whole
    five-minute wait for nothing. Asking once first, without following the
    redirect, turns that into an immediate message. The request it parks
    expires on its own in minutes and grants nothing.

    None for anything but that refusal, including a network failure: those
    are left for the browser to show, exactly as before this check existed.
    """
    try:
        resp = client.get(url, follow_redirects=False, timeout=15)
    except httpx.HTTPError:
        return None
    if resp.status_code != 400:
        return None
    error, description = _server_error(resp)
    if error != "invalid_scope":
        return None
    return description or "the server does not grant that scope."


def token_typ(access_token: str) -> str | None:
    """The `typ` claim of a JWT access token, read WITHOUT verifying it.

    Not a security check -- the server verifies every token it is handed --
    but the only way to tell which grant a deployment actually minted. One
    that predates command-line tokens accepts `scope=cli` and mints an MCP
    token anyway, which the data plane then refuses on every call; storing it
    would leave the CLI signed in to nothing. None when the token is not a
    JWT or carries no string `typ`.
    """
    parts = access_token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, TypeError):
        return None
    typ = claims.get("typ") if isinstance(claims, dict) else None
    return typ if isinstance(typ, str) else None


def revoke(meta: dict, refresh_token: str, client: httpx.Client) -> bool:
    """Revoke a refresh token at the server (RFC 7009). True when it answered 200.

    Best effort by design: the caller deletes the local file whatever this
    returns, because a logout must work offline. The endpoint must live on the
    issuer's origin like every other one `discover` checks, or the token would
    be posted to wherever a metadata document pointed.
    """
    endpoint = meta.get("revocation_endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        return False
    ep = urlsplit(endpoint)
    issuer = urlsplit(meta.get("issuer", ""))
    if (ep.scheme, ep.netloc) != (issuer.scheme, issuer.netloc):
        return False
    try:
        resp = client.post(endpoint, data={"token": refresh_token}, timeout=15)
    except httpx.HTTPError:
        return False
    return resp.status_code == 200
