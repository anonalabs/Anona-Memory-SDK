from __future__ import annotations

import asyncio
import random
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx


@dataclass
class RetrieveWithReceipt:
    """What :meth:`AnonaClient.retrieve_receipt` returns.

    ``memories`` is exactly what :meth:`AnonaClient.retrieve` would have
    returned for the same arguments, so the two are interchangeable at the call
    site; this variant exists only because ``retrieve`` returns a bare list and
    has nowhere to put the receipt id.

    ``receipt_id`` is also the ``X-Request-ID`` of that call, so a receipt can
    be fetched later from a request id you logged yourself, without having used
    this method at all.
    """

    memories: list[dict] = field(default_factory=list)
    receipt_id: str | None = None

    def __iter__(self):
        """Iterate the memories, so this can stand in for a plain result list."""
        return iter(self.memories)

    def __len__(self) -> int:
        return len(self.memories)

    def __getitem__(self, index):
        return self.memories[index]


def _seg(value: str) -> str:
    """Percent-encode one path segment.

    A ``space_id`` is whatever the customer typed as the space name, so it can
    legally contain characters that are structural in a URL. httpx encodes
    spaces and non-ASCII on the wire, but "/", "?" and "#" are left alone
    because they are valid URL syntax — which meant a space named ``a/b``
    addressed ``/v1/spaces/a/b/graph`` (a different route), and ``x?y`` or
    ``a#b`` truncated the path and moved the remainder into a query string or
    fragment. Every path-based method was affected, ``delete_space`` included,
    so such a space could be created and then never reached again.

    ``safe=""`` keeps those characters inside the segment they were written
    into. The API applies the same encoding on its own hop to the engine.
    """
    return quote(str(value), safe="")


class AnonaError(Exception):
    """An error returned by the Anona API.

    The API wraps failures as ``{"error": {"code", "message"}}`` — not
    FastAPI's ``{"detail": ...}`` — so ``code`` is the stable machine-readable
    handle and ``message`` the human one. Both are lifted onto the exception
    here: ``detail`` alone kept the only useful thing (the code) buried inside a
    dict every caller had to re-parse, and a caller that wants to branch on
    "was this ``space_not_found`` or ``space_ambiguous``" should not be writing
    that dict walk itself.

    ``request_id`` is the ``X-Request-ID`` of the failing call, which is what
    support needs to find it in the logs. A 503 carrying no ``request_id`` may
    be a Cloudflare-mangled 502 or 504: the edge strips the body of those two
    statuses, so the API rewrites them to 503 before they leave. Report such a
    failure with a timestamp rather than treating it as a malformed response.

    ``retry_after`` is populated on a 429 from the ``Retry-After`` header (or
    the error body's own ``retry_after`` / ``window_seconds``), in seconds.

    The ``(status_code, detail)`` positional signature is unchanged, so
    ``except AnonaError as e: e.status_code`` keeps working.
    """

    def __init__(
        self,
        status_code: int,
        detail,
        *,
        code: str | None = None,
        request_id: str | None = None,
        retry_after: int | None = None,
    ):
        self.status_code = status_code
        self.detail = detail
        self.code = code
        self.request_id = request_id
        self.retry_after = retry_after
        super().__init__(f"Anona API error {status_code}: {detail}")


@dataclass
class RateLimit:
    """The budget the API reported on the most recent response.

    Every metered response carries ``X-RateLimit-*`` and
    ``X-Credits-Remaining``. They were parsed by nobody, so a caller had no way
    to pace itself and could only discover the ceiling by hitting it. This is
    updated in place on every call — read :attr:`AnonaClient.rate_limit` after
    one to decide whether to slow down before the next.

    Every field is ``None`` until a metered response has been seen (the
    unmetered routes — spaces, settings, webhooks — do not report a budget).
    """

    limit: int | None = None
    remaining: int | None = None
    window_seconds: int | None = None
    credits_remaining: int | None = None
    retry_after: int | None = None


def _translate_transport_error(
    exc: httpx.TransportError, request: httpx.Request
) -> AnonaError:
    """Turn an httpx transport failure into the SDK's own error type.

    The SDK's contract is that every call either returns or raises
    :class:`AnonaError` — a caller catches one type. A read timeout or a dropped
    connection escaped that contract as a raw ``httpx.ReadTimeout`` /
    ``httpx.ConnectError``, which a caller guarding on ``AnonaError`` would not
    catch. ``reason()`` made this concrete: a loaded synthesis can run well past
    a short client timeout while the server is still working, so it surfaced as
    an unhandled httpx exception rather than a clean SDK error. Timeouts map to
    408 and every other transport failure to 503 — informational
    ``status_code``s, since there was no HTTP response.

    Retrying is handled separately (see :class:`_RetryPolicy`); a timeout is
    deliberately *not* retried, because the server may still be working on the
    request that timed out.
    """
    if isinstance(exc, httpx.TimeoutException):
        return AnonaError(
            408,
            f"Request to {request.url} timed out. If this operation is expected "
            f"to run long (e.g. reason on a large space), pass a larger "
            f"timeout= to AnonaClient. ({exc!r})",
        )
    return AnonaError(503, f"Could not reach the Anona API at {request.url}: {exc!r}")


# Methods that can be re-sent without changing the result if the first attempt
# did land. A 5xx is only retried for these, because a 5xx means the request may
# well have executed and a blind re-send of `POST /v1/record` double-writes.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


class _RetryPolicy:
    """Decides whether to re-send a request, and how long to wait first.

    A 429 is retried for **every** method, POST included: the limiter refuses
    the request before the route runs, so nothing was executed and re-sending
    cannot double-write. A 5xx is retried only for :data:`_IDEMPOTENT_METHODS`.

    The wait comes from the server whenever the server said anything —
    ``Retry-After``, else ``retry_after`` or ``window_seconds`` in the error
    body. This matters more than it looks: the rate-limit window is a whole
    minute, so an SDK that backs off on its own schedule tops out in the low
    seconds, spends its retries landing on the same full bucket (each attempt
    counted against it), and then fails anyway. Waiting the seconds the server
    named is the only backoff that can succeed, which is why
    :attr:`max_wait` defaults high enough to actually sit out a window. A
    caller who would rather fail fast passes ``max_retries=0``.

    A blind backoff — no server hint, i.e. a 5xx — stays small and jittered.
    """

    def __init__(self, max_retries: int, max_wait: float):
        self.max_retries = max_retries
        self.max_wait = max_wait

    def should_retry(self, request: httpx.Request, status: int, attempt: int) -> bool:
        if attempt >= self.max_retries:
            return False
        # A multipart body is generated from the caller's own file objects, and
        # those are not rewound between attempts — re-sending would upload a
        # truncated file. Uploads therefore surface the 429 instead.
        if request.headers.get("content-type", "").startswith("multipart/"):
            return False
        if status == 429:
            return True
        return status >= 500 and request.method in _IDEMPOTENT_METHODS

    def wait_for(self, response: httpx.Response, attempt: int) -> float:
        hinted = _server_retry_hint(response)
        if hinted is not None:
            return min(float(hinted), self.max_wait)
        return min(0.5 * (2**attempt) + random.uniform(0, 0.25), self.max_wait)


def _server_retry_hint(response: httpx.Response) -> int | None:
    """Seconds the server asked us to wait, or None if it did not say.

    Reads ``Retry-After`` first. Falls back to the error body, because the
    deployed API carried ``window_seconds`` in the 429 payload before it grew
    the header — an SDK that only understood the header would have ignored the
    one number the server was already sending.
    """
    header = response.headers.get("retry-after")
    if header:
        try:
            return max(0, int(float(header)))
        except ValueError:
            # An HTTP-date form is legal but the API never emits one; treating
            # it as "no hint" is better than parsing dates on the error path.
            return None
    try:
        body = response.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    for key in ("retry_after", "window_seconds"):
        value = error.get(key)
        if isinstance(value, (int, float)):
            return max(0, int(value))
    return None


def _observe_rate_limit(rate_limit: RateLimit, response: httpx.Response) -> None:
    """Copy this response's budget headers onto the client's snapshot."""

    def _int(name: str) -> int | None:
        raw = response.headers.get(name)
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    limit = _int("x-ratelimit-limit")
    if limit is not None:
        rate_limit.limit = limit
        rate_limit.remaining = _int("x-ratelimit-remaining")
        rate_limit.window_seconds = _int("x-ratelimit-window")
    credits = _int("x-credits-remaining")
    if credits is not None:
        rate_limit.credits_remaining = credits
    rate_limit.retry_after = (
        _server_retry_hint(response) if response.status_code == 429 else None
    )


class _ErrorTranslatingTransport(httpx.BaseTransport):
    """Sync transport wrapper: translates transport errors and retries.

    Retrying here rather than at each call site means every method — including
    any added later — gets the same policy, and neither the sync nor the async
    surface can drift from the other.
    """

    def __init__(
        self,
        inner: httpx.BaseTransport,
        policy: _RetryPolicy | None = None,
        rate_limit: RateLimit | None = None,
    ):
        self._inner = inner
        # A transport built by hand — a test injecting a mock, mostly — gets no
        # retrying, which is what wrapping a transport meant before retries
        # existed. The client always passes its own policy.
        self._policy = policy or _RetryPolicy(0, 0.0)
        self._rate_limit = rate_limit if rate_limit is not None else RateLimit()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        attempt = 0
        while True:
            try:
                response = self._inner.handle_request(request)
            except httpx.TransportError as exc:
                raise _translate_transport_error(exc, request) from exc
            # The body has to be in hand to read the retry hint out of it, and
            # reading it here is harmless: httpx serves an already-read response
            # from `.content` when the client reads it again.
            response.read()
            _observe_rate_limit(self._rate_limit, response)
            if not self._policy.should_retry(request, response.status_code, attempt):
                return response
            delay = self._policy.wait_for(response, attempt)
            response.close()
            time.sleep(delay)
            attempt += 1

    def close(self) -> None:
        self._inner.close()


class _AsyncErrorTranslatingTransport(httpx.AsyncBaseTransport):
    """Async twin of :class:`_ErrorTranslatingTransport`."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        policy: _RetryPolicy | None = None,
        rate_limit: RateLimit | None = None,
    ):
        self._inner = inner
        self._policy = policy or _RetryPolicy(0, 0.0)
        self._rate_limit = rate_limit if rate_limit is not None else RateLimit()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        attempt = 0
        while True:
            try:
                response = await self._inner.handle_async_request(request)
            except httpx.TransportError as exc:
                raise _translate_transport_error(exc, request) from exc
            await response.aread()
            _observe_rate_limit(self._rate_limit, response)
            if not self._policy.should_retry(request, response.status_code, attempt):
                return response
            delay = self._policy.wait_for(response, attempt)
            await response.aclose()
            await asyncio.sleep(delay)
            attempt += 1

    async def aclose(self) -> None:
        await self._inner.aclose()


def _compact(pairs) -> dict:
    """Drop unset entries, keeping ``False`` and ``0``.

    The older inline ``if value:`` filter silently dropped every falsey value,
    which is fine for ids and timestamps and wrong for the flags and numbers
    added since — ``prefer_observations=False`` is the whole point of passing
    it, and it would never have reached the wire.
    """
    return {key: value for key, value in pairs if value is not None}


def _search_extras(
    *,
    top_k: int | None = None,
    memory_type: list[str] | None = None,
    tags: list[str] | None = None,
    tags_match: str | None = None,
    tag_groups: list[dict] | None = None,
    prefer_observations: bool | None = None,
    min_score: float | None = None,
    member_id: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
    as_of: str | None = None,
    query_timestamp: str | None = None,
    occurred_after: str | None = None,
    occurred_before: str | None = None,
) -> dict:
    """Every optional `/v1/retrieve` field, with the unset ones dropped.

    ``retrieve``, ``retrieve_receipt`` and ``get_context`` are one endpoint in
    three renderings, and each kept its own copy of this construction — six
    copies counting the async twins. That is how the set of arguments each one
    accepted came to differ, so they share one builder now: an argument added
    here reaches all six at once.

    ``tag_groups`` is passed through unmodelled: the API owns every rule in
    it — the reserved-prefix check at each nesting level, how scope composes
    with it, the size and depth caps — and a second copy of those rules here
    could only drift from the first. Each group is a leaf
    ``{"tags": [...], "match": ...}`` or one of ``{"and": [...]}``,
    ``{"or": [...]}``, ``{"not": {...}}``; groups in the list are AND-ed.
    """
    return _compact(
        (
            ("top_k", top_k),
            ("memory_type", memory_type),
            ("tags", tags),
            ("tags_match", tags_match),
            ("tag_groups", tag_groups),
            ("prefer_observations", prefer_observations),
            ("min_score", min_score),
            ("member_id", member_id),
            ("user_id", user_id),
            ("agent_id", agent_id),
            ("session_id", session_id),
            ("as_of", as_of),
            ("query_timestamp", query_timestamp),
            ("occurred_after", occurred_after),
            ("occurred_before", occurred_before),
        )
    )


def _search_body(space_id: str, query: str, *, limit: int, mode: str, **extras) -> dict:
    """The `/v1/retrieve` payload: the four always-sent fields, plus extras."""
    body: dict = {"space_id": space_id, "query": query, "limit": limit, "mode": mode}
    body.update(_search_extras(**extras))
    return body


def _record_body(
    space_id: str,
    content: str,
    *,
    context: str | None,
    metadata: dict | None,
    tags: list[str] | None,
    background: bool,
    user_id: str | None,
    agent_id: str | None,
    session_id: str | None,
    timestamp: str | None,
) -> dict:
    """The `/v1/record` payload, shared by the sync and async writers."""
    body: dict = {
        "space_id": space_id,
        "content": content,
        "metadata": metadata or {},
    }
    body.update(
        _compact(
            (
                ("context", context),
                ("tags", tags or None),
                ("user_id", user_id),
                ("agent_id", agent_id),
                ("session_id", session_id),
                ("timestamp", timestamp),
            )
        )
    )
    if background:
        body["async"] = True
    return body


class AnonaClient:
    """Synchronous and async client for Anona Memory API."""

    # api.anonalabs.com routes straight to the API, without the hop through the
    # dashboard edge worker that memory.anonalabs.com takes.
    # memory.anonalabs.com keeps working indefinitely — existing code needs no
    # change, and callers can still override base_url.
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anonalabs.com",
        timeout: float = 120.0,
        max_retries: int = 2,
        retry_max_wait: float = 60.0,
    ):
        # `max_retries` covers 429 on every method and 5xx on idempotent ones
        # only; see _RetryPolicy for why those are different rules. Pass 0 to
        # turn retrying off entirely. `retry_max_wait` caps how long a single
        # wait may be — it is deliberately as long as a rate-limit window,
        # because a shorter cap turns "wait and succeed" into "give up early",
        # and the SDK only ever waits that long when the server itself named
        # the number.
        # Default of 120s, not httpx's 5s and not the old hard-coded 30s: a
        # loaded ``reason()`` legitimately runs 70-115s server-side (the API
        # itself waits ~90s), so a 30s client timeout killed the call while the
        # engine was still producing the answer. 120s comfortably outlives the
        # server's own budget; a caller who wants normal calls to fail faster
        # can lower it. It is a ceiling, not a fixed wait — every call returns
        # the instant the server responds.
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._retry_policy = _RetryPolicy(max_retries, retry_max_wait)
        #: Budget reported by the most recent response. See :class:`RateLimit`.
        self.rate_limit = RateLimit()
        # Created lazily (first sync/async call) rather than both up front —
        # a caller that only ever uses one side previously still opened (and
        # leaked) the other's connection pool, since close()/aclose() each
        # only tear down their own half.
        self._client: httpx.Client | None = None
        # Manual override for _get_async_client() below, checked before it
        # ever looks at _async_clients. AnonaClient itself never sets this —
        # only a caller that wants to pin one specific client instance does
        # (chiefly tests, to inject a mocked transport), and taking that on
        # means taking on the responsibility of only ever driving it from a
        # single event loop, same as before this class supported more than
        # one loop at all.
        self._async_client: httpx.AsyncClient | None = None
        # One httpx.AsyncClient per event loop that has called
        # _get_async_client() — see that method's docstring for why a
        # single client shared across loops is unsafe. A plain dict, not a
        # WeakKeyDictionary: an AsyncClient's own internals may hold the
        # loop alive indirectly, which would silently defeat GC-based
        # eviction, so dead entries are instead swept explicitly (see
        # below) rather than left to collection timing. Guarded by a lock
        # because this SDK is driven from thread pools by some adapters —
        # a different OS thread means a different running loop, hence a
        # different dict key potentially being inserted at the same time.
        self._async_clients: dict[asyncio.AbstractEventLoop, httpx.AsyncClient] = {}
        self._async_clients_lock = threading.Lock()

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
                transport=_ErrorTranslatingTransport(
                    httpx.HTTPTransport(), self._retry_policy, self.rate_limit
                ),
            )
        return self._client

    def _get_async_client(self) -> httpx.AsyncClient:
        """The async httpx client for the CURRENTLY RUNNING event loop.

        A single ``httpx.AsyncClient`` reused for this object's whole life
        (the previous implementation) is unsafe: its pooled keep-alive
        connection, and the anyio locks httpcore's pool guards it with, end
        up bound to whichever event loop was running on the *first* call
        that used it. A host that creates a fresh loop per call —
        ``asyncio.run()`` once per turn is the ordinary shape for a CLI, a
        synchronous Flask/Django view, or a Celery/RQ worker driving one of
        the async framework adapters — hands the *second* call's new loop a
        connection pool built for a now-dead one. Deterministically, not a
        race: ``RuntimeError: Event loop is closed``, or "bound to a
        different event loop" if the first loop is still alive elsewhere
        (e.g. another thread). ``MemoryBridge``'s blanket ``except
        Exception`` swallows that into a fail-open ``""``, so the caller
        sees "no memories" on a retrieve/record the API already
        executed — and billed.

        This method keys a client per loop instead: each event loop gets
        its own pool, so there is never a stale connection for a *different*
        loop to inherit. Measured clean across every reported shape —
        repeated ``asyncio.run()`` on one client, real async adapters driven
        one-loop-per-turn, gaps past httpx's keepalive_expiry between calls,
        and concurrent threads each running their own loop.

        Rejected: a single client with ``max_keepalive_connections=0`` (no
        idle connection ever survives to be reused across loops). Simpler,
        and it does pass the sequential shapes above — but it still shares
        *one* ``httpx.AsyncClient``, hence one ``httpcore.AsyncConnectionPool``,
        across every loop that ever calls it, and that pool's own
        bookkeeping lock (``httpcore``'s ``AsyncThreadLock``) is a
        documented no-op in async mode: httpcore assumes an ``AsyncClient``
        is only ever driven from one loop/thread at a time. Confirmed
        directly: several OS threads (each its own event loop — the shape a
        thread-pool-driven caller produces) hitting one
        ``max_keepalive_connections=0`` client concurrently corrupts the
        pool's internal connection-accounting list under real load
        (``ValueError: list.remove(x): x not in list``, plus read errors) —
        worse than the bug being fixed. Keying by loop sidesteps this
        instead of racing to avoid it: each thread's own loop gets its own
        pool, so there is no shared pool state for two threads to corrupt in
        the first place.

        Entries are swept for closed loops on every call rather than left
        to accumulate: ``asyncio.run()`` always closes its loop before
        returning, so a "new loop per call" caller would otherwise leak one
        dict entry — and one unreachable-but-still-referenced
        ``AsyncClient`` — per call, forever. A swept entry's connection(s)
        are simply dropped for ordinary garbage collection to reclaim, not
        explicitly closed: there is no way to cleanly ``aclose()`` a client
        after its owning loop is already gone (see :meth:`aclose`, which
        has the same limitation for exactly the same reason). That is an
        accepted trade-off already established in this SDK, not a new one.

        No handling for "called with no running event loop": every caller
        of this method is itself a coroutine's own body (the ``async_*``
        methods below), which can only be executing — and therefore only
        reach this line — while some loop is actively driving it. Letting
        ``asyncio.get_running_loop()``'s ``RuntimeError`` surface in the
        (unreachable in practice) case where that invariant is somehow
        violated is preferable to masking it.
        """
        if self._async_client is not None:
            return self._async_client
        loop = asyncio.get_running_loop()
        with self._async_clients_lock:
            for dead_loop in [lp for lp in self._async_clients if lp.is_closed()]:
                del self._async_clients[dead_loop]
            client = self._async_clients.get(loop)
            if client is None:
                client = httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=self._timeout,
                    transport=_AsyncErrorTranslatingTransport(
                        httpx.AsyncHTTPTransport(),
                        self._retry_policy,
                        self.rate_limit,
                    ),
                )
                self._async_clients[loop] = client
            return client

    def _raise(self, resp: httpx.Response) -> None:
        if resp.is_success:
            return
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        code = None
        if isinstance(detail, dict):
            error = detail.get("error")
            if isinstance(error, dict):
                code = error.get("code")
        raise AnonaError(
            resp.status_code,
            detail,
            code=code,
            # Header first: it is present even on the responses whose body the
            # edge strips (502/504 arrive as a bodiless 503), which are exactly
            # the failures someone will need an id for.
            request_id=resp.headers.get("x-request-id"),
            retry_after=(
                _server_retry_hint(resp) if resp.status_code == 429 else None
            ),
        )

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        expect_no_content: bool = False,
    ):
        """Issue one request against the API and return its decoded body.

        Every method added from here on goes through this rather than repeating
        the four lines of build-URL / send / raise / decode, which is how the
        sync and async halves of this class drifted apart in the first place.
        A 204 decodes to ``None`` — `resp.json()` on an empty body raises.
        """
        resp = self._get_client().request(
            method, f"{self._base_url}{path}", params=params, json=json
        )
        self._raise(resp)
        if expect_no_content or resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    async def _acall(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        expect_no_content: bool = False,
    ):
        """Async twin of :meth:`_call`."""
        resp = await self._get_async_client().request(
            method, f"{self._base_url}{path}", params=params, json=json
        )
        self._raise(resp)
        if expect_no_content or resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # ── Sync ──────────────────────────────────────────────────────────────────

    def record(
        self,
        space_id: str,
        content: str,
        metadata: dict | None = None,
        tags: list[str] | None = None,
        background: bool = False,
        user_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        timestamp: str | None = None,
        context: str | None = None,
    ) -> dict:
        """Store a memory.

        ``context`` is extra framing stored alongside the content — where the
        content came from, who said it, what the surrounding conversation was.
        It is stored and returned with the memory but is not the memory's text.

        ``user_id`` / ``agent_id`` / ``session_id`` scope the memory inside the
        space: a memory written under a user is only returned to a
        :meth:`retrieve` carrying the same user. That is how one space serves
        many end users without their memories mixing.

        ``tags`` attaches visibility-scope tags that :meth:`retrieve` can filter
        on (e.g. tag by the source agent in agent-to-agent workflows).

        ``timestamp`` (ISO 8601) is when the *event* happened, not when you are
        recording it — use it when importing history so a memory about last June
        is dated last June. It is returned as ``timestamp`` and feeds recency
        ranking and the ``occurred_after`` / ``occurred_before`` event-time
        filter (``occurred_start`` / ``occurred_end`` fill only from a date the
        memory's own text names). It does **not** change when the memory was
        recorded, so it has no effect on :meth:`retrieve`'s ``as_of``.
        Defaults to now.

        With ``background=True`` the write is queued and returns immediately with
        a ``job_id`` (``status="processing"``) instead of the stored
        ``memory_id`` — poll it with :meth:`get_job`. Use this in latency-
        sensitive paths so the call never blocks on fact extraction.
        """
        body = _record_body(
            space_id,
            content,
            context=context,
            metadata=metadata,
            tags=tags,
            background=background,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            timestamp=timestamp,
        )
        resp = self._get_client().post(f"{self._base_url}/v1/record", json=body)
        self._raise(resp)
        return resp.json()

    def record_batch(self, space_id: str, items: list[dict]) -> dict:
        """Bulk-ingest up to 100 memories in one call (always queued).

        Each item is a dict with ``content`` (required) and optional ``context``,
        ``timestamp``, ``metadata``, and ``tags`` (a list of strings, filterable
        by :meth:`retrieve`). Returns a ``job_id`` — poll :meth:`get_job`.
        """
        resp = self._get_client().post(
            f"{self._base_url}/v1/record/batch",
            json={"space_id": space_id, "items": items},
        )
        self._raise(resp)
        return resp.json()

    def get_job(self, space_id: str, job_id: str) -> dict:
        """Status of a queued ingestion job from ``record(background=True)`` or
        :meth:`record_batch`. Free — does not consume credits.

        Returns ``{"job_id", "status", "created_at", "completed_at", "error"}``;
        ``status`` is one of pending / processing / completed / failed /
        cancelled / not_found.
        """
        resp = self._get_client().get(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/jobs/{_seg(job_id)}"
        )
        self._raise(resp)
        return resp.json()

    def retrieve(
        self,
        space_id: str,
        query: str,
        limit: int = 10,
        mode: str = "accurate",
        user_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        as_of: str | None = None,
        query_timestamp: str | None = None,
        occurred_after: str | None = None,
        occurred_before: str | None = None,
        top_k: int | None = None,
        memory_type: list[str] | None = None,
        tags: list[str] | None = None,
        tags_match: str | None = None,
        tag_groups: list[dict] | None = None,
        prefer_observations: bool | None = None,
        min_score: float | None = None,
        member_id: str | None = None,
    ) -> list[dict]:
        """Search memories.

        ``user_id`` / ``agent_id`` / ``session_id`` restrict the search to
        memories written under the same scope. The filter is strict: memories
        stored without a scope are not returned to a scoped search.

        ``mode="accurate"`` (default) neurally reranks results for best
        relevance. ``mode="fast"`` skips that pass — much lower latency, at
        some cost to relevance quality.

        ``tags`` filters on the visibility-scope tags a memory was written
        with, and ``tags_match`` chooses how ("any", "all", "any_strict",
        "all_strict", "exact"). The strict forms also exclude *untagged*
        memories, which the others treat as matching anything.

        ``memory_type`` narrows to particular kinds of memory, ``min_score``
        drops results below a relevance floor, and ``top_k`` bounds how many
        candidates are considered before ranking (``limit`` bounds how many
        come back).

        ``prefer_observations`` defaults to true server-side, collapsing the raw
        evidence behind a synthesized memory so one piece of knowledge appears
        once. Pass ``False`` to also receive the underlying facts.

        ``member_id`` returns only what that member wrote; pass ``"me"`` for
        your own. Attribution exists only in spaces shared across organizations.

        ``as_of`` (ISO 8601) restricts the search to memories recorded at or
        before that instant, so the answer is what the space knew then rather
        than what it knows now. Filters on when a memory was *recorded*, not on
        when the event it describes happened — a backdated import is recorded
        today no matter what ``timestamp`` it carries.

        ``query_timestamp`` (ISO 8601) moves the "now" that recency scoring and
        relative dates in the query ("last June") are measured against. It only
        re-ranks; it never removes a result, so a memory recorded after that
        instant can still come back. Use ``as_of`` when you need the cutoff
        enforced.

        ``occurred_after`` / ``occurred_before`` (ISO 8601) bound when the thing
        *happened*, which is what ``as_of`` cannot address: history imported
        today all shares one record time and spans years of event time. Either
        bound alone is an open-ended window, and the test is an overlap, so an
        event straddling an edge is inside.

        A memory is matched on the event window its own text described, falling
        back to the ``timestamp`` it was recorded with. That fallback carries
        most of the work: a memory whose text named no date has no event window
        of its own, and is still filtered correctly.
        """
        body = _search_body(
            space_id,
            query,
            limit=limit,
            mode=mode,
            top_k=top_k,
            memory_type=memory_type,
            tags=tags,
            tags_match=tags_match,
            tag_groups=tag_groups,
            prefer_observations=prefer_observations,
            min_score=min_score,
            member_id=member_id,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            as_of=as_of,
            query_timestamp=query_timestamp,
            occurred_after=occurred_after,
            occurred_before=occurred_before,
        )
        resp = self._get_client().post(
            f"{self._base_url}/v1/retrieve",
            json=body,
        )
        self._raise(resp)
        return resp.json().get("results", [])

    def retrieve_receipt(
        self,
        space_id: str,
        query: str,
        limit: int = 10,
        mode: str = "accurate",
        receipt_detail: str = "basic",
        user_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        as_of: str | None = None,
        query_timestamp: str | None = None,
        occurred_after: str | None = None,
        occurred_before: str | None = None,
        top_k: int | None = None,
        memory_type: list[str] | None = None,
        tags: list[str] | None = None,
        tags_match: str | None = None,
        tag_groups: list[dict] | None = None,
        prefer_observations: bool | None = None,
        min_score: float | None = None,
        member_id: str | None = None,
    ) -> RetrieveWithReceipt:
        """:meth:`retrieve`, plus the id of the receipt for that search.

        Same search, same results, same arguments. The only difference is the
        return type: :meth:`retrieve` hands back a bare list, which has nowhere
        to carry the receipt id, so this returns both. The result iterates and
        indexes like the list does, so swapping one for the other rarely means
        changing the code that consumes it.

        ``receipt_detail="full"`` additionally asks the search pipeline to
        account for its own cuts, so the receipt explains memories that were
        ranked and dropped before ``limit`` or a relevance floor ever applied.
        It costs some latency on the first call for a given query, because
        those decisions are not part of a cached answer. Leave it ``"basic"``
        for production traffic.

        Pass the ``receipt_id`` to :meth:`get_receipt`, or to :meth:`explain`
        with a memory id to ask about one memory in particular.
        """
        body: dict = {
            "space_id": space_id,
            "query": query,
            "limit": limit,
            "mode": mode,
            "receipt": True,
        }
        if receipt_detail != "basic":
            body["receipt_detail"] = receipt_detail
        body.update(
            _search_extras(
                top_k=top_k,
                memory_type=memory_type,
                tags=tags,
                tags_match=tags_match,
                tag_groups=tag_groups,
                prefer_observations=prefer_observations,
                min_score=min_score,
                member_id=member_id,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                as_of=as_of,
                query_timestamp=query_timestamp,
                occurred_after=occurred_after,
                occurred_before=occurred_before,
            )
        )
        resp = self._get_client().post(
            f"{self._base_url}/v1/retrieve",
            json=body,
        )
        self._raise(resp)
        data = resp.json()
        return RetrieveWithReceipt(
            memories=data.get("results", []),
            # Fall back to the header: the id is the request id either way, and
            # a receipt that could not be stored still leaves the call itself
            # perfectly valid. A receipt is a debugging aid, never load-bearing.
            receipt_id=data.get("receipt_id") or resp.headers.get("x-request-id"),
        )

    def get_receipt(self, request_id: str) -> dict:
        """The manifest for one earlier search: what was returned, what was
        cut, and why.

        ``request_id`` is what :meth:`retrieve_receipt` returned, or the
        ``X-Request-ID`` header of any earlier call, which is the same value.
        Every search builds a receipt whether or not anyone asked for one, so
        this works on a call you never flagged in advance.

        Receipts expire (about an hour), and a missing, expired or foreign one
        is an ordinary 404. Free: it returns something already computed and
        already paid for.
        """
        resp = self._get_client().get(
            f"{self._base_url}/v1/receipts/{_seg(request_id)}"
        )
        self._raise(resp)
        return resp.json()

    def explain(self, request_id: str, memory_id: str) -> dict:
        """Account for one specific memory against an earlier search.

        The receipt says what was cut. This answers the question it cannot:
        why *this* memory is not in your results, including the case where no
        stage of the search mentions it at all. That case comes back as
        ``outcome: "not_retrieved"``, and it is the useful one: nothing matched
        the memory, so raising ``limit`` or lowering a relevance floor will not
        help, and the wording or the scope is what to look at.

        ``arms`` reports how each kind of matching ranked it, with ``None`` for
        one that never found it. Found by ``keyword`` but not ``semantic``
        usually means the query shares words with the memory but not meaning.

        The search is replayed, pinned to the instant the original ran, so
        memories written since do not change the answer. Free, but it does run
        a real search, so it counts against your rate limit.
        """
        resp = self._get_client().get(
            f"{self._base_url}/v1/receipts/{_seg(request_id)}/explain",
            params={"memory_id": memory_id},
        )
        self._raise(resp)
        return resp.json()

    def get_context(
        self,
        space_id: str,
        query: str,
        limit: int = 10,
        max_tokens: int | None = None,
        block_order: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        as_of: str | None = None,
        query_timestamp: str | None = None,
        occurred_after: str | None = None,
        occurred_before: str | None = None,
        top_k: int | None = None,
        memory_type: list[str] | None = None,
        tags: list[str] | None = None,
        tags_match: str | None = None,
        tag_groups: list[dict] | None = None,
        prefer_observations: bool | None = None,
        min_score: float | None = None,
        member_id: str | None = None,
    ) -> str:
        """The relevant memories as one prompt-ready string.

        The same search as :meth:`retrieve`, returned already formatted so it
        can go straight into a system prompt — no join to write, and the token
        budget is handled server-side rather than by a loop that does not have
        one. Returns ``""`` when nothing matched.

        ``max_tokens`` caps the block: whole memories are dropped,
        lowest-ranked first, rather than the text being cut mid-sentence.

        The temporal arguments mean exactly what they mean on
        :meth:`retrieve`, because this is that search with the rendering on
        top: ``as_of`` bounds when a memory was *recorded*, ``query_timestamp``
        only re-ranks, and ``occurred_after`` / ``occurred_before`` bound when
        the thing *happened*. A prompt block assembled without them is
        assembled from the whole corpus, which is rarely what a
        point-in-time question wants.
        """
        body: dict = {
            "space_id": space_id,
            "query": query,
            "limit": limit,
            "format": "block",
        }
        if max_tokens:
            body["context_max_tokens"] = max_tokens
        if block_order:
            body["block_order"] = block_order
        body.update(
            _search_extras(
                top_k=top_k,
                memory_type=memory_type,
                tags=tags,
                tags_match=tags_match,
                tag_groups=tag_groups,
                prefer_observations=prefer_observations,
                min_score=min_score,
                member_id=member_id,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                as_of=as_of,
                query_timestamp=query_timestamp,
                occurred_after=occurred_after,
                occurred_before=occurred_before,
            )
        )
        resp = self._get_client().post(f"{self._base_url}/v1/retrieve", json=body)
        self._raise(resp)
        return resp.json().get("context") or ""

    def reason(
        self,
        space_id: str,
        query: str,
        user_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        model: str | None = None,
        tag_groups: list[dict] | None = None,
    ) -> str | None:
        """Synthesize an answer from everything a space knows about a topic.

        ``user_id`` / ``agent_id`` / ``session_id`` narrow the synthesis to one
        scope, exactly as they do on :meth:`retrieve` — omit them to reason over
        the whole space. ``model`` picks the LLM that answers (a tier name such
        as ``"fast"`` / ``"balanced"``, or a model id); omit it to use the
        space's configured default.

        ``tag_groups`` takes the same boolean expression :meth:`retrieve` takes,
        and narrows what the reasoning agent is allowed to look at rather than
        filtering an answer after the fact. It is worth more here than there:
        the predicate rides every iteration of the agent loop, not one query.
        """
        body: dict = {"space_id": space_id, "query": query}
        for key, value in (
            ("user_id", user_id),
            ("agent_id", agent_id),
            ("session_id", session_id),
            ("model", model),
            ("tag_groups", tag_groups),
        ):
            if value:
                body[key] = value
        resp = self._get_client().post(
            f"{self._base_url}/v1/reason",
            json=body,
        )
        self._raise(resp)
        return resp.json().get("insights")

    # ── Per-user profiles ─────────────────────────────────────────────────────

    #: Query parameters for :meth:`get_user_profile`, in the order the API
    #: documents them. Every one has a server-side default, so only what the
    #: caller actually passed is sent — filling in today's defaults would pin a
    #: caller to values that are free to move.
    _PROFILE_PARAMS = ("limit", "offset", "memory_type", "format", "context_max_tokens")

    @staticmethod
    def _profile_params(
        limit: int | None,
        offset: int | None,
        memory_type: str | None,
        format: str | None,
        context_max_tokens: int | None,
    ) -> dict:
        # `is not None`, not truthiness: ``offset=0`` is falsy and is also a
        # perfectly good explicit offset.
        values = (limit, offset, memory_type, format, context_max_tokens)
        return {
            key: value
            for key, value in zip(AnonaClient._PROFILE_PARAMS, values)
            if value is not None
        }

    def get_user_profile(
        self,
        space_id: str,
        user_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
        memory_type: str | None = None,
        format: str | None = None,
        context_max_tokens: int | None = None,
    ) -> dict:
        """Everything a space has learned about one end user.

        ``user_id`` must be the same value your writes are scoped with — this
        reads back the memories tagged with it, so a typo on either side looks
        like an empty profile.

        Returns ``{"space_id", "user_id", "memory_count", "first_seen",
        "last_active", "memories"}``. Pass ``format="block"`` for a
        prompt-ready ``context`` string as well (with ``context_max_tokens`` to
        cap it), rendered exactly as :meth:`get_context` renders a search.

        Two things worth knowing before you build on the result:

        * **An unknown user is not an error.** A ``user_id`` is a scope tag
          created by the first write naming it, not a resource you register, so
          there is no valid set for a typo to fall outside of. A user nobody
          has ever recorded under returns ``memory_count: 0`` and an empty
          ``memories``, never a 404. An unknown *space* is still a 404.
        * **``memory_count`` can go down.** The default view collapses layers:
          several raw facts become one synthesized note, and the note is what
          gets counted. A profile read during an import can genuinely go 115 →
          67 → 15 while the corpus behind it grows the whole time. Treat it as
          "how many distinct things we know about this user" — never as an
          ingestion counter, and never as the basis for a progress bar.
        """
        resp = self._get_client().get(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/users/{_seg(user_id)}/profile",
            params=self._profile_params(
                limit, offset, memory_type, format, context_max_tokens
            ),
        )
        self._raise(resp)
        return resp.json()

    def ask_about_user(
        self,
        space_id: str,
        user_id: str,
        query: str,
        *,
        model: str | None = None,
    ) -> dict:
        """Synthesize an answer about one end user, from their memories only.

        The scope is not a filter that can be relaxed: the answer only ever
        draws on memories written under this ``user_id``.

        ``model`` picks the LLM that answers (a tier name such as ``"fast"`` /
        ``"balanced"``, or a model id) — the same catalog and the same
        request → space default → platform default ladder :meth:`reason` uses.

        Returns the whole response, not just the answer string as
        :meth:`reason` does, because ``model`` reports which LLM *actually*
        answered — which is what the credits on this call are charged at, and
        differs from what you asked for whenever you asked for nothing. Read
        the answer from ``["insights"]``.
        """
        body: dict = {"query": query}
        if model:
            body["model"] = model
        resp = self._get_client().post(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/users/{_seg(user_id)}/ask",
            json=body,
        )
        self._raise(resp)
        return resp.json()

    def list_spaces(self) -> list[dict]:
        resp = self._get_client().get(f"{self._base_url}/v1/spaces/")
        self._raise(resp)
        return resp.json().get("spaces", [])

    def get_graph(self, space_id: str, *, limit: int = 500, min_count: int = 1) -> dict:
        """Entity relationship graph for a space.

        Nodes are entities; an edge means two entities were mentioned together in
        the same memory (weighted). Returns
        ``{"nodes", "edges", "total_entities", "total_edges"}``.
        """
        resp = self._get_client().get(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/graph",
            params={"limit": limit, "min_count": min_count},
        )
        self._raise(resp)
        return resp.json()

    def list_entities(
        self, space_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        """List the entities extracted in a space, most-mentioned first."""
        resp = self._get_client().get(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/entities",
            params={"limit": limit, "offset": offset},
        )
        self._raise(resp)
        return resp.json().get("items", [])

    def get_entity(self, space_id: str, entity_id: str) -> dict:
        """One entity and its observations (what's been learned about it)."""
        resp = self._get_client().get(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/entities/{_seg(entity_id)}",
        )
        self._raise(resp)
        return resp.json()

    # ── Documents (file upload → retrieval) ────────────────────────────────────

    #: Max size of a single uploaded file, mirrored from the API's per-file cap
    #: so the SDK rejects an oversized file locally instead of uploading it only
    #: to get a 413 back.
    MAX_FILE_BYTES = 25 * 1024 * 1024

    @classmethod
    def _upload_parts(
        cls,
        file,
        filename: str | None,
        strategy: str | None,
        tags: list[str] | str | None,
    ) -> tuple[list, dict]:
        """Normalise a path / bytes / file-like into httpx multipart parts."""
        import os

        if isinstance(file, (str, os.PathLike)):
            with open(file, "rb") as fh:
                content = fh.read()
            name = filename or os.path.basename(str(file))
        elif isinstance(file, (bytes, bytearray)):
            content = bytes(file)
            name = filename or "upload"
        else:  # file-like
            content = file.read()
            name = filename or os.path.basename(str(getattr(file, "name", "upload")))

        if len(content) > cls.MAX_FILE_BYTES:
            raise AnonaError(
                413,
                f"'{name}' is {len(content) // (1024 * 1024)} MB, over the "
                f"{cls.MAX_FILE_BYTES // (1024 * 1024)} MB per-file limit.",
            )

        files = [("files", (name, content))]
        data: dict = {}
        if strategy is not None:
            data["strategy"] = strategy
        if tags:
            data["tags"] = ",".join(tags) if isinstance(tags, (list, tuple)) else tags
        return files, data

    def upload_file(
        self,
        space_id: str,
        file,
        *,
        filename: str | None = None,
        strategy: str | None = None,
        tags: list[str] | str | None = None,
    ) -> dict:
        """Upload a file into a space so retrieval can draw on its content (RAG).

        ``file`` may be a path (str / os.PathLike), raw ``bytes``, or a binary
        file-like object. Supported types are PDF, DOCX, DOC, PPTX, PPT, XLSX,
        XLS, HTML, TXT/MD, CSV, JPEG/PNG images, MP3/WAV audio and
        MP4/MOV/WEBM/MKV video. Images, audio and video are read into text.

        Ingestion is asynchronous — returns ``{"job_ids": [...]}``; poll each with
        :meth:`get_job`. By default the file is stored as retrieval chunks; pass a
        ``strategy`` to override, and ``tags`` to scope later retrieval.
        """
        files, data = self._upload_parts(file, filename, strategy, tags)
        resp = self._get_client().post(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/documents", files=files, data=data
        )
        self._raise(resp)
        return resp.json()

    def create_space(self, name: str, description: str | None = None) -> dict:
        """Create a memory space. Returns ``{"space_id", "name", ...}``."""
        resp = self._get_client().post(
            f"{self._base_url}/v1/spaces/",
            json={"name": name, "description": description},
        )
        self._raise(resp)
        return resp.json()

    def list_documents(
        self, space_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        """List the documents uploaded into a space."""
        resp = self._get_client().get(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/documents",
            params={"limit": limit, "offset": offset},
        )
        self._raise(resp)
        return resp.json().get("documents", [])

    def delete_document(self, space_id: str, document_id: str) -> None:
        """Delete a document and the memories extracted from it."""
        resp = self._get_client().delete(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/documents/{_seg(document_id)}"
        )
        self._raise(resp)

    def delete_space(self, space_id: str) -> None:
        """Delete a space and every memory in it. Irreversible."""
        resp = self._get_client().delete(f"{self._base_url}/v1/spaces/{_seg(space_id)}")
        self._raise(resp)

    def delete_memory(self, space_id: str, memory_id: str) -> None:
        """Delete a single memory from a space. Irreversible."""
        resp = self._get_client().delete(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/memories/{_seg(memory_id)}"
        )
        self._raise(resp)

    # ── Space configuration ───────────────────────────────────────────────────
    #
    # Both settings endpoints replace the whole record rather than patching it,
    # so an argument the caller leaves out is sent as an explicit null and the
    # stored value is cleared. Spelling every key out in the body, rather than
    # dropping the unset ones, is what makes that faithful.

    def get_extraction_settings(self, space_id: str) -> dict:
        """How this space turns recorded text into memories.

        Returns ``{"space_id", "mode", "guidance", "custom_prompt", "labels",
        "free_form_entities"}``. A null field is *unset* — it follows the
        platform default and keeps following it, which is not the same as being
        set to that default's current value.
        """
        resp = self._get_client().get(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/extraction-settings"
        )
        self._raise(resp)
        return resp.json()

    def set_extraction_settings(
        self,
        space_id: str,
        *,
        mode: str | None = None,
        guidance: str | None = None,
        custom_prompt: str | None = None,
        labels: list[dict] | None = None,
        free_form_entities: bool | None = None,
    ) -> dict:
        """Replace this space's extraction settings.

        ``mode`` is ``concise`` (the default), ``verbose``, ``verbatim`` or
        ``custom``. ``guidance`` is added to the standard extraction rules in
        every mode — name the terms your team uses and the fields that always
        matter. ``custom_prompt`` replaces those rules instead, and only applies
        while ``mode`` is ``custom``.

        ``labels`` defines dimensions the extractor classifies every memory
        along. Each entry is ``{"key", "type", "description", "tag", "values"}``
        where ``type`` is ``value`` or ``multi-values`` (you list the allowed
        ``values``) or ``text`` or ``multi-text`` (any value the memory
        supplies, one of them or all of them). A group with ``tag`` set is
        written onto the memory as the tag ``"<key>:<value>"`` as well, so
        ``retrieve`` can filter on it through ``tag_groups``::

            client.set_extraction_settings(
                "default",
                labels=[{
                    "key": "name",
                    "type": "multi-text",
                    "tag": True,
                    "description": "Every name this thing is known by, "
                                   "including abbreviations and short forms.",
                }],
            )

        ``free_form_entities=False`` keeps only entities belonging to a label
        group, and needs ``labels`` set — without it the extractor would keep
        no entities at all.

        This replaces the record: anything you leave out is cleared. Settings
        apply to writes made after the call and never re-extract stored
        memories.
        """
        resp = self._get_client().put(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/extraction-settings",
            json={
                "mode": mode,
                "guidance": guidance,
                "custom_prompt": custom_prompt,
                "labels": labels,
                "free_form_entities": free_form_entities,
            },
        )
        self._raise(resp)
        return resp.json()

    def reset_extraction_settings(self, space_id: str) -> None:
        """Drop this space's extraction settings, back to the platform defaults."""
        resp = self._get_client().delete(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/extraction-settings"
        )
        self._raise(resp)

    def get_chat_settings(self, space_id: str) -> dict:
        """This space's defaults for the drop-in LLM proxy endpoints."""
        resp = self._get_client().get(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/chat-settings"
        )
        self._raise(resp)
        return resp.json()

    def set_chat_settings(
        self,
        space_id: str,
        *,
        memory_limit: int | None = None,
        memory_token_budget: int | None = None,
        auto_record: bool | None = None,
        memory: bool | None = None,
    ) -> dict:
        """Replace this space's proxy defaults.

        A request that sets the same field — in its body or an ``X-Anona-*``
        header — still wins over these. Replaces the record, so anything left
        out is cleared.
        """
        resp = self._get_client().put(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/chat-settings",
            json={
                "memory_limit": memory_limit,
                "memory_token_budget": memory_token_budget,
                "auto_record": auto_record,
                "memory": memory,
            },
        )
        self._raise(resp)
        return resp.json()

    def reset_chat_settings(self, space_id: str) -> None:
        """Drop this space's proxy defaults, back to the platform defaults."""
        resp = self._get_client().delete(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/chat-settings"
        )
        self._raise(resp)

    # ── Webhooks ──────────────────────────────────────────────────────────────

    @staticmethod
    def _webhook_changes(
        url: str | None, event_types: list[str] | None, enabled: bool | None
    ) -> dict:
        """Only the fields actually passed.

        Updating a webhook is a PATCH, unlike the settings PUTs above: the API
        changes what it is sent and leaves the rest alone, so an unset argument
        has to be omitted rather than nulled.
        """
        changes: dict = {}
        if url is not None:
            changes["url"] = url
        if event_types is not None:
            changes["event_types"] = event_types
        if enabled is not None:
            changes["enabled"] = enabled
        return changes

    def create_webhook(
        self,
        space_id: str,
        *,
        url: str,
        event_types: list[str] | None = None,
        enabled: bool = True,
    ) -> dict:
        """Register an HTTPS endpoint to be called when something happens here.

        Events are ``memory.created``, ``memory.consolidated`` and
        ``security.policy_triggered``.

        The response carries ``secret``, and this is the **only** time it is
        returned — store it. Every delivery is signed with it as
        ``X-Anona-Signature: sha256=<hex>``, the HMAC-SHA256 of the raw request
        body; compare with a constant-time check.
        """
        resp = self._get_client().post(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/webhooks",
            json={
                "url": url,
                "event_types": event_types or ["memory.created"],
                "enabled": enabled,
            },
        )
        self._raise(resp)
        return resp.json()

    def list_webhooks(self, space_id: str) -> list[dict]:
        """Every webhook registered on this space."""
        resp = self._get_client().get(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/webhooks"
        )
        self._raise(resp)
        return resp.json().get("items", [])

    def update_webhook(
        self,
        space_id: str,
        webhook_id: str,
        *,
        url: str | None = None,
        event_types: list[str] | None = None,
        enabled: bool | None = None,
    ) -> dict:
        """Change a webhook's URL, events or enabled state. Only what you pass."""
        resp = self._get_client().patch(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/webhooks/{_seg(webhook_id)}",
            json=self._webhook_changes(url, event_types, enabled),
        )
        self._raise(resp)
        return resp.json()

    def delete_webhook(self, space_id: str, webhook_id: str) -> None:
        """Remove a webhook. Queued deliveries for it stop."""
        resp = self._get_client().delete(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/webhooks/{_seg(webhook_id)}"
        )
        self._raise(resp)

    def list_webhook_deliveries(
        self,
        space_id: str,
        webhook_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        """Recent delivery attempts, newest first — for debugging a receiver.

        Returns ``{"items", "next_cursor"}``; pass ``next_cursor`` back as
        ``cursor`` for the next page.
        """
        params: dict = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        resp = self._get_client().get(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}"
            f"/webhooks/{_seg(webhook_id)}/deliveries",
            params=params,
        )
        self._raise(resp)
        return resp.json()

    def get_space(
        self,
        space_id: str,
    ) -> dict:
        """One space by id.

        Accepts the qualified ``owner:name`` form for a space shared with you
        by another organization."""
        return self._call(
            "GET",
            f"/v1/spaces/{_seg(space_id)}",
        )

    def list_memories(
        self,
        space_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        q: str | None = None,
        memory_type: str | None = None,
        state: str | None = None,
        prefer_observations: bool | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        member_id: str | None = None,
    ) -> dict:
        """Page through the memories stored in a space.

        This is browsing, not searching — :meth:`retrieve` ranks by relevance to
        a query, this returns the space's contents in order with a ``total``.

        A synthesized memory and the raw facts behind it are the same knowledge
        in two layers, so only the synthesis is listed by default. Pass
        ``prefer_observations=False`` to page the underlying evidence too.

        ``state`` filters on curation state — ``"active"`` (the default) or
        ``"invalidated"`` to see what has been superseded. ``q`` is a plain
        substring filter, ``memory_type`` narrows by kind, and ``member_id``
        (``"me"`` for yourself) narrows to one writer in a shared space."""
        return self._call(
            "GET",
            f"/v1/spaces/{_seg(space_id)}/memories",
            params=_compact(
                (
                    ("limit", limit),
                    ("offset", offset),
                    ("q", q),
                    ("type", memory_type),
                    ("state", state),
                    ("prefer_observations", prefer_observations),
                    ("user_id", user_id),
                    ("agent_id", agent_id),
                    ("session_id", session_id),
                    ("member_id", member_id),
                )
            ),
        )

    def get_memory_history(
        self,
        space_id: str, memory_id: str,
    ) -> dict:
        """Every recorded version of one memory, newest first.

        A memory is edited in place by :meth:`update_memory` and superseded by
        consolidation, so this is how you see what it used to say and why it
        changed."""
        return self._call(
            "GET",
            f"/v1/spaces/{_seg(space_id)}/memories/{_seg(memory_id)}/history",
        )

    def update_memory(
        self,
        space_id: str,
        memory_id: str,
        *,
        text: str | None = None,
        context: str | None = None,
        occurred_start: str | None = None,
        occurred_end: str | None = None,
        memory_type: str | None = None,
        entities: list[str] | None = None,
        state: str | None = None,
        reason: str | None = None,
    ) -> dict:
        """Correct one memory in place.

        Only the fields you pass are changed. ``state="invalidated"`` retires a
        memory without deleting it, which is the reversible half of
        :meth:`delete_memory`. ``reason`` is a free-text note kept on the
        memory's history, so an audit shows why it moved — worth passing."""
        return self._call(
            "PATCH",
            f"/v1/spaces/{_seg(space_id)}/memories/{_seg(memory_id)}",
            json=_compact(
                (
                    ("text", text),
                    ("context", context),
                    ("occurred_start", occurred_start),
                    ("occurred_end", occurred_end),
                    ("memory_type", memory_type),
                    ("entities", entities),
                    ("state", state),
                    ("reason", reason),
                )
            ),
        )

    def get_usage(self) -> dict:
        """Credits and rate limit for the organization this key belongs to.

        Free, and useful before a bulk ingest: it reports ``credits_remaining``,
        ``credits_limit``, ``credits_used`` and ``rate_limit_per_min``. For the
        budget as of the *last* call you made, read :attr:`rate_limit` instead —
        it costs no request at all."""
        return self._call(
            "GET",
            "/v1/usage/me",
        )

    def get_document(
        self,
        space_id: str, document_id: str,
    ) -> dict:
        """One document by id, with its source and memory count."""
        return self._call(
            "GET",
            f"/v1/spaces/{_seg(space_id)}/documents/{_seg(document_id)}",
        )

    def cancel_job(
        self,
        space_id: str, job_id: str,
    ) -> dict:
        """Cancel a queued ingestion job.

        Only work that has not started yet can be cancelled; a job already
        running is reported as such rather than being torn down mid-write."""
        return self._call(
            "DELETE",
            f"/v1/spaces/{_seg(space_id)}/jobs/{_seg(job_id)}",
        )

    def get_reason_settings(
        self,
        space_id: str,
    ) -> dict:
        """The model this space uses for :meth:`reason`, or null for the default."""
        return self._call(
            "GET",
            f"/v1/spaces/{_seg(space_id)}/reason-settings",
        )

    def set_reason_settings(
        self,
        space_id: str, *, model: str | None = None,
    ) -> dict:
        """Pin the model :meth:`reason` uses for this space.

        Takes a model id or a tier name (``"fast"``, ``"balanced"``). It is
        stored resolved, so re-pointing a tier later never moves a space that
        already chose one. ``model=None`` clears the override.

        Owner-only."""
        return self._call(
            "PUT",
            f"/v1/spaces/{_seg(space_id)}/reason-settings",
            json={"model": model},
        )

    def reset_reason_settings(
        self,
        space_id: str,
    ) -> None:
        """Clear the space's reason-model override. Owner-only."""
        return self._call(
            "DELETE",
            f"/v1/spaces/{_seg(space_id)}/reason-settings",
            expect_no_content=True,
        )

    def list_memory_models(
        self,
        space_id: str, *, limit: int = 50, offset: int = 0, tags: list[str] | None = None,
    ) -> dict:
        """The memory models defined on a space, with their current content.

        A memory model is a standing question the space keeps an answer to —
        refreshed as memories arrive, rather than computed per call. Not to be
        confused with :meth:`list_catalog_models`, which lists the LLMs."""
        return self._call(
            "GET",
            f"/v1/spaces/{_seg(space_id)}/models",
            params=_compact((("limit", limit), ("offset", offset), ("tags", tags))),
        )

    def create_memory_model(
        self,
        space_id: str,
        *,
        name: str,
        query: str,
        model_id: str | None = None,
        tags: list[str] | None = None,
        max_tokens: int | None = None,
        trigger: dict | None = None,
    ) -> dict:
        """Define a memory model.

        Content is generated asynchronously, so the model comes back before it
        has an answer — its ``content`` is null until the first refresh lands.
        Billed flat at creation."""
        return self._call(
            "POST",
            f"/v1/spaces/{_seg(space_id)}/models",
            json=_compact(
                (
                    ("name", name),
                    ("query", query),
                    ("model_id", model_id),
                    ("tags", tags),
                    ("max_tokens", max_tokens),
                    ("trigger", trigger),
                )
            ),
        )

    def get_memory_model(
        self,
        space_id: str, model_id: str,
    ) -> dict:
        """One memory model, with its current content."""
        return self._call(
            "GET",
            f"/v1/spaces/{_seg(space_id)}/models/{_seg(model_id)}",
        )

    def update_memory_model(
        self,
        space_id: str,
        model_id: str,
        *,
        name: str | None = None,
        query: str | None = None,
        tags: list[str] | None = None,
        max_tokens: int | None = None,
        trigger: dict | None = None,
    ) -> dict:
        """Edit a memory model's definition.

        Deliberately does not re-answer it: a rewrite costs credits and an edit
        is often a typo fix. Call :meth:`refresh_memory_model` when you want
        the content regenerated."""
        return self._call(
            "PATCH",
            f"/v1/spaces/{_seg(space_id)}/models/{_seg(model_id)}",
            json=_compact(
                (
                    ("name", name),
                    ("query", query),
                    ("tags", tags),
                    ("max_tokens", max_tokens),
                    ("trigger", trigger),
                )
            ),
        )

    def delete_memory_model(
        self,
        space_id: str, model_id: str,
    ) -> None:
        """Delete a memory model and its content.

        This really deletes. To keep the definition and drop only the answer,
        use :meth:`clear_memory_model`."""
        return self._call(
            "DELETE",
            f"/v1/spaces/{_seg(space_id)}/models/{_seg(model_id)}",
            expect_no_content=True,
        )

    def refresh_memory_model(
        self,
        space_id: str, model_id: str,
    ) -> dict:
        """Re-answer a memory model from the space's current memories.

        Asynchronous and billed flat — returns a ``job_id`` to poll with
        :meth:`get_job`."""
        return self._call(
            "POST",
            f"/v1/spaces/{_seg(space_id)}/models/{_seg(model_id)}/refresh",
        )

    def clear_memory_model(
        self,
        space_id: str, model_id: str,
    ) -> dict:
        """Wipe a memory model's content, keeping its definition."""
        return self._call(
            "POST",
            f"/v1/spaces/{_seg(space_id)}/models/{_seg(model_id)}/clear",
        )

    def get_memory_model_history(
        self,
        space_id: str, model_id: str,
    ) -> dict:
        """Earlier versions of a memory model's content."""
        return self._call(
            "GET",
            f"/v1/spaces/{_seg(space_id)}/models/{_seg(model_id)}/history",
        )

    def get_space_profile(
        self,
        space_id: str,
    ) -> dict:
        """A space's profile — its mission and disposition."""
        return self._call(
            "GET",
            f"/v1/spaces/{_seg(space_id)}/profile",
        )

    def list_catalog_models(self) -> dict:
        """Every LLM this deployment will answer with, and what each costs.

        Selectable on any plan: credits are derived from real cost, so a credit
        balance is already the spend cap and the model choice cannot move it.
        Pass an ``id`` (or a short name) from here as ``model=`` to
        :meth:`reason`, or pin one per space with :meth:`set_reason_settings`.

        Named ``catalog`` to keep it apart from :meth:`list_memory_models`,
        which is the *memory* models on one space."""
        return self._call(
            "GET",
            "/v1/models",
        )

    # ── Async ─────────────────────────────────────────────────────────────────

    async def async_record(
        self,
        space_id: str,
        content: str,
        metadata: dict | None = None,
        tags: list[str] | None = None,
        background: bool = False,
        user_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        timestamp: str | None = None,
    ) -> dict:
        """Async (asyncio) variant of :meth:`record`. ``background=True`` queues
        the write and returns a ``job_id`` — poll with :meth:`async_get_job`."""
        body: dict = {
            "space_id": space_id,
            "content": content,
            "metadata": metadata or {},
        }
        if tags:
            body["tags"] = tags
        for key, value in (
            ("user_id", user_id),
            ("agent_id", agent_id),
            ("session_id", session_id),
            ("timestamp", timestamp),
        ):
            if value:
                body[key] = value
        if background:
            body["async"] = True
        resp = await self._get_async_client().post(
            f"{self._base_url}/v1/record", json=body
        )
        self._raise(resp)
        return resp.json()

    async def async_record_batch(self, space_id: str, items: list[dict]) -> dict:
        """Async (asyncio) variant of :meth:`record_batch`."""
        resp = await self._get_async_client().post(
            f"{self._base_url}/v1/record/batch",
            json={"space_id": space_id, "items": items},
        )
        self._raise(resp)
        return resp.json()

    async def async_get_job(self, space_id: str, job_id: str) -> dict:
        """Async (asyncio) variant of :meth:`get_job`."""
        resp = await self._get_async_client().get(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/jobs/{_seg(job_id)}"
        )
        self._raise(resp)
        return resp.json()

    async def async_retrieve(
        self,
        space_id: str,
        query: str,
        limit: int = 10,
        mode: str = "accurate",
        user_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        as_of: str | None = None,
        query_timestamp: str | None = None,
        occurred_after: str | None = None,
        occurred_before: str | None = None,
        top_k: int | None = None,
        memory_type: list[str] | None = None,
        tags: list[str] | None = None,
        tags_match: str | None = None,
        tag_groups: list[dict] | None = None,
        prefer_observations: bool | None = None,
        min_score: float | None = None,
        member_id: str | None = None,
    ) -> list[dict]:
        """Async (asyncio) variant of :meth:`retrieve`."""
        body: dict = {
            "space_id": space_id,
            "query": query,
            "limit": limit,
            "mode": mode,
        }
        body.update(
            _search_extras(
                top_k=top_k,
                memory_type=memory_type,
                tags=tags,
                tags_match=tags_match,
                tag_groups=tag_groups,
                prefer_observations=prefer_observations,
                min_score=min_score,
                member_id=member_id,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                as_of=as_of,
                query_timestamp=query_timestamp,
                occurred_after=occurred_after,
                occurred_before=occurred_before,
            )
        )
        resp = await self._get_async_client().post(
            f"{self._base_url}/v1/retrieve",
            json=body,
        )
        self._raise(resp)
        return resp.json().get("results", [])

    async def async_retrieve_receipt(
        self,
        space_id: str,
        query: str,
        limit: int = 10,
        mode: str = "accurate",
        receipt_detail: str = "basic",
        user_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        as_of: str | None = None,
        query_timestamp: str | None = None,
        occurred_after: str | None = None,
        occurred_before: str | None = None,
        top_k: int | None = None,
        memory_type: list[str] | None = None,
        tags: list[str] | None = None,
        tags_match: str | None = None,
        tag_groups: list[dict] | None = None,
        prefer_observations: bool | None = None,
        min_score: float | None = None,
        member_id: str | None = None,
    ) -> RetrieveWithReceipt:
        """Async (asyncio) variant of :meth:`retrieve_receipt`."""
        body: dict = {
            "space_id": space_id,
            "query": query,
            "limit": limit,
            "mode": mode,
            "receipt": True,
        }
        if receipt_detail != "basic":
            body["receipt_detail"] = receipt_detail
        body.update(
            _search_extras(
                top_k=top_k,
                memory_type=memory_type,
                tags=tags,
                tags_match=tags_match,
                tag_groups=tag_groups,
                prefer_observations=prefer_observations,
                min_score=min_score,
                member_id=member_id,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                as_of=as_of,
                query_timestamp=query_timestamp,
                occurred_after=occurred_after,
                occurred_before=occurred_before,
            )
        )
        resp = await self._get_async_client().post(
            f"{self._base_url}/v1/retrieve",
            json=body,
        )
        self._raise(resp)
        data = resp.json()
        return RetrieveWithReceipt(
            memories=data.get("results", []),
            receipt_id=data.get("receipt_id") or resp.headers.get("x-request-id"),
        )

    async def async_get_receipt(self, request_id: str) -> dict:
        """Async (asyncio) variant of :meth:`get_receipt`."""
        resp = await self._get_async_client().get(
            f"{self._base_url}/v1/receipts/{_seg(request_id)}"
        )
        self._raise(resp)
        return resp.json()

    async def async_explain(self, request_id: str, memory_id: str) -> dict:
        """Async (asyncio) variant of :meth:`explain`."""
        resp = await self._get_async_client().get(
            f"{self._base_url}/v1/receipts/{_seg(request_id)}/explain",
            params={"memory_id": memory_id},
        )
        self._raise(resp)
        return resp.json()

    async def async_get_context(
        self,
        space_id: str,
        query: str,
        limit: int = 10,
        max_tokens: int | None = None,
        block_order: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        as_of: str | None = None,
        query_timestamp: str | None = None,
        occurred_after: str | None = None,
        occurred_before: str | None = None,
        top_k: int | None = None,
        memory_type: list[str] | None = None,
        tags: list[str] | None = None,
        tags_match: str | None = None,
        tag_groups: list[dict] | None = None,
        prefer_observations: bool | None = None,
        min_score: float | None = None,
        member_id: str | None = None,
    ) -> str:
        """Async (asyncio) variant of :meth:`get_context`."""
        body: dict = {
            "space_id": space_id,
            "query": query,
            "limit": limit,
            "format": "block",
        }
        if max_tokens:
            body["context_max_tokens"] = max_tokens
        if block_order:
            body["block_order"] = block_order
        body.update(
            _search_extras(
                top_k=top_k,
                memory_type=memory_type,
                tags=tags,
                tags_match=tags_match,
                tag_groups=tag_groups,
                prefer_observations=prefer_observations,
                min_score=min_score,
                member_id=member_id,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                as_of=as_of,
                query_timestamp=query_timestamp,
                occurred_after=occurred_after,
                occurred_before=occurred_before,
            )
        )
        resp = await self._get_async_client().post(
            f"{self._base_url}/v1/retrieve", json=body
        )
        self._raise(resp)
        return resp.json().get("context") or ""

    async def async_reason(
        self,
        space_id: str,
        query: str,
        user_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        model: str | None = None,
        tag_groups: list[dict] | None = None,
    ) -> str | None:
        """Async (asyncio) variant of :meth:`reason`."""
        body: dict = {"space_id": space_id, "query": query}
        for key, value in (
            ("user_id", user_id),
            ("agent_id", agent_id),
            ("session_id", session_id),
            ("model", model),
            ("tag_groups", tag_groups),
        ):
            if value:
                body[key] = value
        resp = await self._get_async_client().post(
            f"{self._base_url}/v1/reason",
            json=body,
        )
        self._raise(resp)
        return resp.json().get("insights")

    async def async_get_user_profile(
        self,
        space_id: str,
        user_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
        memory_type: str | None = None,
        format: str | None = None,
        context_max_tokens: int | None = None,
    ) -> dict:
        """Async (asyncio) variant of :meth:`get_user_profile`."""
        resp = await self._get_async_client().get(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/users/{_seg(user_id)}/profile",
            params=self._profile_params(
                limit, offset, memory_type, format, context_max_tokens
            ),
        )
        self._raise(resp)
        return resp.json()

    async def async_ask_about_user(
        self,
        space_id: str,
        user_id: str,
        query: str,
        *,
        model: str | None = None,
    ) -> dict:
        """Async (asyncio) variant of :meth:`ask_about_user`."""
        body: dict = {"query": query}
        if model:
            body["model"] = model
        resp = await self._get_async_client().post(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/users/{_seg(user_id)}/ask",
            json=body,
        )
        self._raise(resp)
        return resp.json()

    async def async_list_spaces(self) -> list[dict]:
        resp = await self._get_async_client().get(f"{self._base_url}/v1/spaces/")
        self._raise(resp)
        return resp.json().get("spaces", [])

    async def async_get_graph(
        self, space_id: str, *, limit: int = 500, min_count: int = 1
    ) -> dict:
        resp = await self._get_async_client().get(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/graph",
            params={"limit": limit, "min_count": min_count},
        )
        self._raise(resp)
        return resp.json()

    async def async_list_entities(
        self, space_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        resp = await self._get_async_client().get(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/entities",
            params={"limit": limit, "offset": offset},
        )
        self._raise(resp)
        return resp.json().get("items", [])

    async def async_get_entity(self, space_id: str, entity_id: str) -> dict:
        resp = await self._get_async_client().get(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/entities/{_seg(entity_id)}",
        )
        self._raise(resp)
        return resp.json()

    async def async_upload_file(
        self,
        space_id: str,
        file,
        *,
        filename: str | None = None,
        strategy: str | None = None,
        tags: list[str] | str | None = None,
    ) -> dict:
        """Async (asyncio) variant of :meth:`upload_file`."""
        files, data = self._upload_parts(file, filename, strategy, tags)
        resp = await self._get_async_client().post(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/documents", files=files, data=data
        )
        self._raise(resp)
        return resp.json()

    async def async_create_space(
        self, name: str, description: str | None = None
    ) -> dict:
        """Async (asyncio) variant of :meth:`create_space`."""
        resp = await self._get_async_client().post(
            f"{self._base_url}/v1/spaces/",
            json={"name": name, "description": description},
        )
        self._raise(resp)
        return resp.json()

    async def async_list_documents(
        self, space_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        """Async (asyncio) variant of :meth:`list_documents`."""
        resp = await self._get_async_client().get(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/documents",
            params={"limit": limit, "offset": offset},
        )
        self._raise(resp)
        return resp.json().get("documents", [])

    async def async_delete_document(self, space_id: str, document_id: str) -> None:
        """Async (asyncio) variant of :meth:`delete_document`."""
        resp = await self._get_async_client().delete(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/documents/{_seg(document_id)}"
        )
        self._raise(resp)

    async def async_delete_space(self, space_id: str) -> None:
        """Async (asyncio) variant of :meth:`delete_space`."""
        resp = await self._get_async_client().delete(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}"
        )
        self._raise(resp)

    async def async_delete_memory(self, space_id: str, memory_id: str) -> None:
        """Async (asyncio) variant of :meth:`delete_memory`."""
        resp = await self._get_async_client().delete(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/memories/{_seg(memory_id)}"
        )
        self._raise(resp)

    async def async_get_extraction_settings(self, space_id: str) -> dict:
        """Async (asyncio) variant of :meth:`get_extraction_settings`."""
        resp = await self._get_async_client().get(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/extraction-settings"
        )
        self._raise(resp)
        return resp.json()

    async def async_set_extraction_settings(
        self,
        space_id: str,
        *,
        mode: str | None = None,
        guidance: str | None = None,
        custom_prompt: str | None = None,
    ) -> dict:
        """Async (asyncio) variant of :meth:`set_extraction_settings`."""
        resp = await self._get_async_client().put(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/extraction-settings",
            json={"mode": mode, "guidance": guidance, "custom_prompt": custom_prompt},
        )
        self._raise(resp)
        return resp.json()

    async def async_reset_extraction_settings(self, space_id: str) -> None:
        """Async (asyncio) variant of :meth:`reset_extraction_settings`."""
        resp = await self._get_async_client().delete(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/extraction-settings"
        )
        self._raise(resp)

    async def async_get_chat_settings(self, space_id: str) -> dict:
        """Async (asyncio) variant of :meth:`get_chat_settings`."""
        resp = await self._get_async_client().get(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/chat-settings"
        )
        self._raise(resp)
        return resp.json()

    async def async_set_chat_settings(
        self,
        space_id: str,
        *,
        memory_limit: int | None = None,
        memory_token_budget: int | None = None,
        auto_record: bool | None = None,
        memory: bool | None = None,
    ) -> dict:
        """Async (asyncio) variant of :meth:`set_chat_settings`."""
        resp = await self._get_async_client().put(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/chat-settings",
            json={
                "memory_limit": memory_limit,
                "memory_token_budget": memory_token_budget,
                "auto_record": auto_record,
                "memory": memory,
            },
        )
        self._raise(resp)
        return resp.json()

    async def async_reset_chat_settings(self, space_id: str) -> None:
        """Async (asyncio) variant of :meth:`reset_chat_settings`."""
        resp = await self._get_async_client().delete(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/chat-settings"
        )
        self._raise(resp)

    async def async_create_webhook(
        self,
        space_id: str,
        *,
        url: str,
        event_types: list[str] | None = None,
        enabled: bool = True,
    ) -> dict:
        """Async (asyncio) variant of :meth:`create_webhook`."""
        resp = await self._get_async_client().post(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/webhooks",
            json={
                "url": url,
                "event_types": event_types or ["memory.created"],
                "enabled": enabled,
            },
        )
        self._raise(resp)
        return resp.json()

    async def async_list_webhooks(self, space_id: str) -> list[dict]:
        """Async (asyncio) variant of :meth:`list_webhooks`."""
        resp = await self._get_async_client().get(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/webhooks"
        )
        self._raise(resp)
        return resp.json().get("items", [])

    async def async_update_webhook(
        self,
        space_id: str,
        webhook_id: str,
        *,
        url: str | None = None,
        event_types: list[str] | None = None,
        enabled: bool | None = None,
    ) -> dict:
        """Async (asyncio) variant of :meth:`update_webhook`."""
        resp = await self._get_async_client().patch(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/webhooks/{_seg(webhook_id)}",
            json=self._webhook_changes(url, event_types, enabled),
        )
        self._raise(resp)
        return resp.json()

    async def async_delete_webhook(self, space_id: str, webhook_id: str) -> None:
        """Async (asyncio) variant of :meth:`delete_webhook`."""
        resp = await self._get_async_client().delete(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}/webhooks/{_seg(webhook_id)}"
        )
        self._raise(resp)

    async def async_list_webhook_deliveries(
        self,
        space_id: str,
        webhook_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        """Async (asyncio) variant of :meth:`list_webhook_deliveries`."""
        params: dict = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        resp = await self._get_async_client().get(
            f"{self._base_url}/v1/spaces/{_seg(space_id)}"
            f"/webhooks/{_seg(webhook_id)}/deliveries",
            params=params,
        )
        self._raise(resp)
        return resp.json()

    async def async_get_space(
        self,
        space_id: str,
    ) -> dict:
        """One space by id.

        Accepts the qualified ``owner:name`` form for a space shared with you
        by another organization."""
        return await self._acall(
            "GET",
            f"/v1/spaces/{_seg(space_id)}",
        )

    async def async_list_memories(
        self,
        space_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        q: str | None = None,
        memory_type: str | None = None,
        state: str | None = None,
        prefer_observations: bool | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        member_id: str | None = None,
    ) -> dict:
        """Page through the memories stored in a space.

        This is browsing, not searching — :meth:`retrieve` ranks by relevance to
        a query, this returns the space's contents in order with a ``total``.

        A synthesized memory and the raw facts behind it are the same knowledge
        in two layers, so only the synthesis is listed by default. Pass
        ``prefer_observations=False`` to page the underlying evidence too.

        ``state`` filters on curation state — ``"active"`` (the default) or
        ``"invalidated"`` to see what has been superseded. ``q`` is a plain
        substring filter, ``memory_type`` narrows by kind, and ``member_id``
        (``"me"`` for yourself) narrows to one writer in a shared space."""
        return await self._acall(
            "GET",
            f"/v1/spaces/{_seg(space_id)}/memories",
            params=_compact(
                (
                    ("limit", limit),
                    ("offset", offset),
                    ("q", q),
                    ("type", memory_type),
                    ("state", state),
                    ("prefer_observations", prefer_observations),
                    ("user_id", user_id),
                    ("agent_id", agent_id),
                    ("session_id", session_id),
                    ("member_id", member_id),
                )
            ),
        )

    async def async_get_memory_history(
        self,
        space_id: str, memory_id: str,
    ) -> dict:
        """Every recorded version of one memory, newest first.

        A memory is edited in place by :meth:`update_memory` and superseded by
        consolidation, so this is how you see what it used to say and why it
        changed."""
        return await self._acall(
            "GET",
            f"/v1/spaces/{_seg(space_id)}/memories/{_seg(memory_id)}/history",
        )

    async def async_update_memory(
        self,
        space_id: str,
        memory_id: str,
        *,
        text: str | None = None,
        context: str | None = None,
        occurred_start: str | None = None,
        occurred_end: str | None = None,
        memory_type: str | None = None,
        entities: list[str] | None = None,
        state: str | None = None,
        reason: str | None = None,
    ) -> dict:
        """Correct one memory in place.

        Only the fields you pass are changed. ``state="invalidated"`` retires a
        memory without deleting it, which is the reversible half of
        :meth:`delete_memory`. ``reason`` is a free-text note kept on the
        memory's history, so an audit shows why it moved — worth passing."""
        return await self._acall(
            "PATCH",
            f"/v1/spaces/{_seg(space_id)}/memories/{_seg(memory_id)}",
            json=_compact(
                (
                    ("text", text),
                    ("context", context),
                    ("occurred_start", occurred_start),
                    ("occurred_end", occurred_end),
                    ("memory_type", memory_type),
                    ("entities", entities),
                    ("state", state),
                    ("reason", reason),
                )
            ),
        )

    async def async_get_usage(self) -> dict:
        """Credits and rate limit for the organization this key belongs to.

        Free, and useful before a bulk ingest: it reports ``credits_remaining``,
        ``credits_limit``, ``credits_used`` and ``rate_limit_per_min``. For the
        budget as of the *last* call you made, read :attr:`rate_limit` instead —
        it costs no request at all."""
        return await self._acall(
            "GET",
            "/v1/usage/me",
        )

    async def async_get_document(
        self,
        space_id: str, document_id: str,
    ) -> dict:
        """One document by id, with its source and memory count."""
        return await self._acall(
            "GET",
            f"/v1/spaces/{_seg(space_id)}/documents/{_seg(document_id)}",
        )

    async def async_cancel_job(
        self,
        space_id: str, job_id: str,
    ) -> dict:
        """Cancel a queued ingestion job.

        Only work that has not started yet can be cancelled; a job already
        running is reported as such rather than being torn down mid-write."""
        return await self._acall(
            "DELETE",
            f"/v1/spaces/{_seg(space_id)}/jobs/{_seg(job_id)}",
        )

    async def async_get_reason_settings(
        self,
        space_id: str,
    ) -> dict:
        """The model this space uses for :meth:`reason`, or null for the default."""
        return await self._acall(
            "GET",
            f"/v1/spaces/{_seg(space_id)}/reason-settings",
        )

    async def async_set_reason_settings(
        self,
        space_id: str, *, model: str | None = None,
    ) -> dict:
        """Pin the model :meth:`reason` uses for this space.

        Takes a model id or a tier name (``"fast"``, ``"balanced"``). It is
        stored resolved, so re-pointing a tier later never moves a space that
        already chose one. ``model=None`` clears the override.

        Owner-only."""
        return await self._acall(
            "PUT",
            f"/v1/spaces/{_seg(space_id)}/reason-settings",
            json={"model": model},
        )

    async def async_reset_reason_settings(
        self,
        space_id: str,
    ) -> None:
        """Clear the space's reason-model override. Owner-only."""
        return await self._acall(
            "DELETE",
            f"/v1/spaces/{_seg(space_id)}/reason-settings",
            expect_no_content=True,
        )

    async def async_list_memory_models(
        self,
        space_id: str, *, limit: int = 50, offset: int = 0, tags: list[str] | None = None,
    ) -> dict:
        """The memory models defined on a space, with their current content.

        A memory model is a standing question the space keeps an answer to —
        refreshed as memories arrive, rather than computed per call. Not to be
        confused with :meth:`list_catalog_models`, which lists the LLMs."""
        return await self._acall(
            "GET",
            f"/v1/spaces/{_seg(space_id)}/models",
            params=_compact((("limit", limit), ("offset", offset), ("tags", tags))),
        )

    async def async_create_memory_model(
        self,
        space_id: str,
        *,
        name: str,
        query: str,
        model_id: str | None = None,
        tags: list[str] | None = None,
        max_tokens: int | None = None,
        trigger: dict | None = None,
    ) -> dict:
        """Define a memory model.

        Content is generated asynchronously, so the model comes back before it
        has an answer — its ``content`` is null until the first refresh lands.
        Billed flat at creation."""
        return await self._acall(
            "POST",
            f"/v1/spaces/{_seg(space_id)}/models",
            json=_compact(
                (
                    ("name", name),
                    ("query", query),
                    ("model_id", model_id),
                    ("tags", tags),
                    ("max_tokens", max_tokens),
                    ("trigger", trigger),
                )
            ),
        )

    async def async_get_memory_model(
        self,
        space_id: str, model_id: str,
    ) -> dict:
        """One memory model, with its current content."""
        return await self._acall(
            "GET",
            f"/v1/spaces/{_seg(space_id)}/models/{_seg(model_id)}",
        )

    async def async_update_memory_model(
        self,
        space_id: str,
        model_id: str,
        *,
        name: str | None = None,
        query: str | None = None,
        tags: list[str] | None = None,
        max_tokens: int | None = None,
        trigger: dict | None = None,
    ) -> dict:
        """Edit a memory model's definition.

        Deliberately does not re-answer it: a rewrite costs credits and an edit
        is often a typo fix. Call :meth:`refresh_memory_model` when you want
        the content regenerated."""
        return await self._acall(
            "PATCH",
            f"/v1/spaces/{_seg(space_id)}/models/{_seg(model_id)}",
            json=_compact(
                (
                    ("name", name),
                    ("query", query),
                    ("tags", tags),
                    ("max_tokens", max_tokens),
                    ("trigger", trigger),
                )
            ),
        )

    async def async_delete_memory_model(
        self,
        space_id: str, model_id: str,
    ) -> None:
        """Delete a memory model and its content.

        This really deletes. To keep the definition and drop only the answer,
        use :meth:`clear_memory_model`."""
        return await self._acall(
            "DELETE",
            f"/v1/spaces/{_seg(space_id)}/models/{_seg(model_id)}",
            expect_no_content=True,
        )

    async def async_refresh_memory_model(
        self,
        space_id: str, model_id: str,
    ) -> dict:
        """Re-answer a memory model from the space's current memories.

        Asynchronous and billed flat — returns a ``job_id`` to poll with
        :meth:`get_job`."""
        return await self._acall(
            "POST",
            f"/v1/spaces/{_seg(space_id)}/models/{_seg(model_id)}/refresh",
        )

    async def async_clear_memory_model(
        self,
        space_id: str, model_id: str,
    ) -> dict:
        """Wipe a memory model's content, keeping its definition."""
        return await self._acall(
            "POST",
            f"/v1/spaces/{_seg(space_id)}/models/{_seg(model_id)}/clear",
        )

    async def async_get_memory_model_history(
        self,
        space_id: str, model_id: str,
    ) -> dict:
        """Earlier versions of a memory model's content."""
        return await self._acall(
            "GET",
            f"/v1/spaces/{_seg(space_id)}/models/{_seg(model_id)}/history",
        )

    async def async_get_space_profile(
        self,
        space_id: str,
    ) -> dict:
        """A space's profile — its mission and disposition."""
        return await self._acall(
            "GET",
            f"/v1/spaces/{_seg(space_id)}/profile",
        )

    async def async_list_catalog_models(self) -> dict:
        """Every LLM this deployment will answer with, and what each costs.

        Selectable on any plan: credits are derived from real cost, so a credit
        balance is already the spend cap and the model choice cannot move it.
        Pass an ``id`` (or a short name) from here as ``model=`` to
        :meth:`reason`, or pin one per space with :meth:`set_reason_settings`.

        Named ``catalog`` to keep it apart from :meth:`list_memory_models`,
        which is the *memory* models on one space."""
        return await self._acall(
            "GET",
            "/v1/models",
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the sync client, if one was ever opened."""
        if self._client is not None:
            self._client.close()

    async def aclose(self) -> None:
        """Close every async client that was ever opened. Never raises.

        There can now be more than one — see :meth:`_get_async_client` — so
        this closes the manual override (if a caller set one) and every
        per-loop entry, individually. Each is wrapped in its own
        ``try``/``except``: this method itself always runs on *some* loop
        (whichever one is driving this coroutine), but the client(s) being
        torn down may belong to a *different*, already-closed one — the
        ordinary shape is "fetch on loop A, loop A ends, close() runs on a
        fresh loop B" — and there is no way to force a clean async teardown
        of loop A's connection from loop B after the fact (``RuntimeError:
        Event loop is closed``, or "bound to a different event loop" if
        loop A is somehow still alive elsewhere). One entry's failure must
        not stop the rest from being attempted, and none may propagate:
        callers (:meth:`__aexit__`, ``MemoryBridge.close``) run this from
        cleanup paths, often their own ``finally``, the worst possible place
        for a new exception to appear.
        """
        if self._async_client is not None:
            try:
                await self._async_client.aclose()
            except Exception:
                pass
            self._async_client = None
        with self._async_clients_lock:
            clients = list(self._async_clients.values())
            self._async_clients.clear()
        for client in clients:
            try:
                await client.aclose()
            except Exception:
                pass

    def __enter__(self) -> "AnonaClient":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    async def __aenter__(self) -> "AnonaClient":
        return self

    async def __aexit__(self, *args) -> None:
        await self.aclose()
