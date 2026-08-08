"""LlamaIndex memory-block adapter tests.

Skips when llama-index-core is absent — CI does not install the optional
extras. Subprocess isolation for the same reason as the other public-SDK
tests.

The first five tests call ``blk._aget``/``blk._aput`` directly — cheap, but
by themselves they would pass even if the adapter were wired into
``Memory``/``FunctionAgent`` incorrectly, the way a direct-call test on the
LangChain and CrewAI adapters would have passed against their own real bugs
(see those adapters' module docstrings). The remaining tests exercise real
orchestration: a real ``Memory`` object for where a block's output lands,
and a real ``FunctionAgent`` tool-calling turn — the two things a direct
call can't check — for call cadence and batch shape. See
``anona/integrations/llamaindex.py``'s module docstring for what those
verified and why the eviction-forcing setup below (tiny token budget,
pre-seeded history) is needed to observe ``_aput`` firing at all.
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
    not _installed("llama_index.core"), reason="llama-index not installed"
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
        "from anona.integrations.llamaindex import AnonaMemoryBlock\n"
        "from llama_index.core.llms import ChatMessage\n"
        "blk = AnonaMemoryBlock(bridge=b)\n"
    )
    script = header + textwrap.dedent(snippet)
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )


def _ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_aget_returns_context():
    _ok(_run("""
        msgs = [ChatMessage(role='user', content='what do I like?')]
        out = asyncio.run(blk._aget(messages=msgs))
        assert out == 'You like Python', out
        _, url, body = seen[-1]
        assert url.endswith('/v1/retrieve')
        print('OK')
    """))


def test_aget_empty_without_messages():
    _ok(_run("""
        out = asyncio.run(blk._aget(messages=[]))
        assert out == '', repr(out)
        assert seen == [], seen
        print('OK')
    """))


def test_aput_records_turn():
    _ok(_run("""
        msgs = [
            ChatMessage(role='user', content='hi'),
            ChatMessage(role='assistant', content='hello'),
        ]
        asyncio.run(blk._aput(msgs))
        method, url, body = seen[-1]
        assert method == 'POST' and url.endswith('/v1/record')
        assert 'hi' in body['content'] and 'hello' in body['content']
        print('OK')
    """))


def test_aget_fails_open():
    _ok(_run("""
        def boom(request):
            return httpx.Response(500, json={'error': {'code': 'boom'}})
        b._client._async_client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
        msgs = [ChatMessage(role='user', content='q')]
        assert asyncio.run(blk._aget(messages=msgs)) == ''
        print('OK')
    """))


def test_scope_forwarded():
    _ok(_run("""
        b2 = MemoryBridge(api_key='k', space_id='s1', base_url='http://t.local',
                          agent_id='a1')
        b2._client._async_client = b._client._async_client
        blk2 = AnonaMemoryBlock(bridge=b2)
        asyncio.run(blk2._aput([ChatMessage(role='user', content='x')]))
        _, _, body = seen[-1]
        assert body['agent_id'] == 'a1'
        print('OK')
    """))


def test_aget_caches_repeated_same_query_briefly():
    # I3: real orchestration calls Memory.aget() (hence _aget) 2-3+ times
    # per turn with the same query text (see the adapter's module
    # docstring) -- without a cache that is 2-3+ live retrieve calls, on
    # the slowest, metered call in the product, for one logical question.
    # Two back-to-back calls with the identical query must collapse to one
    # retrieve. (Currently fails against uncached code: 2 retrieve calls.)
    _ok(_run("""
        msgs = [ChatMessage(role='user', content='what do I like?')]
        out1 = asyncio.run(blk._aget(messages=msgs))
        out2 = asyncio.run(blk._aget(messages=msgs))
        assert out1 == 'You like Python', out1
        assert out2 == 'You like Python', out2
        retrieve_calls = [s for s in seen if s[1].endswith('/v1/retrieve')]
        assert len(retrieve_calls) == 1, retrieve_calls
        print('OK')
    """))


def test_aget_cache_does_not_leak_across_different_queries():
    # The cache is keyed on exact query text and holds one entry -- a
    # topic change (a new turn) must always issue a fresh retrieve, never
    # be silently served the previous turn's cached answer.
    _ok(_run("""
        def handler2(request):
            body = request.content.decode() or 'null'
            payload = json.loads(body)
            seen.append((request.method, str(request.url), payload))
            ctx = 'You like Python' if payload.get('query') == 'q1' else 'You like Rust'
            return httpx.Response(200, json={'context': ctx, 'memory_id': 'm1'})
        b._client._async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler2))

        out1 = asyncio.run(blk._aget(messages=[ChatMessage(role='user', content='q1')]))
        out2 = asyncio.run(blk._aget(messages=[ChatMessage(role='user', content='q2')]))
        assert out1 == 'You like Python', out1
        assert out2 == 'You like Rust', out2
        retrieve_calls = [s for s in seen if s[1].endswith('/v1/retrieve')]
        assert len(retrieve_calls) == 2, retrieve_calls
        print('OK')
    """))


def test_turn_text_formats_a_flushed_batch_without_orphaned_fragments():
    # Direct check of the exact batch shape a real forced eviction produces
    # (verified separately, below): a tool round-trip commits as one
    # batch — question, empty tool-call message, tool result, final
    # answer — and _turn_text must drop the tool-call scaffolding and the
    # raw tool result, keeping only the readable exchange.
    _ok(_run("""
        from anona.integrations.llamaindex import _turn_text, _last_user_text

        batch = [
            ChatMessage(role='user', content="what's my dog's name?"),
            ChatMessage(role='assistant', content=''),
            ChatMessage(role='tool', content='Rex'),
            ChatMessage(role='assistant', content='Your dog is named Rex'),
        ]
        assert _last_user_text(batch) == "what's my dog's name?", _last_user_text(batch)
        assert _turn_text(batch) == (
            "User: what's my dog's name?\\nAssistant: Your dog is named Rex"
        ), _turn_text(batch)
        print('OK')
    """))


def test_content_shape_image_only_message_fails_open():
    # ChatMessage.content is None for a message with no TextBlocks (an
    # image-only message) — confirmed directly against the installed
    # package. _text()'s `content or ""` must turn that into "", never
    # forward None/a block list to MemoryBridge or store it as text.
    _ok(_run("""
        from llama_index.core.base.llms.types import ImageBlock

        img_only = ChatMessage(role='user', blocks=[ImageBlock(url='http://x/y.png')])
        assert img_only.content is None, img_only.content

        out = asyncio.run(blk._aget(messages=[img_only]))
        assert out == '', repr(out)
        assert seen == [], seen

        asyncio.run(blk._aput([img_only]))
        assert seen == [], seen
        print('OK')
    """))


def test_real_memory_places_block_in_system_message():
    # Gate item 4: confirm _aget's return value actually reaches the
    # model's context, and where. Memory.aget() is what every LlamaIndex
    # agent calls to build its next model input — plugging blk into a real
    # Memory (not calling _aget directly) also proves the documented
    # plug-in mechanism (Memory.from_defaults(memory_blocks=[...])) accepts
    # this adapter's output.
    _ok(_run("""
        from llama_index.core.memory import Memory

        memory = Memory.from_defaults(session_id='t1', memory_blocks=[blk])
        built = asyncio.run(memory.aget(input='what do I like?'))
        assert built[0].role.value == 'system', built
        assert 'You like Python' in built[0].content, built[0].content
        print('OK')
    """))


# Dedented on its own, independently of whatever test-specific snippet it
# gets concatenated with below. textwrap.dedent() strips the *minimum*
# common indentation across an entire string — concatenating this (written
# at one indentation level in this module) with a test's snippet (written
# at a deeper indentation level inside a function body) and dedenting the
# combined result once would strip only the smaller of the two, leaving the
# test-specific half still indented and silently nested inside StubLLM's
# class body instead of following it. Pre-dedenting each piece separately
# avoids that (see tests/sdk_public/test_adapter_crewai.py, same trap).
#
# A minimal FunctionCallingLLM: llama-index-core ships no MockLLM. Scripts a
# single tool call (get_dog_name) then a final answer, driving FunctionAgent
# through its real tool-calling loop rather than a single-shot response.
_STUB_LLM = textwrap.dedent("""
    from llama_index.core.agent.workflow import FunctionAgent
    from llama_index.core.base.llms.types import ChatResponse, LLMMetadata
    from llama_index.core.llms.function_calling import FunctionCallingLLM
    from llama_index.core.memory import Memory
    from llama_index.core.tools import FunctionTool, ToolSelection

    def get_dog_name() -> str:
        return 'Rex'

    class StubLLM(FunctionCallingLLM):
        step: int = 0

        @property
        def metadata(self):
            return LLMMetadata(is_function_calling_model=True, is_chat_model=True)

        def _prepare_chat_with_tools(self, tools, user_msg=None, chat_history=None, **kw):
            return {'messages': chat_history or []}

        def get_tool_calls_from_response(self, response, error_on_no_tool_call=True, **kw):
            return response.message.additional_kwargs.get('tool_calls', [])

        async def achat(self, messages, **kw):
            self.step += 1
            if self.step == 1:
                return ChatResponse(message=ChatMessage(
                    role='assistant', content='',
                    additional_kwargs={'tool_calls': [
                        ToolSelection(tool_id='1', tool_name='get_dog_name', tool_kwargs={}),
                    ]},
                ))
            return ChatResponse(message=ChatMessage(
                role='assistant', content='Your dog is named Rex'))

        def chat(self, messages, **kw):
            raise NotImplementedError

        def stream_chat(self, messages, **kw):
            raise NotImplementedError

        def complete(self, prompt, **kw):
            raise NotImplementedError

        def stream_complete(self, prompt, **kw):
            raise NotImplementedError

        async def acomplete(self, prompt, **kw):
            raise NotImplementedError

        async def astream_chat(self, messages, **kw):
            raise NotImplementedError

        async def astream_complete(self, prompt, **kw):
            raise NotImplementedError
""")


def test_real_tool_calling_turn_dedupes_query_and_flushes_a_complete_batch():
    # Verification gate item 1 (call granularity), against a REAL
    # FunctionAgent tool-calling turn driving a REAL Memory wired with the
    # REAL AnonaMemoryBlock — not a probe, not a direct _aget/_aput call.
    # Also covers the block's short-TTL cache (I3): real orchestration
    # calls _aget 3 times for this turn, all with the same query, and only
    # one should reach Anona.
    #
    # _aput is not a per-turn hook here (see the adapter's module
    # docstring): Memory only waterfalls messages out to a block once its
    # short-term token budget is exceeded. A tiny budget plus pre-seeded
    # history forces that eviction to happen within this one turn, which is
    # also what proves _aput never receives an orphaned, answerless
    # fragment the way it did for the LangChain adapter's original bug —
    # the flushed batch for this turn must carry the question AND the
    # final answer together, never just one.
    _ok(_run(_STUB_LLM + textwrap.dedent("""
        # FunctionAgent.run() is a plain (non-async) method that schedules
        # work on the *currently running* loop and hands back an awaitable
        # handler -- unlike Memory's own methods, it cannot be wrapped as
        # asyncio.run(agent.run(...)) from outside a loop (that evaluates
        # agent.run(...) before asyncio.run() has started one). Everything
        # goes through one async main() + one asyncio.run(main()), matching
        # how a real caller would drive it.
        async def main():
            memory = Memory.from_defaults(
                session_id='t1', memory_blocks=[blk],
                token_limit=100, chat_history_token_ratio=0.3, token_flush_size=15,
            )

            # Old history, seeded directly (no agent involved yet) so the
            # buffer starts close to its budget before the real turn runs.
            old = []
            for i in range(6):
                old.append(ChatMessage(role='user', content=f'old question {i} padding padding padding padding'))
                old.append(ChatMessage(role='assistant', content=f'old answer {i} padding padding padding padding'))
            await memory.aput_messages(old)

            tool = FunctionTool.from_defaults(fn=get_dog_name, description="Return the dog's name.")
            agent = FunctionAgent(tools=[tool], llm=StubLLM(), streaming=False)
            resp = await agent.run(user_msg="what's my dog's name?", memory=memory)
            assert 'Rex' in str(resp), resp

            # Push filler turns to evict this turn's own messages out of the
            # short-term buffer and into _aput.
            for i in range(6):
                await memory.aput_messages([
                    ChatMessage(role='user', content=f'filler {i} padding padding padding padding'),
                    ChatMessage(role='assistant', content=f'filler reply {i} padding padding padding padding'),
                ])

        asyncio.run(main())

        retrieve_calls = [s for s in seen if s[1].endswith('/v1/retrieve')]
        record_calls = [s for s in seen if s[1].endswith('/v1/record')]

        # Real orchestration calls Memory.aget() (hence blk._aget) 3 times
        # for this one-tool-round turn (init_run, aggregate_tool_results
        # after the tool result, and a post-finalize refetch) -- all with
        # the same query text, verified by removing the cache locally and
        # observing 3 distinct retrieve calls here. The block's own
        # short-TTL cache collapses that to exactly one live call.
        turn_retrieves = [c for c in retrieve_calls if c[2]['query'] == "what's my dog's name?"]
        assert len(turn_retrieves) == 1, retrieve_calls

        # Exactly one record call is this turn, and it is the complete
        # exchange -- never split across multiple _aput calls, never
        # missing the final answer, never the empty tool-call fragment or
        # the raw tool result on its own.
        turn_records = [c for c in record_calls if "what's my dog's name?" in c[2]['content']]
        assert len(turn_records) == 1, record_calls
        assert turn_records[0][2]['content'] == (
            "User: what's my dog's name?\\nAssistant: Your dog is named Rex"
        ), turn_records
        print('OK')
    """)))


def test_import_error_names_the_extra():
    _ok(_run("""
        import builtins
        real = builtins.__import__
        def fake(name, *a, **kw):
            if name.startswith('llama_index'):
                raise ImportError('nope')
            return real(name, *a, **kw)
        builtins.__import__ = fake
        for mod in [m for m in list(sys.modules) if m.startswith('llama_index')]:
            del sys.modules[mod]
        try:
            from anona.integrations.llamaindex import AnonaMemoryBlock
            AnonaMemoryBlock(bridge=b)
        except ImportError as e:
            assert 'llamaindex' in str(e), e
            print('OK')
        else:
            print('NO ERROR RAISED')
    """))


def test_old_llama_index_version_raises_friendly_error():
    # I1: llama_index.core.memory imports fine on every 0.12.x release, but
    # Memory/BaseMemoryBlock (what this adapter needs) don't exist until
    # 0.12.35 -- older 0.12.x satisfies the package's own >=0.14 floor's
    # predecessor just fine and would otherwise hit a bare AttributeError
    # two lines past require()'s ImportError-only catch. Simulates that
    # shape (module present, attribute missing) by deleting the attribute
    # from the already-imported module, rather than installing an old
    # version — reproduces exactly what require() cannot see.
    _ok(_run("""
        import llama_index.core.memory as m
        real_block = m.BaseMemoryBlock
        del m.BaseMemoryBlock
        try:
            AnonaMemoryBlock(bridge=b)
        except ImportError as e:
            assert 'llama-index-core>=0.14' in str(e), e
            assert 'BaseMemoryBlock' in str(e), e
            print('OK')
        except AttributeError as e:
            raise AssertionError(f'UNFRIENDLY failure -- AttributeError: {e}')
        finally:
            m.BaseMemoryBlock = real_block
    """))
