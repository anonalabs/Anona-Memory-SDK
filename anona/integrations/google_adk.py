"""Google ADK memory-service adapter.

ADK passes ``app_name`` and ``user_id`` on every memory call, so this is the
one adapter where per-user isolation costs the developer nothing: ``user_id``
becomes Anona's ``user_id`` scope, automatically, on every read and write.
``app_name`` becomes Anona's ``agent_id`` scope on the *write*
(``add_session_to_memory``) only — deliberately dropped on the *read*
(``search_memory``); see that method's own comment for why forwarding it
there would make every consolidated observation permanently unreachable.
Interfaces, call graph and event shape below were all verified against the
installed package (google-adk==2.6.3) by driving a real ``Runner`` through a
tool-calling turn and a real ``load_memory`` tool call, not by reading
docstrings.

**Neither method is called by ``Runner`` on its own; both are attached by the
developer, and ADK gives you two different ways to attach a read.**
``search_memory`` runs either when the model decides to call the
``load_memory`` tool, or — attach ``google.adk.tools.preload_memory``
instead and it runs **unconditionally, before every single model call**:
its own docstring says so directly ("This tool will be automatically
executed for each llm_request, and it won't be called by the model"), and
it is exported alongside ``load_memory`` in ``google.adk.tools`` with an
equally discoverable, arguably more inviting name. Confirmed directly: a
one-tool-call turn (2 model steps: call the tool, then answer) attached to
``preload_memory`` produced **2** ``/v1/retrieve`` calls, both carrying the
identical query text — once per model *step* within the turn, not once per
turn, the same over-calling shape the LangChain adapter needed a per-run
cache to avoid. ``preload_memory`` has no such cache; every extra model step
in a tool-calling turn is another full-latency, full-cost retrieve, silently
— unlike ``load_memory``, where the model chooses whether to search at all.
``add_session_to_memory`` is deliberately manual too: ADK's own base-class
docstring says a session "may be added multiple times during its lifetime",
and the only built-in caller in the whole package is the dev-only
``adk api_server``'s explicit "update memory" HTTP endpoint — grepped across
the installed package, no code path in ``Runner``/``BaseAgent``/the LLM flow
ever calls it. The documented, supported hook is
``Context.add_session_to_memory()``, reachable from a callback (confirmed by
driving one): wire an ``after_agent_callback`` that awaits
``callback_context.add_session_to_memory()`` and it fires exactly once per
``runner.run_async()`` call, after any internal tool-calling loop completes
(``BaseAgent.run_async`` calls it once, after ``_run_async_impl`` — the
method containing that loop — has fully finished, confirmed by counting
calls across a real two-step tool-calling turn). Skipping this callback
means Anona never receives a single turn.

**Event shape.** ``session.events`` is a flat list mixing plain text turns
with tool-call/tool-result events. A tool-call or tool-result ``Event``'s
``content.parts`` holds a ``function_call``/``function_response`` ``Part``
whose ``.text`` attribute is ``None`` (confirmed directly against real
events from a tool-calling turn) — never a stringified dict — so
``_event_text`` below naturally produces ``""`` for those and they are
dropped rather than landing in stored memory as a repr. ``Event.author`` is
``"user"`` for the user's own turns and the *agent's own name* (e.g.
``"weather_agent"``, never the literal string ``"agent"``) for every
agent-originated event, including tool-call and tool-result events — so the
speaker mapping below only special-cases ``"user"`` and buckets everything
else as "Assistant", which is correct for both. Partial/streaming chunks
never reach ``session.events`` at all: ADK's own session services drop any
event with ``partial=True`` before it is appended (confirmed by reading
``InMemorySessionService.append_event``), so this adapter does not need to
filter them itself.

**A session is re-added, not added once — and the same ``session.id`` can
show up with a *different* ``events`` list, not just a longer one.**
``BaseMemoryService``'s own docstring says a session "may be added multiple
times during its lifetime", and ADK's reference implementation
(``InMemoryMemoryService.add_session_to_memory``) is built around exactly
that: it *replaces* ``self._session_events[user_key][session.id]`` wholesale
on every call, so a repeat call is idempotent by construction — no
duplication, because the second call overwrites rather than appends.
Anona's ``remember()`` has no such key to replace; every call is a new,
independently-billed write, so transcribing the *whole* session every time
— what a naive read of the interface suggests — would re-store every
earlier turn again on every subsequent call on a reused session (one
``add_session_to_memory`` per turn, same ``session.id`` throughout a
conversation is the normal shape), compounding without bound and, because
raw facts are only ever superseded by a later *observation*, never by
another raw fact, never getting cleaned up on our side either.

A first attempt at fixing that tracked a plain *count* of events already
sent per ``session.id`` and sliced from there. That is wrong, not just
incomplete: ``session.events`` is not guaranteed to be the same
ever-growing list across calls. ``GetSessionConfig(num_recent_events=N)`` is
a first-class, documented way to fetch a **trimmed** view —
``_get_session_impl`` in ``InMemorySessionService`` literally does
``copied_session.events = copied_session.events[-N:]``. A later call that
happens to receive a *shorter* view than the count already recorded slices
to nothing and silently drops it — verified directly: two real turns
through a trimmed ``num_recent_events=2`` re-fetch after each one produced
exactly **one** ``/v1/record`` call, not two; the second turn vanished with
no error, because ``events[2:]`` on a fresh 2-event window is ``[]``. A
dropped memory has no symptom, which makes this worse than the duplication
bug it replaced.

``_AnonaMemoryService`` instead anchors on the **identity** of the last
event actually sent for a ``session.id`` (``Event.id`` — a real, unique
value: assigned once via ``Event.model_post_init`` the moment an event is
constructed, confirmed stable across ``copy.deepcopy`` too, which is how
``InMemorySessionService`` produces every fetched/trimmed view — never
regenerated). On each call it searches the incoming ``events`` for that id:
found → send only what comes after it (the same result the old counting
approach got right in the common case, the live invocation session
strictly growing). Not found — because the id fell out of a trimmed window,
or this is the first call ever for that ``session.id`` — send the **whole**
current view rather than guess a boundary. That can duplicate (the same
trimmed window fetched twice with nothing new in between resends
everything in it), which is the acceptable failure direction here: this is
a memory product, and failing toward *extra* stored content is recoverable
in a way that failing toward *silently missing* content is not.

The watermark lives on the service instance (a dict of
``{session_id: last_sent_event_id}``), so it is per ``AnonaMemoryService(
...)`` call, not global — a fresh instance sends every session it is asked
about as if from scratch, and unlike a fresh ``InMemoryMemoryService()``
restarting (which is clean amnesia — it simply forgets, nothing is
duplicated), that means a full, permanent, billed re-store of every prior
turn for any session that instance already knew about. Restarting the
service is not free here; re-storing is still the right default over
losing data, but it is not "the same as" ADK's own reference behavior.
The dict itself is never evicted — one small entry (a session id string,
an event id string) per distinct ``session.id`` this instance has ever
seen, for the instance's lifetime. Unbounded, but slow to matter: entries
are tiny, and most deployments cycle the process (deploys, restarts,
autoscaling) long before a single long-lived instance accumulates enough
distinct sessions for this to be worth more than knowing about. A process
expected to run indefinitely against a very large, ever-growing set of
distinct sessions should wrap or subclass this to add its own eviction
(e.g. an LRU) — nothing here does that on its own.
"""
from __future__ import annotations

from ._core import MemoryBridge, require


def _event_text(event) -> str:
    """Text of one session event, or ``""`` for a tool-call/tool-result event.

    ``Part.text`` is ``None`` (not a stringified function-call dict) on a
    part that instead carries ``function_call``/``function_response``/inline
    data — confirmed against real events from a tool-calling turn — so this
    already drops that scaffolding without special-casing it.
    """
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) or []
    return " ".join(str(getattr(p, "text", "") or "") for p in parts).strip()


def _transcript(events) -> str:
    """The given events, formatted as a ``Speaker: text`` transcript.

    Takes an events *iterable*, not a session — the caller is responsible
    for deciding which slice of ``session.events`` this covers (see
    ``_AnonaMemoryService.add_session_to_memory``, which passes only the
    events not already sent for this session).

    ``event.author`` is ``"user"`` for the user's own messages and the
    agent's own name for everything else it emits, tool-call/tool-result
    events included — confirmed directly, see the module docstring — so
    bucketing anything not literally ``"user"`` as "Assistant" is correct
    rather than a simplification that happens to work.
    """
    lines = []
    for event in events:
        text = _event_text(event)
        if not text:
            continue
        author = getattr(event, "author", "") or ""
        speaker = "User" if author == "user" else "Assistant"
        lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


def AnonaMemoryService(bridge: MemoryBridge):
    """Anona as an ADK ``BaseMemoryService``.

    A factory, not a class — hence the class-style name. The base class cannot
    be referenced before the guarded import.

    ADK supplies scope per call, so both methods pass it through
    :meth:`MemoryBridge.context` / :meth:`MemoryBridge.remember`'s keyword
    overrides — never by reaching into ``bridge``'s own construction-time
    scope. ``search_memory`` passes only ``user_id``; ``add_session_to_memory``
    passes all three — see ``search_memory``'s own comment for why that
    asymmetry is deliberate, not an oversight. The bridge keeps owning the
    fail-open policy; this adapter only translates types.

    Neither method fires without the developer attaching something first —
    see the module docstring for the two different ways to attach a read
    (``load_memory`` vs. ``preload_memory``, which differ sharply in cost).
    ``add_session_to_memory`` must be wired to a hook yourself, typically::

        from google.adk.agents import LlmAgent
        from google.adk.tools import load_memory

        # ADK invokes this with a *keyword* argument named exactly
        # ``callback_context`` — a differently-named parameter raises
        # TypeError before this adapter ever runs.
        async def save_to_memory(callback_context):
            await callback_context.add_session_to_memory()

        agent = LlmAgent(
            ...,
            tools=[load_memory],                   # lets the model search memory
            after_agent_callback=save_to_memory,   # stores the finished turn
        )

    Usage::

        from google.adk.runners import Runner
        from anona.integrations import MemoryBridge
        from anona.integrations.google_adk import AnonaMemoryService

        bridge = MemoryBridge(api_key="anona_live_...", space_id="my-agent")
        runner = Runner(
            agent=agent,
            app_name="my-app",
            session_service=session_service,
            memory_service=AnonaMemoryService(bridge=bridge),
        )
    """
    base = require("google.adk.memory.base_memory_service", "adk")
    entry = require("google.adk.memory.memory_entry", "adk")
    genai = require("google.genai.types", "adk")
    SearchMemoryResponse = base.SearchMemoryResponse
    MemoryEntry = entry.MemoryEntry
    Content = genai.Content
    Part = genai.Part

    class _AnonaMemoryService(base.BaseMemoryService):
        def __init__(self) -> None:
            super().__init__()
            # {session.id: id of the last event sent for that session}. See
            # the module docstring ("A session is re-added, not added
            # once — and the same session.id can show up with a different
            # events list, not just a longer one."). Anchored on event
            # *identity*, not a count: session.events is not guaranteed to
            # be the same, ever-growing list across calls (a trimmed
            # GetSessionConfig(num_recent_events=...) re-fetch is a real,
            # documented way to see a *shorter* view of the same session),
            # and a count-based watermark silently drops content when that
            # happens. Not evicted — see the module docstring.
            self._last_sent_event_id: dict = {}

        def _new_events(self, session_id, events: list) -> list:
            """The slice of ``events`` not yet sent for ``session_id``.

            Finds the last-sent event by identity in the current view and
            returns everything after it. If there is no recorded anchor
            (never called for this session before) or the anchor isn't in
            this view at all (e.g. a trimmed re-fetch that no longer
            includes it), returns the *whole* view rather than guess a
            boundary — duplicating in that case is the acceptable failure
            direction, not silently dropping it.
            """
            anchor = self._last_sent_event_id.get(session_id)
            if anchor is None:
                return events
            for i, event in enumerate(events):
                if getattr(event, "id", None) == anchor:
                    return events[i + 1:]
            return events

        async def add_session_to_memory(self, session) -> None:
            session_id = getattr(session, "id", None)
            events = list(getattr(session, "events", None) or [])
            new_events = self._new_events(session_id, events)
            if events:
                # Only advance the anchor when there is a real event to
                # anchor on — an empty view must never overwrite a
                # previously recorded (and still valid) anchor with None.
                self._last_sent_event_id[session_id] = getattr(
                    events[-1], "id", None
                )
            await bridge.remember(
                _transcript(new_events),
                user_id=getattr(session, "user_id", None),
                agent_id=getattr(session, "app_name", None),
                session_id=session_id,
            )

        async def search_memory(self, *, app_name: str, user_id: str, query: str):
            # app_name is deliberately NOT forwarded as agent_id here, unlike
            # on the write below. Consolidation writes an observation tagged
            # with only the scope key that wins when several are given
            # (user_id beats agent_id — scope precedence), so a stored
            # observation carries [anona:user:X] alone, never
            # [anona:user:X, anona:agent:A]. A two-key read runs under
            # all_strict (Postgres @>: the row must contain every query
            # tag), which that single-key row can never satisfy — so
            # forwarding agent_id here would make every consolidated
            # observation permanently unreachable through this adapter, and
            # ADK would see only raw world facts, never the synthesized
            # ones. Dropping it costs nothing on isolation: user_id is the
            # key that actually isolates one end user's memories from
            # another's, and it is still forwarded. The write is unaffected
            # by this — @> still matches a three-key stored row against a
            # one-key query, so add_session_to_memory keeps sending all
            # three (see the module docstring's read/write asymmetry note).
            block = await bridge.context(query, user_id=user_id)
            if not block:
                return SearchMemoryResponse(memories=[])
            return SearchMemoryResponse(memories=[
                MemoryEntry(
                    author="anona",
                    content=Content(parts=[Part(text=block)]),
                )
            ])

    return _AnonaMemoryService()
