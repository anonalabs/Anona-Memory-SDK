from __future__ import annotations

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
    into. The API applies the same encoding on its own hop internally.
    """
    return quote(str(value), safe="")


class AnonaError(Exception):
    def __init__(self, status_code: int, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Anona API error {status_code}: {detail}")


class AnonaClient:
    """Synchronous and async client for Anona Memory API."""

    # api.anonalabs.com reaches the API directly, without the hop through the
    # dashboard edge worker that memory.anonalabs.com takes. memory.anonalabs.com
    # keeps working indefinitely, so existing code needs no change.
    def __init__(self, api_key: str, base_url: str = "https://api.anonalabs.com"):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        # Created lazily (first sync/async call) rather than both up front —
        # a caller that only ever uses one side previously still opened (and
        # leaked) the other's connection pool, since close()/aclose() each
        # only tear down their own half.
        self._client: httpx.Client | None = None
        self._async_client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=30.0,
            )
        return self._client

    def _get_async_client(self) -> httpx.AsyncClient:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=30.0,
            )
        return self._async_client

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
    ) -> dict:
        """Store a memory.

        ``tags`` attaches visibility-scope tags that :meth:`retrieve` can filter
        on (e.g. tag by the source agent in agent-to-agent workflows).

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
    ) -> list[dict]:
        """Search memories.

        ``mode="accurate"`` (default) neurally reranks results for best
        relevance. ``mode="fast"`` skips that pass — much lower latency, at
        some cost to relevance quality.
        """
        resp = self._get_client().post(
            f"{self._base_url}/v1/retrieve",
            json={"space_id": space_id, "query": query, "limit": limit, "mode": mode},
        )
        self._raise(resp)
        return resp.json().get("results", [])

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
    ) -> list[dict]:
        """Async (asyncio) variant of :meth:`retrieve`."""
        resp = await self._get_async_client().post(
            f"{self._base_url}/v1/retrieve",
            json={"space_id": space_id, "query": query, "limit": limit, "mode": mode},
        )
        self._raise(resp)
        return resp.json().get("results", [])

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
        """Close the async client, if one was ever opened."""
        if self._async_client is not None:
            await self._async_client.aclose()

    def __enter__(self) -> "AnonaClient":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    async def __aenter__(self) -> "AnonaClient":
        return self

    async def __aexit__(self, *args) -> None:
        await self.aclose()
