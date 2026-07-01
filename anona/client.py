from __future__ import annotations

import httpx


class AnonaError(Exception):
    def __init__(self, status_code: int, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Anona API error {status_code}: {detail}")


class AnonaClient:
    """Synchronous and async client for Anona Memory API."""

    def __init__(self, api_key: str, base_url: str = "https://api.anona.ai"):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )
        self._async_client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )

    def _raise(self, resp: httpx.Response) -> None:
        if not resp.is_success:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise AnonaError(resp.status_code, detail)

    # ── Sync ──────────────────────────────────────────────────────────────────

    def add_memory(
        self,
        space_id: str,
        content: str,
        metadata: dict | None = None,
    ) -> dict:
        resp = self._client.post(
            f"{self._base_url}/v1/memories",
            json={"space_id": space_id, "content": content, "metadata": metadata or {}},
        )
        self._raise(resp)
        return resp.json()

    def search(
        self,
        space_id: str,
        query: str,
        limit: int = 10,
    ) -> list[dict]:
        resp = self._client.post(
            f"{self._base_url}/v1/search",
            json={"space_id": space_id, "query": query, "limit": limit},
        )
        self._raise(resp)
        return resp.json().get("results", [])

    def insights(self, space_id: str, query: str) -> str | None:
        resp = self._client.post(
            f"{self._base_url}/v1/insights",
            json={"space_id": space_id, "query": query},
        )
        self._raise(resp)
        return resp.json().get("insights")

    # ── Async ─────────────────────────────────────────────────────────────────

    async def async_add_memory(
        self,
        space_id: str,
        content: str,
        metadata: dict | None = None,
    ) -> dict:
        resp = await self._async_client.post(
            f"{self._base_url}/v1/memories",
            json={"space_id": space_id, "content": content, "metadata": metadata or {}},
        )
        self._raise(resp)
        return resp.json()

    async def async_search(
        self,
        space_id: str,
        query: str,
        limit: int = 10,
    ) -> list[dict]:
        resp = await self._async_client.post(
            f"{self._base_url}/v1/search",
            json={"space_id": space_id, "query": query, "limit": limit},
        )
        self._raise(resp)
        return resp.json().get("results", [])

    async def async_insights(self, space_id: str, query: str) -> str | None:
        resp = await self._async_client.post(
            f"{self._base_url}/v1/insights",
            json={"space_id": space_id, "query": query},
        )
        self._raise(resp)
        return resp.json().get("insights")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def close(self) -> None:
        self._client.close()

    async def aclose(self) -> None:
        await self._async_client.aclose()

    def __enter__(self) -> "AnonaClient":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    async def __aenter__(self) -> "AnonaClient":
        return self

    async def __aexit__(self, *args) -> None:
        await self.aclose()
