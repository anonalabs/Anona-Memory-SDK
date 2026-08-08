"""Microsoft Agent Framework context-provider adapter.

``ContextProvider`` is the framework's own extension point for exactly this
job, but it is not shaped the way the plan assumed. Verified against the
installed package (agent-framework 1.13.0) and separately against the
literal floor (agent-framework-core==1.0.0, installed alone in an isolated
venv) by driving a real ``agent_framework.Agent`` through a real
tool-calling turn with a scripted chat client — not by reading docs.

**The extension point is ``before_run``/``after_run``, not
``invoking``/``invoked``.** Grepped the installed package, and separately
the 1.0.0 wheel, for ``invoking``/``invoked``: neither name exists anywhere,
at any version back to the literal floor. There is also no bare ``Context``
class to import or return. ``ContextProvider``'s real, current shape
(confirmed with ``inspect.signature`` against both 1.13.0 and 1.0.0)::

    class ContextProvider:
        def __init__(self, source_id: str): ...
        async def before_run(self, *, agent, session, context: SessionContext,
                              state: dict[str, Any]) -> None: ...
        async def after_run(self, *, agent, session, context: SessionContext,
                             state: dict[str, Any]) -> None: ...

``agent``/``session``/``context``/``state`` are all required keyword-only
arguments — there is no way to call either hook positionally with just a
message list, the way the plan's own draft test did. ``source_id`` is a
required positional constructor argument too (used for cross-provider
attribution: other providers can filter injected context/messages by it),
so ``ContextProvider()`` with no arguments — implied by the plan's draft —
does not work either. Also real, and also wrong in the plan: the message
class is ``Message``, not ``ChatMessage`` (the latter does not exist
anywhere in the installed package); ``Role`` is not an enum with a ``.USER``
attribute — it is ``NewType("Role", str)`` over the plain literal strings
``"system"``/``"user"``/``"assistant"``/``"tool"``, so a real message is
built as ``Message(role="user", contents=["..."])``. And there is no
``chat_client.create_agent(...)`` method anywhere in the package — the real
constructor is ``Agent(client=..., context_providers=[...], ...)``. Every
one of these is consistent with the plan having been written against Agent
Framework's pre-1.0 preview API, which was redesigned before the 1.0
release this adapter actually targets — the same kind of gap as CrewAI's
deleted ``ExternalMemory``, just spread across more names at once.

**Adding context: append to a list, don't return an object.** Instead of
returning a ``Context(instructions=...)``, ``before_run`` mutates the
``SessionContext`` it is handed: ``context.extend_instructions(source_id,
text)`` appends to ``SessionContext.instructions`` (a plain list) — it
cannot replace anything, structurally. The framework's own merge, done once
right before the model call (``RawAgent._prepare_session_and_messages``),
is a plain string join::

    if session_context.instructions:
        combined = "\\n".join(session_context.instructions)
        chat_options["instructions"] = (
            f"{chat_options['instructions']}\\n{combined}"
            if "instructions" in chat_options else combined
        )

Confirmed directly against a real run with ``instructions="You are a
helpful weather assistant."`` set on the agent: the (fake) model's
``options["instructions"]`` arrived as ``"You are a helpful weather
assistant.\\n<this adapter's block>"`` — the agent's own instructions
first, this adapter's block appended after, both in the same run.
There is no code path here shaped like the LangChain adapter's
``dataclasses.replace``-style ``request.override(...)`` bug: the merge
point the framework itself owns is additive by construction, not something
an adapter can get wrong by choosing "return" over "append" — the API only
offers "append".

**Granularity: exactly once per ``agent.run()`` call, tool rounds
included.** ``before_run``/``after_run`` run inside
``RawAgent._prepare_session_and_messages`` / ``_run_after_providers``, each
called exactly once per top-level ``run()`` (the streaming path calls
``after_run`` once too, after the stream is fully drained — still once).
The model-facing tool-calling loop (``FunctionInvocationLayer``) lives
entirely *inside* one ``client.get_response(...)`` call and is invisible
above that layer. Verified directly: a scripted two-model-call tool round
trip (a function-call response, then a final-answer response) produced
exactly one ``before_run`` and one ``after_run`` call, confirmed by
counting — and reconfirmed at the literal floor version. This is the
LlamaIndex/ADK end of the spectrum, not LangChain's: no per-step cache is
needed to avoid re-billing retrieve on every step of a tool-calling turn,
because from this hook's point of view there is only one step.

**Automatic — no callback to wire.** Passing ``context_providers=[...]`` to
``Agent(...)`` is sufficient on its own; ``agent.run(...)`` calls both
hooks without any further setup, confirmed by running an agent with no
other configuration and observing both fire. Unlike the ADK adapter, there
is no developer-side callback step to document as a prerequisite.

**Message content shape needs no workaround.** ``Message.text`` is a real
property (not the raw ``contents`` field) that joins only ``TextContent``
items and is ``""`` for a message carrying just a function-call or
function-result content item — confirmed directly against real messages
from the scripted tool-calling turn above (the assistant's function-call
message and the tool's function-result message both had ``.text == ""``;
only the final assistant text message had a non-empty ``.text``). Nothing
``repr()``-shaped or list-like can reach ``/v1/record`` through this
property — the same guarantee the LlamaIndex and ADK adapters found in
their own frameworks' message/content types.

**No per-call scope forwarding — and a real trap if it were added
carelessly.** Unlike ADK, ``AgentSession`` carries no ``user_id``/app
identity at all (just ``session_id``/``service_session_id``/``state``), so
there is nothing framework-native here to map onto Anona's
``user_id``/``agent_id`` the way the ADK adapter maps ADK's own ``user_id``/
``app_name``. Forwarding ``session.session_id`` as Anona's ``session_id``
scope looked tempting but is actively wrong by default: confirmed directly
that when a caller does not pass its own ``session=`` into
``agent.run(...)`` (the common case — every basic example in the
framework's own docstrings does exactly this), the agent creates a *fresh*
``AgentSession()`` — a new random UUID — on every single ``run()`` call.
Auto-forwarding that value would scope every turn's memory to a UUID
different from the previous turn's, fragmenting a conversation's memory
into one isolated write/read pair per turn instead of a shared history.
Scope here stays exactly what the LangChain and LlamaIndex adapters already
do: construction-time only, via ``MemoryBridge``'s own
``user_id``/``agent_id``/``session_id`` keyword arguments.

**Dependency: ``agent-framework-core``, not the ``agent-framework`` meta
package.** Confirmed via the installed distribution's own metadata: the
bare ``agent-framework`` package's only declared dependency is
``agent-framework-core[all]==<same version>`` — the ``[all]`` extra pulls
in every vendor integration the framework ships (Azure, Anthropic, Bedrock,
Redis, Qdrant, Mem0, Mistral, Ollama, ...), none of which this adapter
imports or needs. Every name this module touches (``ContextProvider``,
``SessionContext``, ``Message``, ``Agent``) lives directly in
``agent-framework-core``'s own ``agent_framework`` package — confirmed
against that distribution's file manifest — so declaring the lean package
here (see ``pyproject.toml``) avoids forcing a multi-hundred-megabyte,
mostly-unrelated dependency tree onto every caller of
``pip install 'anona[msagent]'``. A project that already depends on the
``agent-framework`` meta package (as any real user of this adapter will,
to get an actual chat-client implementation) already satisfies this either
way, since the meta package requires the core package as a strict subset.
"""
from __future__ import annotations

from ._core import MemoryBridge, require


def _text(message) -> str:
    """The text of one ``agent_framework.Message``.

    ``.text`` is a real property, not the raw ``contents`` field — it joins
    only ``TextContent`` items and is ``""`` for a message carrying just a
    function-call/function-result content item, confirmed directly against
    real messages from a tool-calling turn. See the module docstring.
    """
    return getattr(message, "text", "") or ""


def _last_user_text(messages) -> str:
    """The most recent ``"user"``-role message's text among ``messages``, or ``""``."""
    for msg in reversed(list(messages or [])):
        if getattr(msg, "role", None) == "user":
            return _text(msg)
    return ""


def _turn_text(context) -> str:
    """This run's new message(s) plus the model's response, formatted for storage.

    ``context.input_messages`` is this call's new message(s) — the current
    turn's ask; ``context.response.messages`` is the model's finished
    output for that same call, tool round-trips included but contributing
    nothing here beyond their own empty ``.text`` (see :func:`_text`). This
    deliberately is not ``context.get_messages(...)``: that also folds in
    every other provider's injected context (e.g. a ``HistoryProvider``'s
    replayed earlier turns), which would re-store history that is already
    stored.
    """
    messages = list(context.input_messages or [])
    if context.response is not None:
        messages += list(context.response.messages or [])
    lines = []
    for msg in messages:
        body = _text(msg)
        if not body:
            continue
        speaker = "User" if getattr(msg, "role", None) == "user" else "Assistant"
        lines.append(f"{speaker}: {body}")
    return "\n".join(lines)


def AnonaContextProvider(
    bridge: MemoryBridge, *, record: bool = True, source_id: str = "anona"
):
    """Anona as an Agent Framework ``ContextProvider``.

    A factory, not a class — hence the class-style name. The base class
    cannot be referenced before the guarded import.

    ``source_id`` is a required constructor argument on the real
    ``ContextProvider`` base (the framework uses it to attribute injected
    instructions/messages so other providers can filter by source);
    exposed here with a default so the documented
    ``AnonaContextProvider(bridge=bridge)`` call still works, and
    overridable for the rare case of wiring more than one Anona provider
    onto the same agent.

    See the module docstring for what was verified about call cadence
    (exactly once per ``agent.run()``, tool rounds included), where the
    injected block lands (appended after the agent's own instructions,
    never replacing them) and why this adapter does not forward per-call
    scope the way the ADK adapter does.

    Usage::

        from agent_framework import Agent
        from anona.integrations import MemoryBridge
        from anona.integrations.ms_agent import AnonaContextProvider

        bridge = MemoryBridge(api_key="anona_live_...", space_id="assistant")
        agent = Agent(
            client=chat_client,
            instructions="You are helpful.",
            context_providers=[AnonaContextProvider(bridge=bridge)],
        )
    """
    af = require("agent_framework", "msagent")

    class _AnonaContextProvider(af.ContextProvider):
        async def before_run(self, *, agent, session, context, state):
            query = _last_user_text(context.input_messages)
            block = await bridge.context(query)
            if block:
                context.extend_instructions(self.source_id, block)

        async def after_run(self, *, agent, session, context, state):
            if not record:
                return
            await bridge.remember(_turn_text(context))

    return _AnonaContextProvider(source_id)
