from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger("anona.litellm")


class AnonaMemory:
    """LiteLLM callback that injects Anona memories before each call and stores Q&A after.

    Usage::

        from anona.integrations.litellm import AnonaMemory

        mem = AnonaMemory(
            api_key="anona_live_...",
            space_id="your-space-id",
            base_url="http://localhost:3001",
        )
        mem.enable()

        # All subsequent litellm.completion() calls auto-inject + auto-store.
    """

    def __init__(
        self,
        api_key: str,
        space_id: str,
        base_url: str = "http://anona-prod-alb-747552680.us-east-1.elb.amazonaws.com",
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

    def enable(self) -> None:
        try:
            import litellm
        except ImportError:
            raise ImportError("litellm required: pip install 'anona[litellm]'")
        litellm.callbacks = [self]

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _search(self, query: str) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self._base_url}/v1/search",
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
            logger.warning("anona: memory search failed — proceeding without injected memories", exc_info=True)
        return []

    async def _store(self, content: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self._base_url}/v1/memories",
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

    # ── LiteLLM hooks ─────────────────────────────────────────────────────────

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        messages = data.get("messages", [])
        user_msgs = [m for m in messages if m.get("role") == "user"]
        if not user_msgs:
            return data

        query = user_msgs[-1].get("content", "")
        if not query:
            return data

        memories = await self._search(query)
        if not memories:
            return data

        block = "# Relevant Memories\n" + "\n".join(
            f"{i + 1}. {r.get('content', '')}" for i, r in enumerate(memories)
        )

        if self._inject_mode == "system":
            sys_msgs = [m for m in messages if m.get("role") == "system"]
            if sys_msgs:
                sys_msgs[-1]["content"] = sys_msgs[-1]["content"] + "\n\n" + block
            else:
                messages.insert(0, {"role": "system", "content": block})
        else:
            user_msgs[-1]["content"] = block + "\n\n" + user_msgs[-1]["content"]

        data["messages"] = messages
        return data

    async def async_post_call_success_hook(
        self, user_api_key_dict, response, kwargs, start_time, end_time
    ):
        if not self._store_after:
            return
        try:
            messages = kwargs.get("messages", [])
            user_msgs = [m for m in messages if m.get("role") == "user"]
            if not user_msgs:
                return

            user_msg = user_msgs[-1].get("content", "")
            if "# Relevant Memories" in user_msg:
                parts = user_msg.split("# Relevant Memories")
                # In "system" mode the user message is unmodified; in "user" mode
                # the memory block was prepended — take the part after the block.
                user_msg = parts[-1].strip() if len(parts) > 1 else parts[0].strip()

            assistant_msg = ""
            if hasattr(response, "choices") and response.choices:
                assistant_msg = response.choices[0].message.content or ""

            if user_msg and assistant_msg:
                content = f"User: {user_msg}\nAssistant: {assistant_msg}"
                asyncio.create_task(self._store(content))
        except Exception:
            logger.warning("anona: failed to prepare conversation turn for storage", exc_info=True)
