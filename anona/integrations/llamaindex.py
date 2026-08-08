"""LlamaIndex memory-block adapter.

Plugs into ``llama_index.core.memory.Memory`` as one block among several, so
Anona supplies long-term memory beside LlamaIndex's own short-term buffer
rather than replacing it. ``BaseMemoryBlock``/``_aget``/``_aput`` are real,
current interfaces — verified against the installed package
(llama-index-core==0.14.23) by driving a real ``Memory`` object and a real
``FunctionAgent`` tool-calling turn, not by reading docstrings. Three things
about the real orchestration are surprising enough to write down.

**A turn is read from at least twice, not once.** ``Memory.aget()`` (hence
every block's ``_aget``) is called once in ``FunctionAgent``'s ``init_run``
before the first model call of a turn, and once more after ``finalize()`` for
internal bookkeeping (structured-output extraction) even when nothing else
consumes that second result. Each additional tool round adds one more call,
before the next model call — a one-tool-round turn measured 3 calls total.
Every call in one turn carries the same extracted query text (no new user
message appears mid-turn), so retrieve — the slowest call in this product,
and metered — gets re-fetched and re-billed 2+ times for every single turn,
not just tool-calling ones.

A memory block has no "run started" hook the way LangChain's
``create_agent`` middleware does (``abefore_agent``), so there is no clean
signal to seed and reset a per-run cache against, and dedup keyed only on
last-extracted query text with no run boundary risks silently serving a
stale answer if two turns in a row happen to ask the identical question.
Rather than accept either the full re-fetch cost or that unbounded staleness
risk, ``_aget`` below is backed by a short (``_CONTEXT_CACHE_TTL_SECONDS``,
a few seconds), single-entry, query-keyed cache: long enough to collapse the
2+ same-turn duplicate calls above (they all land within one turn's own
latency budget), short enough that two genuinely distinct turns essentially
never collide. A different query always evicts the slot outright, so a topic
change can never serve a stale answer regardless of timing — the TTL only
ever governs *repeats* of the identical query. Bounding a cache by time
rather than by an explicit boundary is the same general idea as the
API's own recall cache, though that one is server-side, keyed on the
full request body, and TTL'd in minutes for a different reason (avoiding
recomputation across callers) — not a mechanism shared with this adapter,
just a precedent for "TTL over exact invalidation" being an accepted
trade-off in this codebase.

**A turn is not written on completion.** ``_aput`` is not a per-turn hook.
``Memory`` keeps its own short-term FIFO (a SQL-backed buffer sized by
``token_limit`` / ``chat_history_token_ratio``, 30,000 tokens / 70% by
default) and only waterfalls the oldest messages out to each block's
``_aput`` once that buffer's token budget is exceeded — the exact contract
LlamaIndex's own built-in blocks (``VectorMemoryBlock``,
``FactExtractionMemoryBlock``) are written against: a memory block is
long-term storage for what falls out of the short-term window, not a
mirror of every turn. Two consequences, both verified against a real
``FunctionAgent``: a short conversation may never cross the default
21,000-token threshold, in which case this block's ``_aput`` never fires and
nothing reaches Anona; and when it does fire, LlamaIndex's own eviction logic
always flushes whole turns together — a forced-eviction run across 8 turns
(4 of them tool calls) produced 7 flushed batches, every one starting on a
user message, the tool-call turns arriving as one atomic batch (question,
empty tool-call message, tool result, final answer) rather than split across
separate ``_aput`` calls. That already-grouped shape is why ``_turn_text``
below loops over every message in the batch instead of assuming exactly one
exchange.

That "keep at least one complete turn" guarantee has a corollary worth
stating plainly: it means the *most recently added* turn is never evicted by
itself, at any ``token_limit`` — confirmed by forcing eviction down to a
handful of tokens across several turns and finding the newest turn always
still resident. ``Memory`` has no flush-now or close method, so **the last
turn of a conversation is never stored by this mechanism at all**, unless
another turn is added after it. See
``mintlify-docs/integrations/llamaindex.mdx`` for the pattern that closes
this gap (call ``bridge.remember(...)`` directly at your own turn/session
boundary).

**The default in-memory store does not survive across separate agent runs.**
``Memory.from_defaults()`` with no ``async_database_uri`` defaults to
``sqlite+aiosqlite:///:memory:``. Verified: reusing the same ``Memory``
object across two separate ``agent.run()`` calls (the normal one-run-per-turn
usage) starts the short-term buffer over from empty on the second call —
passing the same ``ctx=`` across calls does not help. A file (or real
database) URI does not have this problem, because a fresh connection still
opens the same durable store. This is a property of how ``Memory`` and
LlamaIndex's agent workflow pass state between runs, not of this block, but
it directly gates whether the buffer above ever accumulates enough to reach
``_aput`` at all — see ``mintlify-docs/integrations/llamaindex.mdx``.

Content shape needed no workaround: ``ChatMessage.content`` is a real
property (not the raw field), already joins multi-``TextBlock`` messages with
``\\n`` and returns ``None`` for an image-only message — never a stringified
list — so the plain ``getattr(message, "content", None)`` below is safe as
written; verified directly against both shapes.
"""
from __future__ import annotations

import importlib.metadata
import time

from ._core import MemoryBridge, require

# How long AnonaMemoryBlock._aget will keep re-serving the last query's
# result for a repeat of that exact query, instead of calling Anona again.
# Long enough to span the 2+ same-turn calls documented in the module
# docstring (they all land within one turn's own model/tool latency); short
# enough that two genuinely separate turns essentially never land inside the
# same window.
_CONTEXT_CACHE_TTL_SECONDS = 5.0


def _text(message) -> str:
    """Text content of one message, across ``ChatMessage`` and dict shapes.

    ``ChatMessage.content`` is a property, not the raw field — it already
    concatenates the message's ``TextBlock``s (joined with newlines) and
    returns ``None`` for a message with no text blocks (an image-only
    message, say), never a raw block list. That means this needs no
    LangChain-style ``.text``-vs-``.content`` split: reading ``.content``
    directly cannot hand a non-string down to ``MemoryBridge`` here.
    """
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    return content or ""


def _role(message) -> str:
    """The role of one message, across ``ChatMessage`` and dict shapes.

    ``ChatMessage.role`` is a ``MessageRole`` enum (``.value`` is the plain
    string); a dict message carries a plain string already. ``"human"``/
    ``"ai"`` are matched below alongside ``"user"``/``"assistant"`` purely
    for parity with the dict fallback other adapters share — real
    ``ChatMessage.role`` values seen from a live agent are exactly
    ``system``/``user``/``assistant``/``tool``.
    """
    role = getattr(message, "role", None)
    if role is None and isinstance(message, dict):
        role = message.get("role")
    return str(getattr(role, "value", role) or "")


def _last_user_text(messages) -> str:
    """The most recent user message's text, or ``""``."""
    for msg in reversed(list(messages or [])):
        if _role(msg) in ("user", "human"):
            return _text(msg)
    return ""


def _turn_text(messages) -> str:
    """Every user/assistant message in ``messages``, formatted for storage.

    Loops over the whole batch rather than picking out exactly one exchange:
    a real flush from ``Memory``'s short-term buffer can (and, verified,
    does) carry more than one message pair — a whole tool round-trip's
    question/tool-call/tool-result/answer in one call, or several older
    turns evicted together. Tool-call and tool-result messages are skipped
    (empty content, or a role that matches neither branch) rather than
    stored verbatim; the final assistant answer is what is worth keeping,
    same as the LangChain adapter's ``_turn_text``.
    """
    lines = []
    for msg in list(messages or []):
        role = _role(msg)
        body = _text(msg)
        if not body:
            continue
        if role in ("user", "human"):
            lines.append(f"User: {body}")
        elif role in ("assistant", "ai"):
            lines.append(f"Assistant: {body}")
    return "\n".join(lines)


def AnonaMemoryBlock(bridge: MemoryBridge, *, name: str = "anona"):
    """Anona as a LlamaIndex memory block.

    A factory, not a class — hence the class-style name. ``BaseMemoryBlock``
    is a pydantic model (confirmed: ``BaseModel, Generic[T]``), so the
    subclass must be defined after the guarded import and instantiated
    through pydantic's own constructor; closing over ``bridge`` keeps it out
    of the model's field set rather than assigning ``self._bridge`` — the
    same reasoning ``anona.integrations.langchain.AnonaRetriever`` uses for
    its own pydantic base class.

    ``priority`` is left at its default of ``0`` ("never truncate", per
    ``BaseMemoryBlock``'s own field): ``bridge.context()`` already returns a
    ranked, deduplicated, token-budgeted block from the server, and letting
    ``Memory`` truncate it further under local token pressure would cut it
    at an arbitrary point picked without that ranking.

    See the module docstring for what was verified about call cadence
    (``_aget`` fires at least twice per turn; ``_aput`` fires only when
    LlamaIndex's own short-term buffer overflows, not once per turn) and why
    a durable ``async_database_uri`` matters for ``Memory`` in real use.

    Usage::

        from llama_index.core.memory import Memory
        from anona.integrations import MemoryBridge
        from anona.integrations.llamaindex import AnonaMemoryBlock

        bridge = MemoryBridge(api_key="anona_live_...", space_id="assistant")
        memory = Memory.from_defaults(
            session_id="chat-1",
            memory_blocks=[AnonaMemoryBlock(bridge=bridge)],
        )
    """
    blocks = require("llama_index.core.memory", "llamaindex")
    if not hasattr(blocks, "BaseMemoryBlock"):
        # require() only catches ImportError from the module import itself;
        # llama_index.core.memory imports fine on versions older than
        # 0.12.35, it just doesn't have BaseMemoryBlock/Memory yet (still the
        # pre-block ChatMemoryBuffer/VectorMemory API) - left unguarded that
        # is an AttributeError two lines down, not the friendly "pip install"
        # message require() exists to produce. Verified against a real 0.14.23
        # install and against the installed distribution's own reported
        # version below; not verified against 0.12.35 itself, so the message
        # points at the floor that has (>=0.14).
        try:
            installed = importlib.metadata.version("llama-index-core")
        except importlib.metadata.PackageNotFoundError:
            installed = "unknown"
        raise ImportError(
            "llama_index.core.memory.BaseMemoryBlock not found (installed "
            f"llama-index-core=={installed}). This adapter needs "
            "llama-index-core>=0.14 (Memory/BaseMemoryBlock were added in "
            "0.12.35; older 0.12.x releases satisfy a looser floor but don't "
            "have them) - upgrade with: pip install 'llama-index-core>=0.14'"
        )

    # Single-entry, query-keyed, time-bounded cache -- see the module
    # docstring's "read from at least twice" section for why this exists and
    # why it is shaped this way rather than reset on a run boundary (there is
    # no run-boundary hook to reset it on). A plain closure variable, not a
    # pydantic field: BaseMemoryBlock is a pydantic model (see below), and
    # this needs no more persistence than the process this instance lives in.
    cache: dict = {"query": None, "value": None, "at": 0.0}

    class _AnonaMemoryBlock(blocks.BaseMemoryBlock[str]):
        async def _aget(self, messages=None, **kwargs) -> str:
            query = _last_user_text(messages)
            now = time.monotonic()
            if query and query == cache["query"] and (
                now - cache["at"] < _CONTEXT_CACHE_TTL_SECONDS
            ):
                return cache["value"]
            value = await bridge.context(query)
            cache["query"], cache["value"], cache["at"] = query, value, now
            return value

        async def _aput(self, messages) -> None:
            await bridge.remember(_turn_text(messages))

    return _AnonaMemoryBlock(name=name)
