from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("anona.litellm")

DEFAULT_BASE_URL = "https://api.anonalabs.com"


class AnonaMemory:
    """LiteLLM callback that injects Anona memories before each call and stores Q&A after.

    Works with plain ``litellm.completion()`` / ``litellm.acompletion()`` — no
    LiteLLM proxy required::

        from anona.integrations.litellm import AnonaMemory

        mem = AnonaMemory(
            api_key="anona_live_...",
            space_id="your-space-id",
        )
        mem.enable()

        # All subsequent litellm.completion() calls auto-inject + auto-store.

    Implementation note: this registers the LiteLLM *SDK-mode* callback hooks
    (``log_pre_api_call`` / ``log_success_event`` and their async twins), which
    are the ones ``litellm.completion()`` actually fires. An earlier version
    implemented the proxy-only hook names (``async_pre_call_hook`` /
    ``async_post_call_success_hook``), which the SDK never calls — so
    ``enable()`` injected and stored nothing. It also *replaced*
    ``litellm.callbacks``, wiping any callbacks the caller had already
    registered; this now appends (with a duplicate guard) instead.
    """

    def __init__(
        self,
        api_key: str,
        space_id: str,
        base_url: str = DEFAULT_BASE_URL,
        recall_limit: int = 5,
        inject_mode: str = "system",
        store_after: bool = True,
    ):
        self._api_key = api_key
        self._space_id = space_id
        self._base_url = base_url.rstrip("/")
        self._recall_limit = recall_limit
        self._inject_mode = inject_mode
        self._store_after = store_after
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        # A persistent sync client so plain (non-async) litellm.completion() —
        # which runs with no event loop — can search/store without spinning one
        # up per call. The old implementation only had async helpers and fired
        # them with asyncio.create_task(), which needs a running loop that a sync
        # completion does not have, and kept no reference to the task (so even
        # where a loop existed the store could be garbage-collected before it
        # ran). The async hooks below now await the store directly, so there is
        # no fire-and-forget task to lose.
        self._sync_http = httpx.Client(timeout=10.0)
        self._async_http: httpx.AsyncClient | None = None

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _get_async_http(self) -> httpx.AsyncClient:
        if self._async_http is None or self._async_http.is_closed:
            self._async_http = httpx.AsyncClient(timeout=10.0)
        return self._async_http

    def _sync_search(self, query: str) -> list[dict]:
        try:
            resp = self._sync_http.post(
                f"{self._base_url}/v1/retrieve",
                headers=self._headers,
                json={
                    "space_id": self._space_id,
                    "query": query,
                    "limit": self._recall_limit,
                },
            )
            if resp.is_success:
                return resp.json().get("results", [])
            logger.warning(
                "anona: memory search failed (HTTP %d) — proceeding without "
                "injected memories: %s",
                resp.status_code,
                resp.text,
            )
        except Exception:
            logger.warning(
                "anona: memory search failed — proceeding without injected memories",
                exc_info=True,
            )
        return []

    async def _async_search(self, query: str) -> list[dict]:
        try:
            resp = await self._get_async_http().post(
                f"{self._base_url}/v1/retrieve",
                headers=self._headers,
                json={
                    "space_id": self._space_id,
                    "query": query,
                    "limit": self._recall_limit,
                },
            )
            if resp.is_success:
                return resp.json().get("results", [])
            logger.warning(
                "anona: memory search failed (HTTP %d) — proceeding without "
                "injected memories: %s",
                resp.status_code,
                resp.text,
            )
        except Exception:
            logger.warning(
                "anona: memory search failed — proceeding without injected memories",
                exc_info=True,
            )
        return []

    def _sync_store(self, content: str) -> None:
        try:
            resp = self._sync_http.post(
                f"{self._base_url}/v1/record",
                headers=self._headers,
                json={"space_id": self._space_id, "content": content},
            )
            if not resp.is_success:
                logger.warning(
                    "anona: failed to store conversation turn (HTTP %d): %s",
                    resp.status_code,
                    resp.text,
                )
        except Exception:
            logger.warning("anona: failed to store conversation turn", exc_info=True)

    async def _async_store(self, content: str) -> None:
        try:
            resp = await self._get_async_http().post(
                f"{self._base_url}/v1/record",
                headers=self._headers,
                json={"space_id": self._space_id, "content": content},
            )
            if not resp.is_success:
                logger.warning(
                    "anona: failed to store conversation turn (HTTP %d): %s",
                    resp.status_code,
                    resp.text,
                )
        except Exception:
            logger.warning("anona: failed to store conversation turn", exc_info=True)

    # ── Message shaping ───────────────────────────────────────────────────────

    def _build_block(self, memories: list[dict]) -> str:
        return "# Relevant Memories\n" + "\n".join(
            f"{i + 1}. {r.get('content', '')}" for i, r in enumerate(memories)
        )

    def _inject(self, messages: list[dict], memories: list[dict]) -> None:
        if not memories:
            return
        block = self._build_block(memories)
        if self._inject_mode == "system":
            sys_msgs = [m for m in messages if m.get("role") == "system"]
            if sys_msgs:
                sys_msgs[-1]["content"] = sys_msgs[-1]["content"] + "\n\n" + block
            else:
                messages.insert(0, {"role": "system", "content": block})
        else:
            user_msgs = [m for m in messages if m.get("role") == "user"]
            if user_msgs:
                user_msgs[-1]["content"] = block + "\n\n" + user_msgs[-1]["content"]

    @staticmethod
    def _last_user_msg(messages: list[dict]) -> str:
        for msg in reversed(messages):
            if msg.get("role") == "user":
                return msg.get("content", "") or ""
        return ""

    @staticmethod
    def _assistant_text(response: Any) -> str:
        try:
            if hasattr(response, "choices") and response.choices:
                return response.choices[0].message.content or ""
        except Exception:
            pass
        return ""

    def _exchange(self, messages: list[dict], response: Any) -> str | None:
        """The "User: … / Assistant: …" turn to store, or None if incomplete."""
        user_msg = self._last_user_msg(messages)
        # In "user" inject mode the block was prepended onto the user message —
        # strip it back off so the stored turn is the human's actual question.
        if "# Relevant Memories" in user_msg:
            parts = user_msg.split("# Relevant Memories")
            user_msg = parts[-1].strip() if len(parts) > 1 else parts[0].strip()
        assistant_msg = self._assistant_text(response)
        if user_msg and assistant_msg:
            return f"User: {user_msg}\nAssistant: {assistant_msg}"
        return None

    # ── enable ────────────────────────────────────────────────────────────────

    def enable(self) -> None:
        """Register as a LiteLLM callback. Call once before litellm.completion()."""
        try:
            import litellm
            from litellm.integrations.custom_logger import CustomLogger
        except ImportError as exc:
            raise ImportError(
                "litellm required: pip install 'anona[litellm]'"
            ) from exc

        anona = self

        class _Callback(CustomLogger):
            # ── sync (litellm.completion) ─────────────────────────────────────
            def log_pre_api_call(self, model: str, messages: list, kwargs: dict) -> None:
                query = anona._last_user_msg(messages)
                if not query:
                    return
                anona._inject(messages, anona._sync_search(query))

            def log_success_event(
                self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any
            ) -> None:
                if not anona._store_after:
                    return
                content = anona._exchange(kwargs.get("messages", []), response_obj)
                if content:
                    anona._sync_store(content)

            def log_failure_event(
                self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any
            ) -> None:
                pass

            # ── async (litellm.acompletion) ───────────────────────────────────
            async def async_log_pre_api_call(
                self, model: str, messages: list, kwargs: dict
            ) -> None:
                query = anona._last_user_msg(messages)
                if not query:
                    return
                anona._inject(messages, await anona._async_search(query))

            async def async_log_success_event(
                self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any
            ) -> None:
                if not anona._store_after:
                    return
                content = anona._exchange(kwargs.get("messages", []), response_obj)
                if content:
                    # Awaited, not fired-and-forgotten: the hook is already
                    # awaited by litellm, so the store completes before the call
                    # returns and can never be dropped by the GC.
                    await anona._async_store(content)

            async def async_log_failure_event(
                self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any
            ) -> None:
                pass

        if getattr(litellm, "callbacks", None) is None:
            litellm.callbacks = []
        # Append rather than replace, so a caller's own callbacks survive. Guard
        # against a duplicate registration (calling enable() twice).
        if not any(type(c).__name__ == "_Callback" for c in litellm.callbacks):
            litellm.callbacks.append(_Callback())
        logger.info(
            "Anona Memory enabled for space '%s' (limit=%d, store=%s)",
            anona._space_id,
            anona._recall_limit,
            anona._store_after,
        )
