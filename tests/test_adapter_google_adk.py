"""Google ADK memory-service adapter tests.

Skips when google-adk is absent — CI does not install the optional extras.
Subprocess isolation for the same reason as the other public-SDK tests.

The first five tests call ``svc.add_session_to_memory``/``svc.search_memory``
directly against hand-built ``FakeSession``/``FakeEvent`` stand-ins — cheap,
and enough to pin the wire contract (which body field carries which ADK
value), but by themselves they would pass even if the adapter were wired
wrong against a real ``Runner``: every earlier adapter in this series had a
real bug a direct-call test could not see (LangChain's hook fired per model
step, not per turn; LlamaIndex's write hook only fires once an internal
buffer overflows; CrewAI's whole assumed API had been removed). The
remaining tests drive a real ``google.adk.runners.Runner`` through a real
tool-calling turn, a real ``after_agent_callback``, and the real
``load_memory`` tool — see ``anona/integrations/google_adk.py``'s module
docstring for the full verification trail. Findings from that trail worth
having in mind reading these:

* Neither ``add_session_to_memory`` nor ``search_memory`` is ever called by
  ``Runner``/``BaseAgent`` on their own — both are developer-triggered
  (``Context.add_session_to_memory()`` from a hook; the ``load_memory`` tool,
  ``preload_memory``, or ``tool_context.search_memory()`` for reads).
* An ``after_agent_callback`` is invoked with a **keyword** argument named
  ``callback_context`` (confirmed by reading ``base_agent.py``) — a callback
  parameter named anything else raises ``TypeError`` at the framework level,
  not inside this adapter.
* A tool-call/tool-result ``Event``'s ``Part.text`` is ``None``, never a
  stringified function-call dict, so it is dropped by the transcript builder
  without any special-casing.
* A session may be (and, in normal multi-turn use, is) added more than once;
  ``add_session_to_memory`` must send only what is new since the last call
  on that ``session.id``, not the whole, ever-growing transcript again —
  see ``test_add_session_to_memory_sends_only_new_events_on_repeat_call``
  and ``test_real_two_turns_on_same_session_do_not_resend_the_first_turn``.
* ``session.events`` is not always that same, ever-growing list either —
  ``GetSessionConfig(num_recent_events=N)`` is a documented way to fetch a
  *shorter*, trimmed view of the same session, and a naive count-based
  delta silently drops content when that happens (verified: two real turns
  through a trimmed re-fetch after each produced only **one**
  ``/v1/record`` call under that approach) — see
  ``test_real_trimmed_session_view_still_reaches_record_for_both_turns``.
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
    not _installed("google.adk"), reason="google-adk not installed"
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
        "from anona.integrations.google_adk import AnonaMemoryService\n"
        "svc = AnonaMemoryService(bridge=b)\n"
        "class FakeContent:\n"
        "    def __init__(self, text): self.parts = [type('P', (), {'text': text})()]\n"
        "_fake_event_counter = [0]\n"
        "class FakeEvent:\n"
        "    # Real ADK events always carry a unique, stable id (assigned\n"
        "    # once via Event.model_post_init at construction, confirmed\n"
        "    # never regenerated) -- FakeEvent mirrors that here so tests\n"
        "    # against this stand-in exercise the identity-based delta\n"
        "    # logic for real, rather than always hitting its no-anchor\n"
        "    # fallback path.\n"
        "    def __init__(self, author, text):\n"
        "        self.author = author; self.content = FakeContent(text)\n"
        "        _fake_event_counter[0] += 1\n"
        "        self.id = f'fake-event-{_fake_event_counter[0]}'\n"
        "class FakeSession:\n"
        "    def __init__(self):\n"
        "        self.app_name = 'my-app'; self.user_id = 'u1'; self.id = 'sess1'\n"
        "        self.events = [FakeEvent('user', 'hi'), FakeEvent('agent', 'hello')]\n"
    )
    script = header + textwrap.dedent(snippet)
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )


def _ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_search_memory_maps_adk_scope():
    # I2: agent_id (app_name) is deliberately NOT forwarded on the
    # read, unlike on the write (test_add_session_to_memory_records_transcript
    # below still asserts agent_id IS sent there). Consolidation writes an
    # observation tagged with only the winning scope key -- user_id beats
    # agent_id -- so a stored observation is [anona:user:X] alone. A
    # two-key [user, agent] read runs under all_strict (Postgres @>, every
    # query tag must be present on the row), which that single-key
    # observation can never satisfy: forwarding agent_id here made every
    # consolidated observation permanently unreachable through this
    # adapter. Isolation is unaffected -- user_id is still forwarded, and
    # that is the key that actually isolates one end user from another.
    _ok(_run("""
        asyncio.run(svc.search_memory(app_name='my-app', user_id='u1', query='q'))
        _, url, body = seen[-1]
        assert url.endswith('/v1/retrieve')
        assert body['user_id'] == 'u1'
        assert 'agent_id' not in body, body
        assert body['format'] == 'block'
        print('OK')
    """))


def test_search_memory_returns_response_with_memories():
    _ok(_run("""
        resp = asyncio.run(svc.search_memory(app_name='a', user_id='u1', query='q'))
        assert len(resp.memories) == 1, resp.memories
        text = resp.memories[0].content.parts[0].text
        assert text == 'You like Python', text
        print('OK')
    """))


def test_search_memory_empty_returns_no_memories():
    _ok(_run("""
        def empty(request):
            return httpx.Response(200, json={'context': ''})
        b._client._async_client = httpx.AsyncClient(transport=httpx.MockTransport(empty))
        resp = asyncio.run(svc.search_memory(app_name='a', user_id='u1', query='q'))
        assert list(resp.memories) == []
        print('OK')
    """))


def test_add_session_to_memory_records_transcript():
    _ok(_run("""
        asyncio.run(svc.add_session_to_memory(FakeSession()))
        method, url, body = seen[-1]
        assert method == 'POST' and url.endswith('/v1/record')
        assert 'hi' in body['content'] and 'hello' in body['content']
        assert body['user_id'] == 'u1'
        assert body['agent_id'] == 'my-app'
        assert body['session_id'] == 'sess1'
        print('OK')
    """))


def test_search_memory_fails_open():
    _ok(_run("""
        def boom(request):
            return httpx.Response(500, json={'error': {'code': 'boom'}})
        b._client._async_client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
        resp = asyncio.run(svc.search_memory(app_name='a', user_id='u1', query='q'))
        assert list(resp.memories) == []
        print('OK')
    """))


def test_add_session_to_memory_sends_only_new_events_on_repeat_call():
    # I1 (review fix round 1): a session may be added multiple times during
    # its lifetime (BaseMemoryService's own docstring), and ADK's reference
    # InMemoryMemoryService is safe against that because it *replaces* its
    # stored copy per session.id -- remember() has no such key to replace,
    # so re-transcribing the whole, ever-growing session.events on every
    # call would re-store every earlier turn again, compounding forever
    # (raw facts are only ever superseded by an observation, never by
    # another raw fact). The same FakeSession, mutated in place between two
    # calls on the same svc instance -- the watermark lives on the service,
    # not the session -- to isolate the dedup logic itself.
    _ok(_run("""
        session = FakeSession()
        asyncio.run(svc.add_session_to_memory(session))
        _, _, first_body = seen[-1]
        assert first_body['content'] == 'User: hi\\nAssistant: hello', first_body['content']

        session.events.append(FakeEvent('user', 'turn two'))
        session.events.append(FakeEvent('agent', 'second reply'))
        asyncio.run(svc.add_session_to_memory(session))
        _, _, second_body = seen[-1]
        assert second_body['content'] == 'User: turn two\\nAssistant: second reply', second_body['content']
        assert 'hi' not in second_body['content'], second_body['content']
        assert 'hello' not in second_body['content'], second_body['content']
        print('OK')
    """))


def test_add_session_to_memory_handles_empty_events_without_crashing():
    # The `if events:` guard around `events[-1]` in add_session_to_memory
    # (only advance the anchor when there is a real event to anchor on) is
    # reachable for real: GetSessionConfig(num_recent_events=0) is a
    # documented way to fetch a session whose events view is genuinely empty.
    # Every other test's FakeSession starts with events already populated, so
    # this guard had no coverage -- removing it crashes with IndexError on
    # events[-1] while the rest of the suite stays green. An empty view also
    # must not overwrite a previously recorded anchor, and _transcript([]) is
    # "", which MemoryBridge.remember() treats as nothing to store.
    _ok(_run("""
        session = FakeSession()
        session.events = []
        asyncio.run(svc.add_session_to_memory(session))
        assert seen == [], seen
        print('OK')
    """))


# Dedented on its own, independently of whatever test-specific snippet it
# gets concatenated with below. textwrap.dedent() strips the *minimum*
# common indentation across an entire string — concatenating this (written
# at one indentation level in this module) with a test's snippet (written at
# a deeper indentation level inside a function body) and dedenting the
# combined result once would strip only the smaller of the two, leaving the
# test-specific half still indented and silently nested inside StubLlm's
# class body instead of following it. Pre-dedenting each piece separately
# avoids that (see tests/sdk_public/test_adapter_crewai.py and
# test_adapter_llamaindex.py, same trap).
#
# A minimal BaseLlm: google-adk ships no mock model. `mode` scripts one of
# three real orchestration shapes, each verified against the installed
# package (google-adk==2.6.3): a tool-calling turn (get_dog_name), a
# load_memory round trip (the model calls the load_memory tool, then answers
# using the real FunctionResponse the tool comes back with — not a canned
# value), or a single plain reply.
_REAL_AGENT = textwrap.dedent("""
    from google.adk.agents import LlmAgent
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.adk.tools import load_memory as load_memory_tool
    from google.genai import types as genai_types

    def get_dog_name() -> str:
        "Return the dog's name."
        return 'Rex'

    class StubLlm(BaseLlm):
        model: str = 'stub-model'
        step: int = 0
        mode: str = 'plain'

        async def generate_content_async(self, llm_request, stream: bool = False):
            self.step += 1
            if self.mode == 'tool' and self.step == 1:
                yield LlmResponse(content=genai_types.Content(
                    role='model',
                    parts=[genai_types.Part(function_call=genai_types.FunctionCall(
                        name='get_dog_name', args={}, id='call1'))],
                ))
                return
            if self.mode == 'tool':
                yield LlmResponse(content=genai_types.Content(
                    role='model', parts=[genai_types.Part(text='Your dog is named Rex')]))
                return
            if self.mode == 'load_memory' and self.step == 1:
                yield LlmResponse(content=genai_types.Content(
                    role='model',
                    parts=[genai_types.Part(function_call=genai_types.FunctionCall(
                        name='load_memory', args={'query': 'what do I like'}, id='call2'))],
                ))
                return
            if self.mode == 'load_memory':
                # Read the tool's real FunctionResponse back out of the
                # request rather than asserting on a canned value -- proves
                # search_memory's return shape survives the whole
                # load_memory_tool -> FunctionResponse -> next model call
                # round trip, not just that it doesn't crash.
                last = llm_request.contents[-1]
                fr = last.parts[0].function_response
                result = fr.response.get('result') if fr and fr.response else None
                yield LlmResponse(content=genai_types.Content(
                    role='model',
                    parts=[genai_types.Part(text=f'MEMCHECK:{result!r}')]))
                return
            yield LlmResponse(content=genai_types.Content(
                role='model', parts=[genai_types.Part(text='ack')]))

    async def save_to_memory(callback_context):
        # The documented hook (Context.add_session_to_memory(), see the
        # adapter's module docstring) and the only way a session's events
        # ever reach the memory service -- nothing in Runner/BaseAgent calls
        # this on its own. Verified: the framework invokes an
        # after_agent_callback with a KEYWORD argument named
        # callback_context, not positionally -- a parameter named anything
        # else raises TypeError before this adapter ever runs.
        await callback_context.add_session_to_memory()

    def make_agent(mode, tools=None, after=None, name='dog_helper_agent'):
        # name deliberately not "agent": a real session's events carry the
        # AGENT'S OWN NAME as author for every non-user event, tool calls
        # and tool results included -- never the literal string "agent" --
        # confirmed against real events, so this exercises that rather than
        # coincidentally matching the adapter's binary author check.
        return LlmAgent(
            name=name, model=StubLlm(mode=mode),
            instruction='You are a helpful assistant.',
            tools=tools or [], after_agent_callback=after,
        )

    session_service = InMemorySessionService()
""")


def test_real_tool_calling_turn_stores_clean_transcript_via_after_agent_callback():
    _ok(_run(_REAL_AGENT + textwrap.dedent("""
        captured = {}

        async def main():
            agent = make_agent('tool', tools=[get_dog_name], after=save_to_memory)
            runner = Runner(app_name='my-app', agent=agent,
                             session_service=session_service, memory_service=svc)
            session = await session_service.create_session(app_name='my-app', user_id='u1')
            captured['session_id'] = session.id
            async for event in runner.run_async(
                user_id='u1', session_id=session.id,
                new_message=genai_types.Content(
                    role='user', parts=[genai_types.Part(text="what's my dog's name?")]),
            ):
                pass

        asyncio.run(main())

        record_calls = [s for s in seen if s[1].endswith('/v1/record')]
        # Exactly one -- after_agent_callback fires once per run, after the
        # internal tool-calling loop (model -> tool -> model) has already
        # finished, never once per model step.
        assert len(record_calls) == 1, record_calls
        _, _, body = record_calls[0]
        # The function-call/function-response scaffolding never leaks in --
        # only the clean text turn, same contract as every other adapter's
        # _turn_text.
        assert body['content'] == (
            "User: what's my dog's name?\\nAssistant: Your dog is named Rex"
        ), body['content']
        assert body['user_id'] == 'u1'
        assert body['agent_id'] == 'my-app'          # session.app_name -> agent_id
        assert body['session_id'] == captured['session_id']
        print('OK')
    """)))


def test_add_session_to_memory_is_not_called_automatically():
    _ok(_run(_REAL_AGENT + textwrap.dedent("""
        async def main():
            # No after_agent_callback at all -- this is the point being
            # tested: storage is entirely developer-triggered.
            agent = make_agent('plain')
            runner = Runner(app_name='my-app', agent=agent,
                             session_service=session_service, memory_service=svc)
            session = await session_service.create_session(app_name='my-app', user_id='u1')
            async for event in runner.run_async(
                user_id='u1', session_id=session.id,
                new_message=genai_types.Content(role='user', parts=[genai_types.Part(text='hi')]),
            ):
                pass

        asyncio.run(main())

        # Nothing reached Anona at all. Runner/BaseAgent never call
        # add_session_to_memory on their own -- grepped across the
        # installed package, the only built-in caller is the dev-only
        # "adk api_server"'s explicit HTTP endpoint.
        assert seen == [], seen
        print('OK')
    """)))


def test_real_two_turns_on_same_session_do_not_resend_the_first_turn():
    # I1, against a real Runner: the normal shape of a multi-turn
    # conversation is one after_agent_callback call per turn, all on the
    # same session_id, with session.events accumulating across run_async()
    # calls -- not two separate sessions (every other test in this file
    # uses a fresh session_id per call, which is exactly why this gap
    # shipped uncaught the first time). The second run's session carries
    # all four events (turn one's exchange plus turn two's), so this proves
    # the delta logic against ADK's real event accumulation, not a
    # hand-built stand-in.
    _ok(_run(_REAL_AGENT + textwrap.dedent("""
        async def main():
            agent = make_agent('plain', after=save_to_memory)
            runner = Runner(app_name='my-app', agent=agent,
                             session_service=session_service, memory_service=svc)
            session = await session_service.create_session(app_name='my-app', user_id='u1')

            async for event in runner.run_async(
                user_id='u1', session_id=session.id,
                new_message=genai_types.Content(role='user', parts=[genai_types.Part(text='turn one')]),
            ):
                pass
            async for event in runner.run_async(
                user_id='u1', session_id=session.id,
                new_message=genai_types.Content(role='user', parts=[genai_types.Part(text='turn two')]),
            ):
                pass

        asyncio.run(main())

        record_calls = [s for s in seen if s[1].endswith('/v1/record')]
        assert len(record_calls) == 2, record_calls
        first_content = record_calls[0][2]['content']
        second_content = record_calls[1][2]['content']
        assert first_content == 'User: turn one\\nAssistant: ack', first_content
        assert second_content == 'User: turn two\\nAssistant: ack', second_content
        assert 'turn one' not in second_content, second_content
        print('OK')
    """)))


def test_real_trimmed_session_view_still_reaches_record_for_both_turns():
    # N1 (review fix round 2): session.events is not guaranteed to be the
    # same, ever-growing list across calls -- GetSessionConfig(
    # num_recent_events=N) is a first-class, documented way to fetch a
    # *trimmed* view (google/adk/sessions/in_memory_session_service.py,
    # _get_session_impl: copied_session.events = copied_session.events[-N:]).
    # A count-based watermark cannot tell "these are the same events, seen
    # before" from "this is a shorter, different view of the same session"
    # -- against the pre-fix (fix round 1) code, this exact scenario
    # silently dropped the second turn entirely: turn one's write set the
    # count to 2, then a num_recent_events=2 re-fetch after turn two
    # returned a fresh 2-event list holding only turn two's own exchange,
    # and events[2:] on that is []. Drives the write through
    # memory_service.add_session_to_memory(session) directly on a trimmed
    # re-fetch -- the docs' own recommended direct call site, and the one
    # the after_agent_callback idiom (which always sees the live,
    # monotonically-growing invocation session) does not exercise.
    _ok(_run(_REAL_AGENT + textwrap.dedent("""
        from google.adk.sessions.base_session_service import GetSessionConfig

        async def main():
            agent = make_agent('plain')
            runner = Runner(app_name='my-app', agent=agent,
                             session_service=session_service, memory_service=svc)
            session = await session_service.create_session(app_name='my-app', user_id='u1')
            sid = session.id

            async for event in runner.run_async(
                user_id='u1', session_id=sid,
                new_message=genai_types.Content(role='user', parts=[genai_types.Part(text='turn one')]),
            ):
                pass
            trimmed_1 = await session_service.get_session(
                app_name='my-app', user_id='u1', session_id=sid,
                config=GetSessionConfig(num_recent_events=2),
            )
            await svc.add_session_to_memory(trimmed_1)

            async for event in runner.run_async(
                user_id='u1', session_id=sid,
                new_message=genai_types.Content(role='user', parts=[genai_types.Part(text='turn two')]),
            ):
                pass
            trimmed_2 = await session_service.get_session(
                app_name='my-app', user_id='u1', session_id=sid,
                config=GetSessionConfig(num_recent_events=2),
            )
            assert len(trimmed_2.events) == 2, trimmed_2.events   # confirms the view really is trimmed
            await svc.add_session_to_memory(trimmed_2)

        asyncio.run(main())

        record_calls = [s for s in seen if s[1].endswith('/v1/record')]
        # Both turns must reach Anona -- neither silently dropped by a
        # watermark that can't tell a shorter view from an already-seen one.
        assert len(record_calls) == 2, record_calls
        assert 'turn one' in record_calls[0][2]['content'], record_calls
        assert 'turn two' in record_calls[1][2]['content'], record_calls
        print('OK')
    """)))


def test_real_cross_user_isolation_end_to_end():
    # The point of this adapter: user_id is forwarded per call rather than
    # fixed at construction, so one AnonaMemoryService safely serves every
    # user of an app. Both the write (a real turn plus after_agent_callback)
    # and the read (a real load_memory tool call) go through a real Runner
    # -- not a direct svc.remember/search_memory call -- against a mock
    # backend that actually partitions storage by user_id, so a cross-user
    # leak would show up as real content crossing users, not just a wrong
    # kwarg in a request body.
    _ok(_run(_REAL_AGENT + textwrap.dedent("""
        store = {}
        def scoped_handler(request):
            body = json.loads(request.content.decode() or 'null')
            seen.append((request.method, str(request.url), body))
            url = str(request.url)
            if url.endswith('/v1/record'):
                store.setdefault(body.get('user_id'), []).append(body['content'])
                return httpx.Response(200, json={'memory_id': 'm1'})
            if url.endswith('/v1/retrieve'):
                block = '\\n'.join(store.get(body.get('user_id'), []))
                return httpx.Response(200, json={'context': block})
            return httpx.Response(404, json={'error': {'code': 'not_found'}})
        b._client._async_client = httpx.AsyncClient(transport=httpx.MockTransport(scoped_handler))

        async def store_turn(user_id, text):
            agent = make_agent('plain', after=save_to_memory)
            runner = Runner(app_name='my-app', agent=agent,
                             session_service=session_service, memory_service=svc)
            session = await session_service.create_session(app_name='my-app', user_id=user_id)
            async for event in runner.run_async(
                user_id=user_id, session_id=session.id,
                new_message=genai_types.Content(role='user', parts=[genai_types.Part(text=text)]),
            ):
                pass

        async def ask(user_id):
            agent = make_agent('load_memory', tools=[load_memory_tool])
            runner = Runner(app_name='my-app', agent=agent,
                             session_service=session_service, memory_service=svc)
            session = await session_service.create_session(app_name='my-app', user_id=user_id)
            final = None
            async for event in runner.run_async(
                user_id=user_id, session_id=session.id,
                new_message=genai_types.Content(role='user', parts=[genai_types.Part(text='what do I like?')]),
            ):
                if event.content and event.content.parts and event.content.parts[0].text:
                    final = event.content.parts[0].text
            return final

        async def main():
            await store_turn('alice', 'I like pizza')
            await store_turn('bob', 'I like sushi')
            return await ask('bob'), await ask('alice')

        bob_answer, alice_answer = asyncio.run(main())

        assert bob_answer and 'sushi' in bob_answer and 'pizza' not in bob_answer, bob_answer
        assert alice_answer and 'pizza' in alice_answer and 'sushi' not in alice_answer, alice_answer
        print('OK')
    """)))


def test_real_load_memory_tool_round_trip():
    # Confirms SearchMemoryResponse is exactly what ADK's own load_memory
    # tool expects: tool_context.search_memory() calls this adapter's
    # search_memory directly (google/adk/agents/context.py), wraps the
    # result in LoadMemoryResponse, and that wrapped result must survive
    # being handed back to the model as a real FunctionResponse.
    _ok(_run(_REAL_AGENT + textwrap.dedent("""
        async def main():
            agent = make_agent('load_memory', tools=[load_memory_tool])
            runner = Runner(app_name='my-app', agent=agent,
                             session_service=session_service, memory_service=svc)
            session = await session_service.create_session(app_name='my-app', user_id='u1')
            final = None
            async for event in runner.run_async(
                user_id='u1', session_id=session.id,
                new_message=genai_types.Content(role='user', parts=[genai_types.Part(text='what do I like?')]),
            ):
                if event.content and event.content.parts and event.content.parts[0].text:
                    final = event.content.parts[0].text
            return final

        final = asyncio.run(main())
        assert final is not None and 'You like Python' in final, final
        retrieve_calls = [s for s in seen if s[1].endswith('/v1/retrieve')]
        assert len(retrieve_calls) == 1, retrieve_calls
        assert retrieve_calls[0][2]['query'] == 'what do I like', retrieve_calls
        print('OK')
    """)))


def test_real_load_memory_tool_round_trip_empty_memories():
    # Gate item 4's empty case: SearchMemoryResponse(memories=[]) must not
    # crash the tool-calling loop when consumed for real, not just when
    # constructed directly.
    _ok(_run(_REAL_AGENT + textwrap.dedent("""
        def empty(request):
            return httpx.Response(200, json={'context': ''})
        b._client._async_client = httpx.AsyncClient(transport=httpx.MockTransport(empty))

        async def main():
            agent = make_agent('load_memory', tools=[load_memory_tool])
            runner = Runner(app_name='my-app', agent=agent,
                             session_service=session_service, memory_service=svc)
            session = await session_service.create_session(app_name='my-app', user_id='u1')
            final = None
            async for event in runner.run_async(
                user_id='u1', session_id=session.id,
                new_message=genai_types.Content(role='user', parts=[genai_types.Part(text='what do I like?')]),
            ):
                if event.content and event.content.parts and event.content.parts[0].text:
                    final = event.content.parts[0].text
            return final

        final = asyncio.run(main())
        assert final is not None
        assert 'memories=[]' in final, final
        print('OK')
    """)))


def test_import_error_names_the_extra():
    # Uses sys.modules[name] = None rather than patching builtins.__import__:
    # a sibling adapter task found that patching __import__ does not
    # reliably intercept require()'s importlib.import_module() call once the
    # target's top-level package is already cached (confirmed directly here
    # too -- google is a namespace package, already imported earlier in
    # this very subprocess via _run()'s own header building `svc`, and
    # google.adk.memory.base_memory_service is a submodule of it).
    # sys.modules[name] = None is the documented way to force ImportError
    # regardless of that: the import system checks for a None entry before
    # ever consulting a finder or __import__.
    _ok(_run("""
        for name in ('google.adk.memory.base_memory_service',
                     'google.adk.memory.memory_entry',
                     'google.genai.types'):
            sys.modules[name] = None
        if 'anona.integrations.google_adk' in sys.modules:
            del sys.modules['anona.integrations.google_adk']
        try:
            from anona.integrations.google_adk import AnonaMemoryService
            AnonaMemoryService(bridge=b)
            print('NO ERROR RAISED')
        except ImportError as e:
            assert "pip install 'anona[adk]'" in str(e), str(e)
            print('OK')
    """))
