"""Shared core for the framework adapters.

Every adapter is a translation layer over exactly two logical operations:
fetch a prompt-ready context block before the model runs, and store the turn
after it does — each available as an async method and a synchronous one, so
this module owns both. An adapter never talks to the API directly and the
fail-open policy is decided in one place instead of six.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import logging
from types import ModuleType
from typing import Any, Coroutine, TypeVar

from ..client import AnonaClient

logger = logging.getLogger("anona.integrations")

_T = TypeVar("_T")


def _sync(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run a coroutine from sync code, loop or no loop.

    Used only by :meth:`MemoryBridge.close` to run the async client's
    teardown. CrewAI and Strands used to be driven through this too, but a
    fresh ``asyncio.run`` loop per call is unsound for repeated sequential
    use against a pooled ``httpx.AsyncClient``: the pool's keep-alive
    connection stays bound to whichever loop created it, so the next call's
    new loop inherits a connection it cannot use and fails every other call,
    deterministically (see :meth:`MemoryBridge.context_sync`). Both adapters
    now call the bridge's synchronous methods directly instead, which never
    touch an event loop at all. ``close()`` is a one-shot call per bridge
    lifetime rather than a repeated one, so it does not hit that particular
    failure mode — but it can still fail if the loop it runs on cannot reach
    the resource it is closing (see ``close()``'s own docstring), which is
    why ``close()`` wraps this in a ``try``/``except`` rather than trusting
    it unconditionally.

    ``asyncio.run`` raises if a loop is already running, which it might be if
    ``close()`` is called from inside an async context without ``await``, so
    that case gets its own thread.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def require(module: str, extra: str) -> ModuleType:
    """Import a framework, or explain which extra installs it.

    Adapters call this inside ``__init__`` rather than at module import, so
    ``anona`` stays importable without any framework installed.
    """
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ImportError(
            f"{module} required: pip install 'anona[{extra}]'"
        ) from exc


class MemoryBridge:
    """Anona, reduced to the two operations an agent framework needs — recall
    and store — each with an async form (:meth:`context`/:meth:`remember`)
    and a synchronous one (:meth:`context_sync`/:meth:`remember_sync`). Four
    adapters (LangChain, LlamaIndex, Google ADK, Microsoft Agent Framework)
    are themselves async and drive the async pair directly; CrewAI and
    Strands are themselves synchronous interfaces and drive the sync pair —
    see :meth:`context_sync` for why that is a distinct code path rather
    than the async methods run through a helper.

    Every one of the four **fails open**: if Anona is unreachable the agent
    runs without memory rather than raising into somebody's production
    agent. A missing ``api_key`` or ``space_id`` is the one thing that
    raises, because that is a typo at wiring time, not an outage — and
    silently meaning "no memory, forever" is worse than a stack trace on
    line one.
    """

    def __init__(
        self,
        api_key: str,
        space_id: str,
        *,
        base_url: str = "https://api.anonalabs.com",
        user_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        limit: int = 10,
        max_tokens: int | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if not space_id:
            raise ValueError("space_id is required")
        self._client = AnonaClient(api_key=api_key, base_url=base_url)
        self._space_id = space_id
        self._scope = {
            "user_id": user_id,
            "agent_id": agent_id,
            "session_id": session_id,
        }
        self._limit = limit
        self._max_tokens = max_tokens

    def _effective_scope(
        self,
        user_id: str | None,
        agent_id: str | None,
        session_id: str | None,
    ) -> dict:
        """Construction-time scope, with any per-call value taking precedence.

        Most frameworks fix the scope when the agent is wired up. Google ADK
        hands us ``user_id`` and ``app_name`` on every single call instead, so
        the per-call form has to win — without an adapter reaching into private
        state to get it.
        """
        override = {
            "user_id": user_id,
            "agent_id": agent_id,
            "session_id": session_id,
        }
        return {
            key: (override[key] if override[key] is not None else default)
            for key, default in self._scope.items()
        }

    async def context(
        self,
        query: str,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """Relevant memories as one prompt-ready string. ``""`` if none.

        The emptiness check lives inside the ``try`` along with the call
        itself: a truthy non-string (an adapter forwarding a framework
        message object instead of extracted text, say) fails ``.strip()``
        with an ``AttributeError`` that must fail open exactly like a network
        error would, not escape as a raise.
        """
        try:
            if not query or not query.strip():
                return ""
            return await self._client.async_get_context(
                space_id=self._space_id,
                query=query,
                limit=self._limit,
                max_tokens=self._max_tokens,
                **self._effective_scope(user_id, agent_id, session_id),
            )
        except Exception:
            logger.warning(
                "anona: memory search failed — continuing without memories",
                exc_info=True,
            )
            return ""

    def context_sync(
        self,
        query: str,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """Synchronous sibling of :meth:`context`, for synchronous adapters.

        CrewAI and Strands are themselves synchronous interfaces (a tool
        function returns a plain value, not a coroutine), so something has
        to bridge them onto this async core. The tempting bridge —
        ``_sync(self.context(...))``, i.e. ``asyncio.run`` per call — is
        exactly what this method exists to avoid: ``AnonaClient``'s async
        HTTP client is created once and reused for the bridge's whole
        lifetime, so its pooled keep-alive connection ends up bound to
        whichever event loop happened to be running on the *first* call.
        ``asyncio.run`` tears its loop down when the coroutine returns, so
        the *second* sequential call — on a new loop — inherits a
        connection bound to a now-dead one and fails. Measured at exactly
        every other call, deterministically; not a race. The failure lands
        inside this class's own ``except Exception`` below, which is the
        dangerous part: it fails open into a result indistinguishable from
        "no memories found", while the API has already executed (and
        billed) the call.

        This method sidesteps the problem instead of working around it: it
        drives :class:`AnonaClient`'s plain synchronous methods, which use a
        plain ``httpx.Client`` over real sockets with no event loop involved
        at all, so there is no loop for a pooled connection to outlive.
        Same fail-open contract as :meth:`context` — never raises except the
        constructor's own validation.
        """
        try:
            if not query or not query.strip():
                return ""
            return self._client.get_context(
                space_id=self._space_id,
                query=query,
                limit=self._limit,
                max_tokens=self._max_tokens,
                **self._effective_scope(user_id, agent_id, session_id),
            )
        except Exception:
            logger.warning(
                "anona: memory search failed — continuing without memories",
                exc_info=True,
            )
            return ""

    async def remember(
        self,
        text: str,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Store one turn. Never raises.

        Same reasoning as :meth:`context`: the emptiness check is inside the
        ``try`` so a truthy non-string ``text`` fails open through the same
        ``except`` instead of raising ``AttributeError`` out of "never
        raises".
        """
        try:
            if not text or not text.strip():
                return
            await self._client.async_record(
                space_id=self._space_id,
                content=text,
                **self._effective_scope(user_id, agent_id, session_id),
            )
        except Exception:
            logger.warning("anona: failed to store turn", exc_info=True)

    def remember_sync(
        self,
        text: str,
        *,
        user_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Synchronous sibling of :meth:`remember`, for synchronous adapters.

        See :meth:`context_sync` for why this exists as its own method
        rather than ``_sync(self.remember(...))``. Same fail-open contract:
        never raises.
        """
        try:
            if not text or not text.strip():
                return
            self._client.record(
                space_id=self._space_id,
                content=text,
                **self._effective_scope(user_id, agent_id, session_id),
            )
        except Exception:
            logger.warning("anona: failed to store turn", exc_info=True)

    def close(self) -> None:
        """Close the underlying HTTP client(s). Never raises.

        Both the sync half (opened by :meth:`context_sync`/
        :meth:`remember_sync`) and the async half (opened by :meth:`context`/
        :meth:`remember`) can each end up holding an open connection pool,
        depending on which methods were actually called on this bridge — so
        both get a teardown attempt: ``AnonaClient.close()`` for the plain
        ``httpx.Client``, and ``_sync(self._client.aclose())`` for the
        ``httpx.AsyncClient``. Closing a client that was never opened is
        already a safe no-op on ``AnonaClient``.

        The sync half never raises here — it is plain socket I/O, never
        bound to an event loop. The async half is where this used to raise:
        ``aclose()`` has to run on the event loop that created the pooled
        connection, and by the time ``close()`` is called that loop has
        often already finished (the ordinary shape is ``asyncio.run(...)``
        completes, *then* cleanup runs). ``_sync`` hands the coroutine a new
        loop regardless, so the attempt itself always starts — but the
        underlying ``aclose()`` can still fail against the dead loop's
        orphaned locks/transport (``RuntimeError: Event loop is closed``, or
        "bound to a different event loop" if that first loop is still alive
        elsewhere). There is no way to force a clean async teardown from a
        foreign loop after the fact, so once that happens the connection is
        past this method's reach — the same fail-open posture ``context()``/
        ``remember()`` take applies here too: log it and return rather than
        raise into whatever cleanup code called ``close()``, often a
        ``finally`` block, the worst possible place for a new exception to
        appear.
        """
        self._client.close()
        try:
            _sync(self._client.aclose())
        except Exception:
            logger.warning(
                "anona: could not cleanly close the async HTTP client — "
                "continuing",
                exc_info=True,
            )
