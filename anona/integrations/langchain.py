"""LangChain and LangGraph adapters.

Two seams, because LangChain has two shapes worth supporting:

* :class:`AnonaRetriever` for retrieval chains — memory as a retriever.
* :class:`AnonaMemory` for ``create_agent`` — memory injected before the model
  runs and the turn stored after.

Deliberately **not** a LangGraph ``BaseStore``. A Store is namespaced
key-value: ``get(namespace, key)`` must return one exact record. Anona is
search. Implementing the half we can and raising on ``get`` would break
LangGraph's own memory tools at runtime inside somebody's agent, so we offer
the two shapes that map cleanly instead.
"""
from __future__ import annotations

from ._core import MemoryBridge, require


def _msg_role(msg) -> str | None:
    """The role of one message, across dict and LangChain message types."""
    return getattr(msg, "type", None) or (
        msg.get("role") if isinstance(msg, dict) else None
    )


def _msg_text(msg) -> str:
    """Text content of one message, across dict and LangChain message types.

    ``BaseMessage.content`` is typed ``str | list[str | dict]`` — a real
    LangChain message can carry multimodal content blocks, not just a plain
    string; this is the standard shape, not an edge case. ``.text`` is
    ``langchain_core``'s own normalizer for that split (it returns a ``str``
    subclass either way, so ``or ""``/f-string/``.strip()`` usage downstream
    is unaffected). A plain dict — the OpenAI wire shape the rest of this
    module also accepts — has no ``.text`` attribute, so it falls through to
    ``.get("content")`` unchanged. Reading raw ``.content`` instead of
    ``.text`` was a real bug here: a list-content message silently produced
    no memories on read, and its ``repr()`` got written to storage on record.
    """
    text = getattr(msg, "text", None)
    if text is None:
        text = msg.get("content") if isinstance(msg, dict) else None
    return text or ""


def _existing_system_text(system_message) -> str:
    """Text of a request's current system message, whatever shape it's in.

    ``ModelRequest.system_message`` is documented as ``SystemMessage | None``,
    but ``create_agent``'s own ``system_prompt`` kwarg is typed
    ``str | SystemMessage | None`` — handle all three so a caller's system
    prompt is never silently dropped. ``_msg_text`` can't be reused as-is for
    the ``str`` case: a plain ``str`` has no ``.text`` attribute either, so
    ``getattr(msg, "text", None)`` falls through to the dict branch and drops
    it, the same failure mode this function exists to avoid.
    """
    if system_message is None:
        return ""
    if isinstance(system_message, str):
        return system_message
    return _msg_text(system_message)


def _last_user_text(messages: list) -> str:
    """The most recent user message, across dict and LangChain message types."""
    for msg in reversed(messages or []):
        if _msg_role(msg) in ("user", "human"):
            return _msg_text(msg)
    return ""


def _turn_text(messages: list) -> str:
    """The last user/assistant exchange, formatted for storage."""
    user = _last_user_text(messages)
    assistant = ""
    for msg in reversed(messages or []):
        if _msg_role(msg) in ("assistant", "ai"):
            assistant = _msg_text(msg)
            break
    if not user:
        return ""
    return f"User: {user}\nAssistant: {assistant}" if assistant else f"User: {user}"


def AnonaRetriever(bridge: MemoryBridge):
    """Anona as a LangChain retriever.

    A factory, not a class — hence the class-style name. The base class cannot
    be referenced until ``langchain_core`` is imported, and importing it at
    module level would make ``import anona`` require langchain. Defining the
    subclass inside the factory defers that, and closing over ``bridge``
    sidesteps ``BaseRetriever`` being a pydantic model, where an undeclared
    ``self._bridge`` attribute would be rejected.

    Returns the whole context block as a single ``Document`` rather than one
    per memory: the block is already ranked, deduplicated and token-budgeted
    server-side, and splitting it back apart would discard that.
    """
    retrievers = require("langchain_core.retrievers", "langchain")
    documents = require("langchain_core.documents", "langchain")
    Document = documents.Document

    class _AnonaRetriever(retrievers.BaseRetriever):
        async def _aget_relevant_documents(self, query: str, **kwargs) -> list:
            block = await bridge.context(query)
            if not block:
                return []
            return [Document(page_content=block, metadata={"source": "anona"})]

        def _get_relevant_documents(self, query: str, **kwargs) -> list:
            raise NotImplementedError(
                "AnonaRetriever is async-only; use ainvoke() rather than invoke()."
            )

    return _AnonaRetriever()


def AnonaMemory(bridge: MemoryBridge, *, record: bool = True):
    """Anona as ``create_agent`` middleware.

    A factory, for the same reason as :func:`AnonaRetriever`.

    Three hooks, deliberately **not** ``before_model``/``after_model``:

    * ``abefore_agent`` seeds a per-run context cache — see "Caching" below.
    * ``awrap_model_call`` composes the context block into the outgoing
      request's ``system_message`` rather than writing a message into
      ``state["messages"]``. ``create_agent``'s default state schema merges
      that channel with LangGraph's ``add_messages`` reducer, which
      reconciles by message ``.id``: an injected dict has none, so the
      reducer treats it as new and appends it, landing *after* the existing
      conversation instead of before it — verified against the installed
      API (``langchain==1.3.14``) by replaying the reducer on
      ``before_model``'s own return value and by inspecting what a compiled
      graph actually sent the model. A ``ModelRequest``'s ``system_message``
      is a per-call field, not a state channel, so the reducer never touches
      it.
    * ``aafter_agent`` stores the turn once the whole agent run has
      finished. ``after_model`` fires once per *model step*, so one
      tool-calling turn (model → tool → model) called it twice: once after
      the tool-call-only message (empty content, storing an orphaned
      answerless fragment as its own memory) and once after the real
      answer. ``after_agent`` is the framework's own once-per-turn hook —
      measured firing exactly once across a two-step tool call against the
      installed API.

    Both memory operations fail open — a memory outage leaves the agent
    running without memory.

    **Compose, don't replace, the system message.** An earlier version did
    ``request.override(system_message=SystemMessage(content=block))``
    unconditionally. ``ModelRequest.override()`` is a plain
    ``dataclasses.replace`` — it replaces the field rather than merging —
    so that silently discarded a caller's own ``create_agent(system_prompt=
    ...)``: the model received only the memory block, with no error, no
    warning, and no log. Fixed by reading ``request.system_message`` first
    (handling ``None``, ``str`` and ``SystemMessage``, since
    ``system_prompt`` itself is typed to accept all three) and concatenating
    the caller's instructions ahead of the memory block, verified end to end
    against a real compiled graph with ``system_prompt=`` set.

    **Caching.** ``awrap_model_call`` fires once per *model step*, and in a
    tool-calling loop the query text does not change between steps — so
    without caching, a single turn re-fetches (and re-bills) the identical
    retrieve call once per step, serially blocking each step on the slowest
    call in the system. The block is cached for the lifetime of one agent
    run, keyed on the query text, in a middleware-private ``state_schema``
    field (``_anona_context_cache``) seeded fresh by ``abefore_agent`` on
    every run. This needed the declared-state-field route specifically:
    mutating ``request.state`` directly from ``awrap_model_call`` doesn't
    persist to the next step (each step gets an independently reconstructed
    view), a plain ``contextvars.ContextVar`` doesn't propagate between hook
    invocations under LangGraph's task-based scheduling, and keying an
    instance-level cache by ``id(request.runtime)`` is unsound — that
    object's identity is stable across the steps of one run, but not
    reliably distinct across separate ones, since Python recycles freed
    addresses. A *declared* state field, by contrast, is reset by
    ``abefore_agent`` at the start of every run (never carrying a stale
    value into a later turn or, on a shared middleware instance, a
    concurrent one) and is the same live object across every step of that
    run (so an update from step one is visible in step two) — both
    confirmed against the installed API, including under concurrent runs on
    one shared middleware instance.
    """
    middleware = require("langchain.agents.middleware", "langchain")
    messages_mod = require("langchain_core.messages", "langchain")
    SystemMessage = messages_mod.SystemMessage

    _CACHE_KEY = "_anona_context_cache"

    class _AnonaState(middleware.AgentState, total=False):
        _anona_context_cache: dict

    class _AnonaMemory(middleware.AgentMiddleware):
        state_schema = _AnonaState

        async def abefore_agent(self, state, runtime=None):
            return {_CACHE_KEY: {}}

        async def awrap_model_call(self, request, handler):
            query = _last_user_text(request.messages)
            cache = (
                request.state.get(_CACHE_KEY)
                if isinstance(request.state, dict)
                else None
            )
            if cache is not None and query in cache:
                block = cache[query]
            else:
                block = await bridge.context(query)
                if cache is not None:
                    cache[query] = block
            if block:
                existing = _existing_system_text(request.system_message)
                combined = f"{existing}\n\n{block}" if existing else block
                request = request.override(
                    system_message=SystemMessage(content=combined)
                )
            return await handler(request)

        async def aafter_agent(self, state, runtime=None):
            if not record:
                return state
            messages = state.get("messages", []) if isinstance(state, dict) else []
            await bridge.remember(_turn_text(messages))
            return state

    return _AnonaMemory()
