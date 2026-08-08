"""Microsoft Agent Framework context-provider adapter tests.

Skips when agent-framework is absent — CI does not install the optional
extras. Subprocess isolation for the same reason as the other public-SDK
tests.

The brief this adapter was built from assumed an ``invoking``/``invoked``
API returning a ``Context(instructions=...)`` object, importable as
``from agent_framework import ChatMessage, Role``. None of that exists in
the installed package (agent-framework 1.13.0, and separately verified
against the literal floor agent-framework-core==1.0.0): the real extension
point is ``ContextProvider.before_run``/``after_run``, both required
keyword-only methods taking ``(*, agent, session, context: SessionContext,
state: dict)``; there is no bare ``Context`` class — a provider mutates the
``SessionContext`` it is handed (``context.extend_instructions(source_id,
text)``) instead of returning one; messages are ``agent_framework.Message``,
not ``ChatMessage``, and ``Role`` is not an enum (no ``.USER`` attribute) —
it is a plain string type over ``"user"``/``"assistant"``/etc. See
``anona/integrations/ms_agent.py``'s module docstring for the full
verification trail. The first five tests below call
``before_run``/``after_run`` directly (adjusted for the real signature) to
pin the wire contract; by themselves they would pass even if the adapter
were wired wrong against a real ``Agent`` — every earlier adapter in this
series had a real bug a direct-call test could not see (LangChain's hook
fired per model step; LlamaIndex's write hook only fires on internal buffer
overflow; CrewAI's whole assumed API had been removed; ADK's hooks are
never called automatically at all). The remaining tests drive a real
``agent_framework.Agent`` through a real tool-calling turn, with only the
raw model call scripted (the framework's own ``FunctionInvocationLayer``
tool loop runs for real). Two findings from that trail worth having in mind
reading these:

* ``before_run``/``after_run`` each fire exactly once per ``agent.run()``
  call, regardless of how many internal tool round-trips happen inside
  it — confirmed by counting across a scripted two-model-call tool turn.
  This is the safe end of the range this series has seen; no per-step
  cache is needed the way the LangChain adapter needed one.
* Passing ``context_providers=[...]`` to ``Agent(...)`` is sufficient on
  its own — ``agent.run(...)`` calls both hooks with no other setup,
  unlike the ADK adapter's mandatory ``after_agent_callback`` wiring.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_SDK_ROOT = Path(__file__).resolve().parent.parent


def _installed(module: str) -> bool:
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"], capture_output=True
    ).returncode == 0


pytestmark = pytest.mark.skipif(
    not _installed("agent_framework"), reason="agent-framework not installed"
)


def _run(snippet: str) -> subprocess.CompletedProcess:
    header = (
        "import sys\n"
        f"sys.path.insert(0, {str(_SDK_ROOT)!r})\n"
        "import json, asyncio, httpx\n"
        "from anona.integrations._core import MemoryBridge\n"
        "seen = []\n"
        "def handler(request):\n"
        "    body = request.content.decode() or 'null'\n"
        "    seen.append((request.method, str(request.url), json.loads(body)))\n"
        "    return httpx.Response(200, json={'context': 'You like Python',\n"
        "                                     'memory_id': 'm1'})\n"
        "b = MemoryBridge(api_key='k', space_id='s1', base_url='http://t.local')\n"
        "b._client._async_client = httpx.AsyncClient(\n"
        "    transport=httpx.MockTransport(handler),\n"
        "    headers={'Authorization': 'Bearer k'})\n"
        "from anona.integrations.ms_agent import AnonaContextProvider\n"
        "from agent_framework import Message, SessionContext, AgentSession, ChatResponse\n"
        "p = AnonaContextProvider(bridge=b)\n"
        "session = AgentSession()\n"
        "state = {}\n"
    )
    script = header + textwrap.dedent(snippet)
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )


def _ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_before_run_adds_context_instructions():
    _ok(_run("""
        ctx = SessionContext(input_messages=[Message(role='user', contents=['what do I like?'])])
        asyncio.run(p.before_run(agent=None, session=session, context=ctx, state=state))
        assert any('You like Python' in i for i in ctx.instructions), ctx.instructions
        _, url, body = seen[-1]
        assert url.endswith('/v1/retrieve')
        assert body['format'] == 'block'
        print('OK')
    """))


def test_before_run_empty_when_no_memories():
    _ok(_run("""
        def empty(request):
            return httpx.Response(200, json={'context': ''})
        b._client._async_client = httpx.AsyncClient(transport=httpx.MockTransport(empty))
        ctx = SessionContext(input_messages=[Message(role='user', contents=['q'])])
        asyncio.run(p.before_run(agent=None, session=session, context=ctx, state=state))
        assert ctx.instructions == [], ctx.instructions
        print('OK')
    """))


def test_after_run_records_turn():
    _ok(_run("""
        ctx = SessionContext(input_messages=[Message(role='user', contents=['hi'])])
        # The framework sets this exact private attribute itself right before
        # calling after_run (agent_framework/_agents.py:
        # "session_context._response = agent_response  # type: ignore[assignment]")
        # -- SessionContext.response is a read-only property by design, so
        # this is the framework's own documented way of populating it, not a
        # workaround. Same category as this file already reaching into
        # b._client._async_client for MockTransport wiring.
        ctx._response = ChatResponse(messages=[Message(role='assistant', contents=['hello'])])
        asyncio.run(p.after_run(agent=None, session=session, context=ctx, state=state))
        method, url, body = seen[-1]
        assert method == 'POST' and url.endswith('/v1/record')
        assert 'hi' in body['content'] and 'hello' in body['content']
        print('OK')
    """))


def test_before_run_fails_open():
    _ok(_run("""
        def boom(request):
            return httpx.Response(500, json={'error': {'code': 'boom'}})
        b._client._async_client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
        ctx = SessionContext(input_messages=[Message(role='user', contents=['q'])])
        asyncio.run(p.before_run(agent=None, session=session, context=ctx, state=state))
        assert ctx.instructions == []
        print('OK')
    """))


def test_scope_forwarded():
    # Unlike the ADK adapter, agent_framework.AgentSession carries no
    # user_id/app identity to forward per call (just session_id, and see
    # test_real_session_id_changes_across_runs_without_explicit_session for
    # why forwarding even that would be actively wrong by default) -- so
    # this pins the same thing the LangChain/LlamaIndex adapters guarantee:
    # scope fixed on the bridge at construction time reaches the wire.
    _ok(_run("""
        b2 = MemoryBridge(api_key='k', space_id='s1', base_url='http://t.local',
                          session_id='sess1')
        b2._client._async_client = b._client._async_client
        p2 = AnonaContextProvider(bridge=b2)
        ctx = SessionContext(input_messages=[Message(role='user', contents=['x'])])
        asyncio.run(p2.after_run(agent=None, session=session, context=ctx, state=state))
        _, _, body = seen[-1]
        assert body['session_id'] == 'sess1'
        print('OK')
    """))


def test_after_run_record_false_is_silent():
    _ok(_run("""
        p3 = AnonaContextProvider(bridge=b, record=False)
        before = len(seen)
        ctx = SessionContext(input_messages=[Message(role='user', contents=['hi'])])
        ctx._response = ChatResponse(messages=[Message(role='assistant', contents=['hello'])])
        asyncio.run(p3.after_run(agent=None, session=session, context=ctx, state=state))
        assert len(seen) == before, seen
        print('OK')
    """))


def test_before_run_no_user_message_skips_the_call():
    _ok(_run("""
        before = len(seen)
        ctx = SessionContext(input_messages=[])
        asyncio.run(p.before_run(agent=None, session=session, context=ctx, state=state))
        assert len(seen) == before, seen
        assert ctx.instructions == []
        print('OK')
    """))


# Dedented on its own, independently of whatever test-specific snippet it
# gets concatenated with below -- same trap noted in test_adapter_crewai.py,
# test_adapter_llamaindex.py and test_adapter_google_adk.py: textwrap.dedent
# strips the *minimum* common indentation across the whole combined string,
# so pre-dedenting each half separately keeps the test-specific half from
# ending up still-indented and silently nested inside a class/function body.
#
# agent_framework ships no mock chat client. ScriptedChatClient composes
# FunctionInvocationLayer + BaseChatClient -- the same composition
# agent_framework.openai.OpenAIChatClient uses (confirmed via its __mro__)
# -- so the real tool-calling loop (local get_weather() actually executes,
# a real FunctionResultContent actually gets built and fed back) runs for
# real; only the raw model call (_inner_get_response) is scripted.
_REAL_AGENT = textwrap.dedent("""
    from agent_framework import (
        Agent, BaseChatClient, ChatResponse, Content, ContextProvider,
        FunctionInvocationLayer, Message,
    )

    def get_weather(location: str) -> str:
        "Get the weather for a location."
        return f'sunny in {location}'

    class ScriptedChatClient(FunctionInvocationLayer, BaseChatClient):
        def __init__(self, script, **kw):
            super().__init__(**kw)
            self.script = list(script)
            self.calls = []

        async def _inner_get_response(self, *, messages, stream, options, **kwargs):
            await self._validate_options(options)
            self.calls.append((list(messages), dict(options)))
            return self.script[len(self.calls) - 1]

    def tool_call_then_answer(answer='The weather in Paris is sunny.'):
        return [
            ChatResponse(
                messages=[Message(role='assistant', contents=[
                    Content.from_function_call(
                        call_id='c1', name='get_weather', arguments={'location': 'Paris'})
                ])],
                response_id='r1',
            ),
            ChatResponse(messages=[Message(role='assistant', contents=[answer])], response_id='r2'),
        ]
""")


def test_real_tool_calling_turn_fires_hooks_once_and_stores_clean_transcript():
    _ok(_run(_REAL_AGENT + textwrap.dedent("""
        provider = AnonaContextProvider(bridge=b)
        client = ScriptedChatClient(tool_call_then_answer())
        agent = Agent(
            client=client, name='weather-agent',
            instructions='You are a helpful weather assistant.',
            tools=[get_weather], context_providers=[provider],
        )
        resp = asyncio.run(agent.run("What's the weather in Paris?"))
        assert resp.text == 'The weather in Paris is sunny.', resp.text

        retrieve_calls = [s for s in seen if s[1].endswith('/v1/retrieve')]
        record_calls = [s for s in seen if s[1].endswith('/v1/record')]
        # Exactly one each, despite two model calls inside this one turn --
        # before_run/after_run run once per agent.run(), not once per model
        # step (LangChain's worst bug: the same hook firing per step stored
        # an orphaned, answerless memory on every tool round-trip).
        assert len(retrieve_calls) == 1, retrieve_calls
        assert len(record_calls) == 1, record_calls
        assert len(client.calls) == 2, client.calls

        # The function-call/function-result scaffolding never leaks in --
        # only the clean text turn, same contract as every other adapter's
        # _turn_text. Message.text is '' for both the assistant's
        # function-call message and the tool's function-result message.
        content = record_calls[0][2]['content']
        assert content == (
            "User: What's the weather in Paris?\\nAssistant: The weather in Paris is sunny."
        ), content
        print('OK')
    """)))


def test_real_instructions_are_appended_not_replaced():
    _ok(_run(_REAL_AGENT + textwrap.dedent("""
        provider = AnonaContextProvider(bridge=b)
        client = ScriptedChatClient(tool_call_then_answer())
        agent = Agent(
            client=client,
            instructions='You are a helpful weather assistant.',
            tools=[get_weather], context_providers=[provider],
        )
        asyncio.run(agent.run("What's the weather in Paris?"))

        # The model's own options['instructions'] on the FIRST call (before
        # any tool result exists) already carries both: the agent's own
        # instructions text is not dropped, and the memory block is appended
        # after it, not instead of it. This is the LangChain adapter's worst
        # bug in reverse -- there, request.override(system_message=...) was
        # a dataclasses.replace that silently discarded the caller's own
        # system prompt.
        seen_instructions = client.calls[0][1].get('instructions') or ''
        assert seen_instructions.startswith('You are a helpful weather assistant.'), seen_instructions
        assert 'You like Python' in seen_instructions, seen_instructions
        print('OK')
    """)))


def test_real_context_provider_runs_with_no_extra_wiring():
    # Unlike the ADK adapter (which requires an explicit after_agent_callback
    # or nothing is ever stored), passing context_providers=[...] to Agent(...)
    # is the whole setup -- no callback, no manual hook registration.
    _ok(_run(_REAL_AGENT + textwrap.dedent("""
        provider = AnonaContextProvider(bridge=b)
        client = ScriptedChatClient([
            ChatResponse(messages=[Message(role='assistant', contents=['hi there'])], response_id='r1'),
        ])
        agent = Agent(client=client, context_providers=[provider])
        asyncio.run(agent.run('hello'))
        retrieve_calls = [s for s in seen if s[1].endswith('/v1/retrieve')]
        record_calls = [s for s in seen if s[1].endswith('/v1/record')]
        assert len(retrieve_calls) == 1, retrieve_calls
        assert len(record_calls) == 1, record_calls
        print('OK')
    """)))


def test_real_session_id_changes_across_runs_without_explicit_session():
    # Documents why this adapter does NOT auto-forward session.session_id as
    # Anona's session_id scope: when the caller does not pass its own
    # session= into agent.run() (the common case -- every basic example in
    # the framework's own docstrings does this), a fresh AgentSession() with
    # a new random UUID is created on every single run() call. Forwarding it
    # would fragment one conversation's memory into one isolated write/read
    # pair per turn instead of a shared history.
    _ok(_run(_REAL_AGENT + textwrap.dedent("""
        class SpySessionId(ContextProvider):
            def __init__(self):
                super().__init__(source_id='spy')
                self.ids = []
            async def before_run(self, *, agent, session, context, state):
                self.ids.append(session.session_id)
            async def after_run(self, *, agent, session, context, state):
                pass

        spy = SpySessionId()
        client = ScriptedChatClient([
            ChatResponse(messages=[Message(role='assistant', contents=['ok1'])], response_id='r1'),
            ChatResponse(messages=[Message(role='assistant', contents=['ok2'])], response_id='r2'),
        ])
        agent = Agent(client=client, context_providers=[AnonaContextProvider(bridge=b), spy])
        asyncio.run(agent.run('first'))
        asyncio.run(agent.run('second'))
        assert len(spy.ids) == 2
        assert spy.ids[0] != spy.ids[1], spy.ids
        print('OK')
    """)))


def test_import_error_names_the_extra():
    # sys.modules[name] = None is the standard, documented way to force the
    # next import of that name to raise ImportError, enforced deep inside
    # importlib._bootstrap._find_and_load itself -- unlike patching
    # builtins.__import__, which does NOT intercept require()'s
    # importlib.import_module(module) call here (confirmed directly:
    # import_module resolves a plain top-level name via
    # importlib._bootstrap._gcd_import, which never calls back through
    # builtins.__import__ for that name -- only nested relative imports
    # inside the target package's own source do).
    _ok(_run("""
        for mod in [m for m in list(sys.modules) if m.startswith('agent_framework')]:
            del sys.modules[mod]
        sys.modules['agent_framework'] = None
        if 'anona.integrations.ms_agent' in sys.modules:
            del sys.modules['anona.integrations.ms_agent']
        try:
            from anona.integrations.ms_agent import AnonaContextProvider
            AnonaContextProvider(bridge=b)
        except ImportError as e:
            assert "pip install 'anona[msagent]'" in str(e), str(e)
            print('OK')
    """))
