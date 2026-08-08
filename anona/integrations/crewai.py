"""CrewAI memory tools.

**Reality diverged from the original plan here, so read this before changing
anything.** The plan assumed CrewAI still exposed its old (pre-1.0)
``ExternalMemory`` plus a text-in/text-out ``Storage`` ABC
(``save(value, metadata)`` / ``search(query, limit, score_threshold)`` /
``reset()``) that a customer could drop into ``Crew(memory=ExternalMemory(
storage=...))``. Verified against the installed package (crewai 1.15.13) and
against CrewAI's current published docs: **that class does not exist
anymore.** ``crewai.memory.external`` is gone; grepping the installed
package for ``ExternalMemory`` or ``external_memory`` returns nothing.

CrewAI replaced it with a single ``Memory`` class
(``crewai.memory.unified_memory``) that does its own LLM-driven content
analysis and embedding, backed by a pluggable
``crewai.memory.storage.backend.StorageBackend``. That protocol is a raw
vector store, not a text search API:

* ``StorageBackend.search(query_embedding: list[float], ...)`` receives an
  **already-computed embedding vector, never the query text.** Confirmed by
  reading ``Memory.recall()`` and ``RecallFlow`` end to end: the query string
  is embedded (``embed_text``/``embed_texts``) before ``storage.search()`` is
  ever called, and the flow's own state only threads the embedding through
  from there — the plaintext does not survive to the storage boundary. A
  backend cannot forward a real query to Anona's ``/v1/retrieve`` (which
  needs text) from inside ``search()``, because the text is gone by the time
  ``search()`` runs.
* ``StorageBackend.save()`` receives ``MemoryRecord``s that CrewAI's own LLM
  has already synthesized from the raw task/result text (via
  ``Memory.extract_memories`` and per-item field inference in
  ``EncodingFlow``) — not the original text.
* Both of the above require the customer to configure ``Memory(llm=...,
  embedder=...)`` (default: OpenAI) **regardless of which storage backend is
  plugged in.** That is a CrewAI-side requirement of the new ``Memory``
  class itself, not something a storage backend can opt out of.

None of that composes with Anona: no embeddings are exposed to callers (or
should be — that vector store is exactly what Anona exists to remove), and
Anona holds the LLM keys, not the customer. A ``StorageBackend`` wired into
``Memory(storage=...)`` could accept writes, but its ``search()`` could only
ever return nothing, silently — memory that is never recalled isn't memory,
it's a one-way log, and shipping that as "CrewAI memory" would be worse than
not shipping this adapter at all.

This mirrors two decisions already made elsewhere in this same plan: the
LangChain adapter skips LangGraph's ``BaseStore`` because Anona is search,
not exact key-value lookup; the Strands adapter ships tools instead of a
``SessionManager`` because Strands has no memory interface to implement
faithfully. Same judgment here: **CrewAI tools** — ``@tool``-decorated
functions an agent calls explicitly — are the one seam in current CrewAI
that is still plain text in, plain text out, at every hop, with no
CrewAI-side LLM or embedder requirement at all. :meth:`AnonaStorage.as_tools`
is the integration point this module actually recommends; see
``mintlify-docs/integrations/crewai.mdx``.

``AnonaStorage`` itself is kept — plain, synchronous, text-based
``save``/``search``/``reset`` — both because it is a clean, directly
testable unit the tools above are built from, and because it is the public
symbol this task's interface commits to.
"""
from __future__ import annotations

import logging
from typing import Any

from ._core import MemoryBridge, require

logger = logging.getLogger("anona.integrations")


def _as_text(value: Any) -> str | None:
    """``value`` if it is a non-empty string, else ``None``.

    Guards ``save``/``search`` against a caller handing over something other
    than a string (a dict, a list of content blocks, ...). The tool path
    (:meth:`AnonaStorage.as_tools`) already can't reach this — each tool
    declares a plain ``str`` argument, and CrewAI validates the model's
    tool-call arguments against that before ``_run`` ever executes, so a
    non-string value is rejected as a tool-execution error the agent sees
    and can retry, never silently stored or searched. This guard exists for
    direct callers of :class:`AnonaStorage`, so a non-string can never reach
    ``MemoryBridge`` and get turned into a stringified-garbage memory (the
    LangChain adapter's worst bug: a raw list stored verbatim as
    ``"User: [{'type': 'text', 'text': '...'}]"``). ``MemoryBridge`` itself
    would also fail open on a non-string (its own ``.strip()`` check raises
    and is caught), but that path logs a generic warning with a full
    traceback for what is really just an expected shape mismatch — this
    guard is the intentional, documented version of the same contract.
    """
    return value if isinstance(value, str) and value.strip() else None


class AnonaStorage:
    """Anona as a plain search/save helper for CrewAI agents.

    Use :meth:`as_tools` to give an agent working, Anona-backed recall — see
    the module docstring for why that is the recommended integration and
    ``crewai.memory.storage.backend.StorageBackend`` is not.

    Usage::

        from crewai import Agent
        from anona.integrations import MemoryBridge
        from anona.integrations.crewai import AnonaStorage

        bridge = MemoryBridge(api_key="anona_live_...", space_id="research-crew")
        storage = AnonaStorage(bridge=bridge)

        agent = Agent(
            role="Researcher",
            goal="...",
            backstory="...",
            tools=[*existing_tools, *storage.as_tools()],
        )
    """

    def __init__(self, bridge: MemoryBridge) -> None:
        require("crewai", "crewai")
        self._bridge = bridge

    def save(self, value: Any, metadata: dict | None = None) -> None:
        """Store ``value`` as a memory. ``metadata`` is accepted, not sent —
        not because Anona has nowhere to put it (``/v1/record`` does have a
        ``metadata`` field, and :meth:`AnonaClient.async_record` already
        forwards one), but because :meth:`MemoryBridge.remember`, shared by
        every adapter, does not accept or forward a ``metadata`` parameter.
        Out of scope to change here; noted so the choice reads as deliberate
        rather than as a dropped feature.
        """
        text = _as_text(value)
        if text is None:
            logger.warning("anona: save() got a non-string value — skipping")
            return
        # remember_sync(), not _sync(self._bridge.remember(...)) -- CrewAI
        # tools are called sequentially, and a fresh event loop per _sync()
        # call is unsound for that (see MemoryBridge.context_sync's
        # docstring): every other sequential call silently failed open.
        self._bridge.remember_sync(text)

    def search(
        self,
        query: Any,
        limit: int | None = None,
        score_threshold: float | None = None,
    ) -> list[dict]:
        """Search for memories relevant to ``query``.

        Returns the whole context block as a single result dict — like
        :func:`anona.integrations.langchain.AnonaRetriever`, the block is
        already ranked, deduplicated and token-budgeted server-side.
        ``limit``/``score_threshold`` are accepted for interface familiarity
        but have no Anona equivalent to forward to; the bridge's own
        construction-time ``limit``/``max_tokens`` govern the block.
        """
        text = _as_text(query)
        if text is None:
            return []
        # context_sync(), not _sync(self._bridge.context(...)) -- same
        # reasoning as save() above.
        block = self._bridge.context_sync(text)
        if not block:
            return []
        return [{"context": block, "metadata": {"source": "anona"}}]

    def reset(self) -> None:
        """Deliberately a no-op.

        CrewAI calls ``reset()`` on some internal paths, so raising would
        crash a working crew. Anona has no delete-all endpoint, and a
        list-then-delete loop has no transaction — a half-failed reset
        leaves an arbitrary subset behind, which is worse than not
        resetting at all.
        """
        logger.warning(
            "anona: reset() is not supported — clear the space from the Anona "
            "dashboard, or delete and recreate it."
        )

    def as_tools(self) -> list[Any]:
        """Two CrewAI tools — search and save — an agent can call directly.

        A factory method rather than module-level ``BaseTool`` subclasses:
        the framework's tool base cannot be referenced until ``crewai`` is
        imported, and importing it at module scope would make ``import
        anona`` require crewai. The closures below capture ``self`` (already
        constructed, so already past the ``require()`` gate) instead of
        storing it as a field, sidestepping ``BaseTool`` being a pydantic
        model where an undeclared attribute would be rejected — the same
        reasoning ``anona.integrations.langchain.AnonaRetriever`` uses for
        the same kind of base class.

        Verified against a real, compiled crew (crewai 1.15.13): a scripted
        agent that calls the search tool, then the save tool, then answers,
        produces exactly one ``/v1/retrieve`` and one ``/v1/record`` call,
        each carrying the exact text the agent's tool call supplied — tool
        calls are agent-initiated, not an automatic per-step hook, so there
        is no framework-internal mechanism here that can fire twice for one
        logical action the way LangChain's ``before_model``/``after_model``
        did.

        Names are prefixed ``Anona: `` on purpose. CrewAI auto-injects its
        own memory tools (``crewai/tools/memory_tools.py``,
        ``RecallMemoryTool``/``RememberTool``, named literally ``"Search
        memory"``/``"Save to memory"``) onto every agent whenever
        ``Crew(memory=True)`` or an agent-level ``memory=True`` is set, and
        ``Crew._merge_tools`` dedups by sanitized tool name, keeping
        whichever tool object it was handed *last* for a given name — the
        auto-injected built-in, not ours. An unprefixed ``"Search memory"``
        here previously collided with that built-in and silently lost: a
        customer piloting Anona inside an existing ``memory=True`` crew got
        answers from CrewAI's own local LanceDB/OpenAI-backed memory, with
        no error, no warning, nothing in any log — confirmed both by
        reproducing the merge directly and by running a real crew with both
        tool sets attached. Only the search name actually collided
        (CrewAI's save tool is named ``"Save to memory"``, not ``"Save
        memory"``); both are prefixed anyway so neither can ever be
        shadowed by, or shadow, a future CrewAI built-in.
        """
        require("crewai.tools", "crewai")
        from crewai.tools import tool

        storage = self

        @tool("Anona: Search memory")
        def search_memory(query: str) -> str:
            """Search stored memories for information relevant to the query."""
            results = storage.search(query)
            return results[0]["context"] if results else "No relevant memories found."

        @tool("Anona: Save memory")
        def save_memory(content: str) -> str:
            """Save a fact, decision, or observation as a durable memory."""
            storage.save(content)
            return "Saved to memory."

        return [search_memory, save_memory]
