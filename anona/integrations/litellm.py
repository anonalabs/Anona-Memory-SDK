from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("anona.litellm")

DEFAULT_BASE_URL = "https://api.anonalabs.com"

# Delimits an injected block so a later `_inject` call can find and replace it
# rather than concatenate onto it — see `_inject`'s docstring note.
_BLOCK_START = "<!-- anona:memories:start -->"
_BLOCK_END = "<!-- anona:memories:end -->"


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

    The duplicate guard is per-*instance*, not per-class: two ``AnonaMemory``
    objects for two different spaces each get their own callback registered.
    An earlier version matched on ``type(c).__name__ == "_Callback"``, which
    is true of every instance's callback (the class is defined fresh inside
    each call to ``enable()`` but always under that same local name) — so the
    second space's ``enable()`` silently registered nothing at all, having
    found the first space's callback already sitting in the list.
    """

    def __init__(
        self,
        api_key: str,
        space_id: str,
        base_url: str = DEFAULT_BASE_URL,
        recall_limit: int = 5,
        inject_mode: str = "system",
        store_after: bool = True,
        timeout: float = 3.0,
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
        # `log_pre_api_call` runs synchronously on litellm.completion()'s own
        # call path, so this timeout is added to every single chat call, not
        # just a background op — a slow gateway must not turn into a slow chat
        # app. Both search and store already fail soft into a no-op on any
        # exception (timeout included), so a low ceiling costs an unenriched
        # turn on a bad network, never a broken one. 3s, not the 10s this used
        # to hardcode; overridable for a caller who wants to trade latency for
        # a better chance of a slow search completing.
        self._timeout = timeout
        # A persistent sync client so plain (non-async) litellm.completion() —
        # which runs with no event loop — can search/store without spinning one
        # up per call. The old implementation only had async helpers and fired
        # them with asyncio.create_task(), which needs a running loop that a sync
        # completion does not have, and kept no reference to the task (so even
        # where a loop existed the store could be garbage-collected before it
        # ran). The async hooks below now await the store directly, so there is
        # no fire-and-forget task to lose.
        self._sync_http = httpx.Client(timeout=timeout)
        self._async_http: httpx.AsyncClient | None = None
        # Set once `enable()` registers this instance's callback — the
        # identity `enable()` dedups against, instead of the shared class name
        # every instance's callback happens to have (see the class docstring).
        self._callback: Any | None = None

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _get_async_http(self) -> httpx.AsyncClient:
        if self._async_http is None or self._async_http.is_closed:
            self._async_http = httpx.AsyncClient(timeout=self._timeout)
        return self._async_http

    def close(self) -> None:
        """Release the pooled HTTP connections.

        Neither client was ever closeable before — fine for a callback meant to
        live for the process lifetime, but there was no way to release the pool
        for a caller who does want to tear one down (tests, a short-lived
        script that calls `enable()` per run). The async client's close is
        scheduled rather than awaited: `enable()`'s hooks are the only async
        caller, `litellm.acompletion()` runs them from inside a loop, and this
        method itself is sync so there is no `await` to offer here.
        """
        if not self._sync_http.is_closed:
            self._sync_http.close()
        if self._async_http is not None and not self._async_http.is_closed:
            try:
                import asyncio

                asyncio.get_running_loop().create_task(self._async_http.aclose())
            except RuntimeError:
                # No running loop (e.g. interpreter shutdown, or a sync-only
                # caller that never used acompletion) — nothing will ever poll
                # this connection again, so leaking the fd is the only option
                # short of a blocking close from possibly the wrong thread.
                pass

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
        body = "# Relevant Memories\n" + "\n".join(
            f"{i + 1}. {r.get('content', '')}" for i, r in enumerate(memories)
        )
        return f"{_BLOCK_START}\n{body}\n{_BLOCK_END}"

    @staticmethod
    def _without_previous_block(text: str) -> str:
        """Drop a block `_inject` wrote on an earlier call, if any.

        `_inject` mutates the caller's own message object in place, and the
        common multi-turn pattern is to keep one `messages` list across calls
        (`messages.append(...); litellm.completion(messages=messages)`) — so
        without this, the system/user message that already carries a block
        would get a second one concatenated onto it on every subsequent turn
        instead of the stale one being replaced.
        """
        start = text.find(_BLOCK_START)
        if start == -1:
            return text
        end = text.find(_BLOCK_END, start)
        if end == -1:
            return text
        return (text[:start] + text[end + len(_BLOCK_END):]).strip()

    @staticmethod
    def _with_block(content: Any, block: str, *, prepend: bool) -> Any:
        """Fold `block` into a message's `content`, replacing any prior block.

        `content` is not guaranteed to be a string — a multimodal message
        (image + text parts) carries a list of `{"type": ..., ...}` dicts, and
        `block + content` / `content + block` raised TypeError on that shape
        before this existed. A list gets the block as its own text part
        instead; a string gets the block spliced in as before.
        """
        if isinstance(content, list):
            parts = [
                p for p in content
                if not (
                    isinstance(p, dict)
                    and p.get("type") == "text"
                    and _BLOCK_START in (p.get("text") or "")
                )
            ]
            text_part = {"type": "text", "text": block}
            return [text_part] + parts if prepend else parts + [text_part]
        text = AnonaMemory._without_previous_block(content or "")
        if not text:
            return block
        return f"{block}\n\n{text}" if prepend else f"{text}\n\n{block}"

    def _inject(self, messages: list[dict], memories: list[dict]) -> None:
        if not memories:
            return
        block = self._build_block(memories)
        if self._inject_mode == "system":
            sys_msgs = [m for m in messages if m.get("role") == "system"]
            if sys_msgs:
                msg = sys_msgs[-1]
                msg["content"] = self._with_block(msg.get("content"), block, prepend=False)
            else:
                messages.insert(0, {"role": "system", "content": block})
        else:
            user_msgs = [m for m in messages if m.get("role") == "user"]
            if user_msgs:
                msg = user_msgs[-1]
                msg["content"] = self._with_block(msg.get("content"), block, prepend=True)

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
        # strip it back off so the stored turn is the human's actual question,
        # not a copy of the memories that were just injected for it.
        if isinstance(user_msg, str):
            user_msg = self._without_previous_block(user_msg)
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
        # Append rather than replace, so a caller's own callbacks survive.
        # Dedup on this instance's own callback object, not its class name —
        # every instance's callback is named "_Callback" (see class
        # docstring), so a classname check treats a second AnonaMemory's
        # first-ever enable() as a duplicate of the first instance's. Identity
        # still catches the real duplicate case: calling enable() again on the
        # same instance, or after the caller cleared litellm.callbacks (in
        # which case re-adding the same object is correct, not a leak).
        if self._callback is None:
            self._callback = _Callback()
        if self._callback not in litellm.callbacks:
            litellm.callbacks.append(self._callback)
        logger.info(
            "Anona Memory enabled for space '%s' (limit=%d, store=%s)",
            anona._space_id,
            anona._recall_limit,
            anona._store_after,
        )
