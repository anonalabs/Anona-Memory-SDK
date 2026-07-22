from __future__ import annotations

import httpx


class AnonaError(Exception):
    def __init__(self, status_code: int, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Anona API error {status_code}: {detail}")


class AnonaClient:
    """Synchronous and async client for Anona Memory API."""

    def __init__(self, api_key: str, base_url: str = "https://memory.anonalabs.com"):
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
            f"{self._base_url}/v1/spaces/{space_id}/jobs/{job_id}"
        )
        self._raise(resp)
        return resp.json()

    def retrieve(
        self,
        space_id: str,
        query: str,
        limit: int = 10,
    ) -> list[dict]:
        resp = self._get_client().post(
            f"{self._base_url}/v1/retrieve",
            json={"space_id": space_id, "query": query, "limit": limit},
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
            f"{self._base_url}/v1/spaces/{space_id}/graph",
            params={"limit": limit, "min_count": min_count},
        )
        self._raise(resp)
        return resp.json()

    def list_entities(
        self, space_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        """List the entities extracted in a space, most-mentioned first."""
        resp = self._get_client().get(
            f"{self._base_url}/v1/spaces/{space_id}/entities",
            params={"limit": limit, "offset": offset},
        )
        self._raise(resp)
        return resp.json().get("items", [])

    def get_entity(self, space_id: str, entity_id: str) -> dict:
        """One entity and its observations (what's been learned about it)."""
        resp = self._get_client().get(
            f"{self._base_url}/v1/spaces/{space_id}/entities/{entity_id}",
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

    def delete_space(self, space_id: str) -> None:
        """Delete a space and every memory in it. Irreversible."""
        resp = self._get_client().delete(f"{self._base_url}/v1/spaces/{space_id}")
        self._raise(resp)

    def delete_memory(self, space_id: str, memory_id: str) -> None:
        """Delete a single memory from a space. Irreversible."""
        resp = self._get_client().delete(
            f"{self._base_url}/v1/spaces/{space_id}/memories/{memory_id}"
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
            f"{self._base_url}/v1/spaces/{space_id}/jobs/{job_id}"
        )
        self._raise(resp)
        return resp.json()

    async def async_retrieve(
        self,
        space_id: str,
        query: str,
        limit: int = 10,
    ) -> list[dict]:
        resp = await self._get_async_client().post(
            f"{self._base_url}/v1/retrieve",
            json={"space_id": space_id, "query": query, "limit": limit},
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
            f"{self._base_url}/v1/spaces/{space_id}/graph",
            params={"limit": limit, "min_count": min_count},
        )
        self._raise(resp)
        return resp.json()

    async def async_list_entities(
        self, space_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        resp = await self._get_async_client().get(
            f"{self._base_url}/v1/spaces/{space_id}/entities",
            params={"limit": limit, "offset": offset},
        )
        self._raise(resp)
        return resp.json().get("items", [])

    async def async_get_entity(self, space_id: str, entity_id: str) -> dict:
        resp = await self._get_async_client().get(
            f"{self._base_url}/v1/spaces/{space_id}/entities/{entity_id}",
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

    async def async_delete_space(self, space_id: str) -> None:
        """Async (asyncio) variant of :meth:`delete_space`."""
        resp = await self._get_async_client().delete(
            f"{self._base_url}/v1/spaces/{space_id}"
        )
        self._raise(resp)

    async def async_delete_memory(self, space_id: str, memory_id: str) -> None:
        """Async (asyncio) variant of :meth:`delete_memory`."""
        resp = await self._get_async_client().delete(
            f"{self._base_url}/v1/spaces/{space_id}/memories/{memory_id}"
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
