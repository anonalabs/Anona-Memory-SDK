from __future__ import annotations

import asyncio
import threading
from urllib.parse import quote

import httpx


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
    def __init__(self, status_code: int, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Anona API error {status_code}: {detail}")


class AnonaClient:
    """Synchronous and async client for Anona Memory API."""

    # api.anonalabs.com routes straight to the API, without the hop through the
    # dashboard edge worker that memory.anonalabs.com takes.
    # memory.anonalabs.com keeps working indefinitely — existing code needs no
    # change, and callers can still override base_url.
    def __init__(self, api_key: str, base_url: str = "https://api.anonalabs.com"):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
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
                timeout=30.0,
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
                    timeout=30.0,
                )
                self._async_clients[loop] = client
            return client

    def _raise(self, resp: httpx.Response) -> None:
        if not resp.is_success:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise AnonaError(resp.status_code, detail)

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
    ) -> dict:
        """Store a memory.

        ``user_id`` / ``agent_id`` / ``session_id`` scope the memory inside the
        space: a memory written under a user is only returned to a
        :meth:`retrieve` carrying the same user. That is how one space serves
        many end users without their memories mixing.

        ``tags`` attaches visibility-scope tags that :meth:`retrieve` can filter
        on (e.g. tag by the source agent in agent-to-agent workflows).

        ``timestamp`` (ISO 8601) is when the *event* happened, not when you are
        recording it — use it when importing history so a memory about last June
        is dated last June. It is returned as ``occurred_start`` /
        ``occurred_end`` and feeds recency ranking. It does **not** change when
        the memory was recorded, so it has no effect on :meth:`retrieve`'s
        ``as_of``. Defaults to now.

        With ``background=True`` the write is queued and returns immediately with
        a ``job_id`` (``status="processing"``) instead of the stored
        ``memory_id`` — poll it with :meth:`get_job`. Use this in latency-
        sensitive paths so the call never blocks on fact extraction.
        """
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
    ) -> list[dict]:
        """Search memories.

        ``user_id`` / ``agent_id`` / ``session_id`` restrict the search to
        memories written under the same scope. The filter is strict: memories
        stored without a scope are not returned to a scoped search.

        ``mode="accurate"`` (default) neurally reranks results for best
        relevance. ``mode="fast"`` skips that pass — much lower latency, at
        some cost to relevance quality.

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
        """
        body: dict = {
            "space_id": space_id,
            "query": query,
            "limit": limit,
            "mode": mode,
        }
        for key, value in (
            ("user_id", user_id),
            ("agent_id", agent_id),
            ("session_id", session_id),
            ("as_of", as_of),
            ("query_timestamp", query_timestamp),
        ):
            if value:
                body[key] = value
        resp = self._get_client().post(
            f"{self._base_url}/v1/retrieve",
            json=body,
        )
        self._raise(resp)
        return resp.json().get("results", [])

    def get_context(
        self,
        space_id: str,
        query: str,
        limit: int = 10,
        max_tokens: int | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """The relevant memories as one prompt-ready string.

        The same search as :meth:`retrieve`, returned already formatted so it
        can go straight into a system prompt — no join to write, and the token
        budget is handled server-side rather than by a loop that does not have
        one. Returns ``""`` when nothing matched.

        ``max_tokens`` caps the block: whole memories are dropped,
        lowest-ranked first, rather than the text being cut mid-sentence.
        """
        body: dict = {
            "space_id": space_id,
            "query": query,
            "limit": limit,
            "format": "block",
        }
        if max_tokens:
            body["context_max_tokens"] = max_tokens
        for key, value in (
            ("user_id", user_id),
            ("agent_id", agent_id),
            ("session_id", session_id),
        ):
            if value:
                body[key] = value
        resp = self._get_client().post(f"{self._base_url}/v1/retrieve", json=body)
        self._raise(resp)
        return resp.json().get("context") or ""

    def reason(self, space_id: str, query: str) -> str | None:
        resp = self._get_client().post(
            f"{self._base_url}/v1/reason",
            json={"space_id": space_id, "query": query},
        )
        self._raise(resp)
        return resp.json().get("insights")

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
        file-like object. Supported types include PDF, DOCX, PPTX, XLSX, images
        (OCR), HTML, TXT/MD, CSV, and audio (transcription).

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
    ) -> list[dict]:
        """Async (asyncio) variant of :meth:`retrieve`."""
        body: dict = {
            "space_id": space_id,
            "query": query,
            "limit": limit,
            "mode": mode,
        }
        for key, value in (
            ("user_id", user_id),
            ("agent_id", agent_id),
            ("session_id", session_id),
            ("as_of", as_of),
            ("query_timestamp", query_timestamp),
        ):
            if value:
                body[key] = value
        resp = await self._get_async_client().post(
            f"{self._base_url}/v1/retrieve",
            json=body,
        )
        self._raise(resp)
        return resp.json().get("results", [])

    async def async_get_context(
        self,
        space_id: str,
        query: str,
        limit: int = 10,
        max_tokens: int | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
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
        for key, value in (
            ("user_id", user_id),
            ("agent_id", agent_id),
            ("session_id", session_id),
        ):
            if value:
                body[key] = value
        resp = await self._get_async_client().post(
            f"{self._base_url}/v1/retrieve", json=body
        )
        self._raise(resp)
        return resp.json().get("context") or ""

    async def async_reason(self, space_id: str, query: str) -> str | None:
        resp = await self._get_async_client().post(
            f"{self._base_url}/v1/reason",
            json={"space_id": space_id, "query": query},
        )
        self._raise(resp)
        return resp.json().get("insights")

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
