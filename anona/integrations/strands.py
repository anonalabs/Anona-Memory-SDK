"""AWS Strands adapter — memory as tools.

Strands has no memory-provider interface. Its ``SessionManager`` persists and
restores the *verbatim* message list; Anona returns synthesized memories, so
implementing that interface would hand back something that is not the
conversation. Two tools instead: the agent recalls and saves explicitly. This
decision was made before this task started (see
``mintlify-docs/integrations/strands.mdx``'s "Why tools" section) — what
follows is what was verified about the installed package, not a re-litigation
of it.

Verified against the installed package (strands-agents 1.51.0) by driving a
real ``strands.Agent`` through a real, scripted tool-calling turn — not by
reading source or docs — and re-verified the same way at the literal floor
(``strands-agents==1.0.0``, installed alone in an isolated venv) since a
version floor is only trustworthy if both ends of the range were run, not
just the newest release.

**1. ``@tool`` is real, exactly where the brief says, and ``tool_name`` is the
right attribute.** ``strands.tool`` is a plain decorator; applied to a
function it returns a ``strands.tools.decorator.DecoratedFunctionTool``,
confirmed with ``isinstance`` against a real decorated function at both
versions. Its name is exposed as ``.tool_name`` — a real ``@property``, not a
guess — which is what the framework's own ``ToolRegistry`` reads to key the
tool. ``functools.update_wrapper`` also copies the original function's
``__name__`` onto the wrapper, so the brief's fallback chain
(``tool_name`` then ``__name__``) happens to agree in the common case, but
the two attributes are **not** the same thing: confirmed directly that
``@tool(name="renamed")`` on a function still named ``original_func_name``
produces ``tool_name == "renamed"`` while ``__name__ == "original_func_name"``
— ``tool_name`` is the one Strands actually registers and calls, ``__name__``
is an incidental side effect of ``update_wrapper``. This module does not use
``name=`` (see point 2 for why the function names below already carry the
prefix), so the two agree here, but ``tool_name`` is what to trust in
general.

**2. Tool-name collisions: real mechanism, no default collision, namespaced
anyway.** The installed package (not the literal floor — introduced somewhere
after 1.0.0, absent there, present at 1.51.0) ships a full native memory
subsystem, ``strands.memory`` (``MemoryManager``, a ``MemoryStore`` protocol
with ``search``/``add``/``add_messages``, automatic extraction, automatic
context injection) that the brief had no way to know about. It is opt-in only
— an ``Agent`` never gets one unless the caller constructs a ``MemoryManager``
with actual stores and passes ``memory_manager=`` — and its default tool
names, read directly out of ``memory_manager.py``, are ``search_memory`` and
``add_memory``, not ``recall_memory``/``save_memory``, so there is no default
collision with the brief's originally proposed names under ordinary use.

But the collision *mechanism* itself was confirmed real, silent, and
version-stable, by constructing a real ``Agent`` with two ``@tool``-decorated
functions sharing one name and reading which survived in
``agent.tool_registry.registry``, at both 1.51.0 and the literal 1.0.0 floor:
whichever tool is later in the ``tools=[...]`` list silently wins, with no
exception and no visible log — ``ToolRegistry.register_tool`` only raises on
a duplicate name when the incoming tool's ``supports_hot_reload`` is
``False``, and every plain function-based tool (``DecoratedFunctionTool``,
what ``@tool`` produces) reports ``supports_hot_reload = True`` unconditionally,
so the raise-on-duplicate branch never actually fires for this kind of tool.
This is the same danger class the CrewAI adapter hit for real (an unprefixed
``"Search memory"`` silently lost to CrewAI's own built-in of the same name)
— no live collision was reproduced here, since Strands' own vocabulary
differs from ours today, but the mechanism that would make one silent is
confirmed to exist and to have zero warning either way. Given a native
memory subsystem now exists that this task's author never saw, "doubt" is
exactly the right word, so the tools below are named ``anona_recall_memory``
and ``anona_save_memory`` rather than the brief's bare ``recall_memory``/
``save_memory`` — cheap to do, shrinks the collision surface against both
Strands' own current/future built-ins and any other tool package a caller
might load, and (unlike CrewAI's ``"Anona: Search memory"`` fix) stays a
plain identifier, which matters here specifically: real model providers
(Bedrock Converse, OpenAI, Anthropic) constrain function/tool names to
``[a-zA-Z0-9_-]``, and confirmed directly that Strands' own registry and
schema validation do *not* reject a colon-and-space name like CrewAI's
pattern — it would construct and register without error locally, then be
liable to fail at the model provider, a strictly worse place to discover it.

**3. Argument shapes: nothing repr()-shaped can reach the function body, and
this is enforced structurally, not just by convention.** Confirmed directly:
Strands validates a tool call's ``input`` dict against a Pydantic model
generated from the function's own type hints *before* the function is ever
invoked. Calling a probe tool's ``.stream()`` with ``{"query": 123}``,
``{"query": ["a", "b"]}``, ``{"query": {"nested": 1}}`` and ``{"query": None}``
never once reached the function body — each came back as a tool-call error
result (Pydantic's ``string_type`` validation message) instead. A sibling
adapter's worst bug was a raw list reaching ``/v1/record`` as a stringified
Python repr; that specific failure mode cannot happen on the normal
tool-call path here, because the framework itself refuses to invoke the
function at all for a wrong-shaped argument. (Calling the decorated function
directly in Python, bypassing the tool-call path entirely, has no such
guard — but ``MemoryBridge.context``/``.remember`` already fail open on a
non-string via their own ``.strip()`` check, so that path is covered too, by
the shared core rather than by this adapter.)

**4. Return values: a plain string is the right shape, confirmed both ways.**
``DecoratedFunctionTool``'s tool-call path (``_wrap_tool_result``) wraps a
plain string return in ``{"status": "success", "content": [{"text": ...}]}``
automatically; a dict already shaped like ``{"status": ..., "content": ...}``
passes through with a ``toolUseId`` merged in. Both tools below return plain
strings on every path, success and failure alike — ``recall_memory`` returns
"No relevant memories found." rather than an empty string when
``bridge.context()`` comes back empty (network failure or genuinely no
matches — indistinguishable by design, since the bridge fails open), so
there is never a case where this adapter hands Strands an empty result to
wrap. Verified end-to-end with a real scripted turn including a failure
(mock transport returning HTTP 500 for the retrieve call): the agent's final
answer still incorporated "No relevant memories found." as ordinary tool
output, no crash, no dropped turn.

**5. ``SessionManager`` premise still holds.** Confirmed directly against
the abstract base (``strands.session.session_manager.SessionManager``):
every hook (``append_message(message: Message, agent)``,
``redact_latest_message``, ``sync_agent``) operates on the literal ``Message``
objects the framework itself constructed for the turn — there is no
extraction or synthesis step anywhere in the interface. The docs page's "why
tools, not a SessionManager" framing is accurate for the installed version,
not carried over unverified.
"""
from __future__ import annotations

from ._core import MemoryBridge, require


def anona_tools(bridge: MemoryBridge) -> list:
    """Two Strands tools backed by one Anona space.

    See the module docstring for what was verified about the installed
    package: ``tool_name`` vs ``__name__``, the tool-name-collision mechanism
    this adapter namespaces against, the argument-validation guarantee that
    keeps non-string input from ever reaching these functions, and the
    return-value contract (plain strings, always, success or failure).

    Usage::

        from strands import Agent
        from anona.integrations import MemoryBridge
        from anona.integrations.strands import anona_tools

        bridge = MemoryBridge(api_key="anona_live_...", space_id="assistant")
        agent = Agent(tools=anona_tools(bridge))
    """
    strands = require("strands", "strands")
    tool = strands.tool

    # Both tools call the bridge's *_sync methods, not _sync(bridge.context/
    # remember(...)) -- Strands calls tools sequentially, and a fresh event
    # loop per _sync() call is unsound for that (see
    # MemoryBridge.context_sync's docstring): every other sequential call
    # silently failed open.

    @tool
    def anona_recall_memory(query: str) -> str:
        """Search long-term memory for anything relevant to a question.

        Args:
            query: What to look for, phrased as a question or topic.
        """
        block = bridge.context_sync(query)
        return block or "No relevant memories found."

    @tool
    def anona_save_memory(content: str) -> str:
        """Store a fact worth remembering in future conversations.

        Args:
            content: The fact to remember, as a complete sentence.
        """
        bridge.remember_sync(content)
        return "Saved."

    return [anona_recall_memory, anona_save_memory]
