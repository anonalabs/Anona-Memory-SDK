"""LangChain / LangGraph adapter tests.

Skips when langchain-core is absent — CI does not install the optional extras.
Subprocess isolation for the same reason as the other public-SDK tests.

The middleware tests build a real compiled ``create_agent`` graph rather than
calling a hook directly. A first round of this file called
``before_model``/``after_model`` by hand and every assertion passed — but
that bypasses exactly the two mechanisms that broke in a real agent:
LangGraph's ``add_messages`` reducer (which reorders a message with no
``.id``) and the fact that ``before_model``/``after_model`` fire once per
*model step*, not once per turn. A spy chat model inside a compiled graph
puts both back in the loop, so a regression to either shows up here the way
it would for a real user.

A second round added ``test_middleware_preserves_user_system_prompt`` (the
``wrap_model_call`` fix from round one turned out to silently discard a
caller's own ``create_agent(system_prompt=...)`` — replacing rather than
composing) and two tests around the per-run context cache added to stop
``awrap_model_call`` re-fetching (and re-billing) the same retrieve call once
per model step of a tool loop.
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
    not _installed("langchain_core"), reason="langchain not installed"
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
    )
    script = header + textwrap.dedent(snippet)
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )


def _ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_retriever_returns_context_document():
    _ok(_run("""
        from anona.integrations.langchain import AnonaRetriever
        r = AnonaRetriever(bridge=b)
        docs = asyncio.run(r._aget_relevant_documents('what do I like?'))
        assert len(docs) == 1
        assert docs[0].page_content == 'You like Python'
        print('OK')
    """))


def test_retriever_returns_nothing_when_no_memories():
    _ok(_run("""
        from anona.integrations.langchain import AnonaRetriever
        def empty(request):
            return httpx.Response(200, json={'context': ''})
        b._client._async_client = httpx.AsyncClient(transport=httpx.MockTransport(empty))
        r = AnonaRetriever(bridge=b)
        docs = asyncio.run(r._aget_relevant_documents('q'))
        assert docs == [], docs
        print('OK')
    """))


def test_middleware_injects_system_message_ahead_of_conversation():
    # Regression (C1): before_model prepended the block into state['messages'],
    # which create_agent's default state schema merges through LangGraph's
    # add_messages reducer. That reducer reconciles by message .id; an
    # injected dict has none, so it was treated as a brand-new message and
    # appended at the end — landing after the conversation, not before it.
    # A compiled graph is required to catch this: calling the hook directly
    # and inspecting its return value never runs the reducer at all.
    _ok(_run("""
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage, SystemMessage, HumanMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        from langchain.agents import create_agent
        from anona.integrations.langchain import AnonaMemory

        class SpyModel(BaseChatModel):
            calls: list = []

            def _generate(self, messages, stop=None, run_manager=None, **kw):
                self.calls.append(list(messages))
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content='answer'))])

            @property
            def _llm_type(self):
                return 'spy'

        spy = SpyModel()
        agent = create_agent(model=spy, middleware=[AnonaMemory(bridge=b)])
        asyncio.run(agent.ainvoke({'messages': [{'role': 'user', 'content': 'what do I like?'}]}))

        assert len(spy.calls) == 1, len(spy.calls)
        sent = spy.calls[0]
        assert len(sent) == 2, [type(m).__name__ for m in sent]
        assert isinstance(sent[0], SystemMessage), type(sent[0])
        assert sent[0].content == 'You like Python', sent[0].content
        assert isinstance(sent[1], HumanMessage), type(sent[1])
        print('OK')
    """))


def test_middleware_preserves_user_system_prompt():
    # Regression (N1, introduced by the C1 fix above): the first fix set
    # request.system_message unconditionally via request.override(...),
    # which is a plain dataclasses.replace — it replaces the field, it
    # doesn't merge. create_agent(system_prompt=...) populates that same
    # field, so the model received only the memory block; the caller's own
    # system prompt vanished with no error, no warning, no log. Fix composes
    # instead: the caller's instructions first, the memory block after.
    _ok(_run("""
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage, SystemMessage, HumanMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        from langchain.agents import create_agent
        from anona.integrations.langchain import AnonaMemory

        class SpyModel(BaseChatModel):
            calls: list = []

            def _generate(self, messages, stop=None, run_manager=None, **kw):
                self.calls.append(list(messages))
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content='answer'))])

            @property
            def _llm_type(self):
                return 'spy'

        spy = SpyModel()
        agent = create_agent(
            model=spy,
            system_prompt='You are a helpful support assistant. Always be polite.',
            middleware=[AnonaMemory(bridge=b)],
        )
        asyncio.run(agent.ainvoke({'messages': [{'role': 'user', 'content': 'hi'}]}))

        sent = spy.calls[0]
        assert len(sent) == 2, [type(m).__name__ for m in sent]
        assert isinstance(sent[0], SystemMessage), type(sent[0])
        assert sent[0].content.startswith('You are a helpful support assistant'), sent[0].content
        assert sent[0].content.endswith('You like Python'), sent[0].content
        assert isinstance(sent[1], HumanMessage), type(sent[1])
        print('OK')
    """))


def test_middleware_records_turn_once_across_tool_call():
    # Regression (C2): after_model fires once per model *step*, not once per
    # turn. A one-tool turn is two steps (call the tool, then answer the
    # question), so the old hook called remember() twice: once after the
    # tool-call-only message (empty content — an orphaned, answerless
    # fragment stored as its own memory) and once after the real answer.
    # Needs a real tool-calling loop, not a hand-built two-message state, to
    # exercise the actual step count.
    _ok(_run("""
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        from langchain_core.tools import tool
        from langchain.agents import create_agent
        from anona.integrations.langchain import AnonaMemory

        @tool
        def get_dog_name() -> str:
            "Return the dog's name."
            return 'Rex'

        class SpyModel(BaseChatModel):
            step: int = 0

            def bind_tools(self, tools, **kw):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kw):
                if self.step == 0:
                    self.step += 1
                    return ChatResult(generations=[ChatGeneration(message=AIMessage(
                        content='',
                        tool_calls=[{'name': 'get_dog_name', 'args': {}, 'id': 'call_1'}],
                    ))])
                return ChatResult(generations=[ChatGeneration(
                    message=AIMessage(content='Your dog is named Rex'))])

            @property
            def _llm_type(self):
                return 'spy'

        agent = create_agent(model=SpyModel(), tools=[get_dog_name], middleware=[AnonaMemory(bridge=b)])
        asyncio.run(agent.ainvoke({'messages': [{'role': 'user', 'content': "what's my dog's name?"}]}))

        record_calls = [s for s in seen if s[1].endswith('/v1/record')]
        assert len(record_calls) == 1, record_calls
        _, _, body = record_calls[0]
        assert body['content'] == "User: what's my dog's name?\\nAssistant: Your dog is named Rex", body['content']
        print('OK')
    """))


def test_middleware_caches_context_within_one_tool_loop():
    # Regression (N2): awrap_model_call fires once per model step, and the
    # query text is byte-identical across every step of one tool-calling
    # turn (no new human message appears mid-loop) — measured by the
    # reviewer logging all three query bodies in a 3-step run. Without
    # caching, retrieve — metered, and the slowest call in the system — gets
    # re-fetched and re-billed once per step, serially blocking each one.
    _ok(_run("""
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        from langchain_core.tools import tool
        from langchain.agents import create_agent
        from anona.integrations.langchain import AnonaMemory

        @tool
        def get_dog_name() -> str:
            "Return the dog's name."
            return 'Rex'

        class SpyModel(BaseChatModel):
            step: int = 0

            def bind_tools(self, tools, **kw):
                return self

            def _generate(self, messages, stop=None, run_manager=None, **kw):
                if self.step == 0:
                    self.step += 1
                    return ChatResult(generations=[ChatGeneration(message=AIMessage(
                        content='',
                        tool_calls=[{'name': 'get_dog_name', 'args': {}, 'id': 'call_1'}],
                    ))])
                return ChatResult(generations=[ChatGeneration(
                    message=AIMessage(content='Your dog is named Rex'))])

            @property
            def _llm_type(self):
                return 'spy'

        agent = create_agent(model=SpyModel(), tools=[get_dog_name], middleware=[AnonaMemory(bridge=b)])
        asyncio.run(agent.ainvoke({'messages': [{'role': 'user', 'content': "what's my dog's name?"}]}))

        retrieve_calls = [s for s in seen if s[1].endswith('/v1/retrieve')]
        assert len(retrieve_calls) == 1, retrieve_calls
        print('OK')
    """))


def test_middleware_context_cache_does_not_leak_across_runs():
    # The per-run cache must be scoped to exactly one agent run, not the
    # middleware instance's lifetime — otherwise a long-lived instance would
    # serve a stale block to a later turn (or, on a shared instance, a
    # concurrent one — the bridge carries user/agent/session scope, so that
    # would be a cross-user leak, not just staleness). Same query text,
    # two separate ainvoke() calls on the same agent: each must issue its
    # own retrieve, proving the cache resets rather than surviving the run.
    _ok(_run("""
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        from langchain.agents import create_agent
        from anona.integrations.langchain import AnonaMemory

        class SpyModel(BaseChatModel):
            def _generate(self, messages, stop=None, run_manager=None, **kw):
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content='answer'))])

            @property
            def _llm_type(self):
                return 'spy'

        agent = create_agent(model=SpyModel(), middleware=[AnonaMemory(bridge=b)])
        asyncio.run(agent.ainvoke({'messages': [{'role': 'user', 'content': 'same question'}]}))
        asyncio.run(agent.ainvoke({'messages': [{'role': 'user', 'content': 'same question'}]}))

        retrieve_calls = [s for s in seen if s[1].endswith('/v1/retrieve')]
        assert len(retrieve_calls) == 2, retrieve_calls
        print('OK')
    """))


def test_middleware_fails_open():
    # Same fail-open contract as before the rework, now proven through a
    # real compiled graph: the agent must finish normally, not just avoid
    # raising inside a bare hook call.
    _ok(_run("""
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage, HumanMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        from langchain.agents import create_agent
        from anona.integrations.langchain import AnonaMemory

        def boom(request):
            return httpx.Response(500, json={'error': {'code': 'boom'}})
        b._client._async_client = httpx.AsyncClient(transport=httpx.MockTransport(boom))

        class SpyModel(BaseChatModel):
            calls: list = []

            def _generate(self, messages, stop=None, run_manager=None, **kw):
                self.calls.append(list(messages))
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content='answer'))])

            @property
            def _llm_type(self):
                return 'spy'

        spy = SpyModel()
        agent = create_agent(model=spy, middleware=[AnonaMemory(bridge=b)])
        result = asyncio.run(agent.ainvoke({'messages': [{'role': 'user', 'content': 'hi'}]}))

        sent = spy.calls[0]
        assert len(sent) == 1, [type(m).__name__ for m in sent]   # no system message injected
        assert isinstance(sent[0], HumanMessage)
        assert result['messages'][-1].content == 'answer'   # agent still ran normally
        print('OK')
    """))


def test_last_user_text_reads_real_message_objects():
    _ok(_run("""
        from langchain_core.messages import HumanMessage, AIMessage
        from anona.integrations.langchain import _last_user_text, _turn_text

        messages = [HumanMessage(content='what do I like?'), AIMessage(content='You like Python')]
        assert _last_user_text(messages) == 'what do I like?', _last_user_text(messages)
        assert _turn_text(messages) == 'User: what do I like?\\nAssistant: You like Python', _turn_text(messages)
        print('OK')
    """))


def test_text_extraction_handles_list_content_blocks():
    # Regression (C3): BaseMessage.content is str | list[str | dict] — the
    # standard LangChain multimodal shape, not an edge case. Reading raw
    # .content instead of .text meant _last_user_text handed a *list* to
    # MemoryBridge.context(), which fails its .strip() check and silently
    # returns no memories; _turn_text embedded the list's repr() in an
    # f-string, which passed the emptiness check and got POSTed as a real,
    # garbage memory.
    _ok(_run("""
        from langchain_core.messages import HumanMessage, AIMessage
        from anona.integrations.langchain import _last_user_text, _turn_text

        messages = [
            HumanMessage(content=[{'type': 'text', 'text': 'what do I like?'}]),
            AIMessage(content=[{'type': 'text', 'text': 'You like Python'}]),
        ]
        assert _last_user_text(messages) == 'what do I like?', _last_user_text(messages)
        assert _turn_text(messages) == 'User: what do I like?\\nAssistant: You like Python', _turn_text(messages)
        print('OK')
    """))


def test_import_error_names_the_extra():
    _ok(_run("""
        import builtins
        real = builtins.__import__
        def fake(name, *a, **kw):
            if name.startswith('langchain'):
                raise ImportError('nope')
            return real(name, *a, **kw)
        builtins.__import__ = fake
        for mod in [m for m in list(sys.modules) if m.startswith('langchain')]:
            del sys.modules[mod]
        if 'anona.integrations.langchain' in sys.modules:
            del sys.modules['anona.integrations.langchain']
        try:
            from anona.integrations.langchain import AnonaRetriever
            AnonaRetriever(bridge=b)
        except ImportError as e:
            assert "pip install 'anona[langchain]'" in str(e), str(e)
            print('OK')
    """))


# ---------------------------------------------------------------------------
# Real-socket regression. Every test above this line
# uses httpx.MockTransport, which never opens a socket and has no
# event-loop-bound state — structurally unable to catch this bug (see
# anona/client.py's AnonaClient._get_async_client() docstring for the full
# mechanism, and test_client_lifecycle.py / test_memory_bridge.py's own
# real-socket sections for the same regression at the AnonaClient/
# MemoryBridge layers). This is the layer that actually matters: LangChain
# is a real *async* framework, so a host embedding it drives create_agent
# with its own asyncio.run() — and the ordinary shape for a CLI, a
# synchronous Flask/Django view, or a Celery/RQ worker is one fresh event
# loop per turn, not one long-lived loop for the process's whole life.
# Measured against the pre-fix SDK: exactly 1 of 6 turns got memory injected
# (turn 1 only), while the server received all 6 retrieve calls — the
# other five turns' responses were lost client-side to the stale connection,
# not to the network.
# ---------------------------------------------------------------------------


def _run_real(snippet: str) -> subprocess.CompletedProcess:
    """Like ``_run``, but a REAL localhost socket instead of MockTransport —
    no transport is mocked at all; ``b`` is wired to a real HTTP server."""
    header = (
        "import sys\n"
        f"sys.path.insert(0, {str(_SDK_ROOT)!r})\n"
        "import json, threading\n"
        "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
        "from anona.integrations._core import MemoryBridge\n"
        "seen = []\n"
        "seen_lock = threading.Lock()\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    protocol_version = 'HTTP/1.1'\n"
        "    def log_message(self, *a): pass\n"
        "    def do_POST(self):\n"
        "        n = int(self.headers.get('Content-Length', 0))\n"
        "        body = self.rfile.read(n)\n"
        "        with seen_lock:\n"
        "            seen.append((self.path, json.loads(body or b'null')))\n"
        "        out = json.dumps({'context': 'You like Python',\n"
        "                          'memory_id': 'm1'}).encode()\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Type', 'application/json')\n"
        "        self.send_header('Content-Length', str(len(out)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(out)\n"
        "srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)\n"
        "port = srv.server_address[1]\n"
        "threading.Thread(target=srv.serve_forever, daemon=True).start()\n"
        "BASE = f'http://127.0.0.1:{port}'\n"
        "b = MemoryBridge(api_key='k', space_id='s1', base_url=BASE)\n"
    )
    script = header + textwrap.dedent(snippet)
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )


def test_middleware_survives_one_event_loop_per_turn_over_a_real_socket():
    # The actual bug shape, driven through the real adapter rather than the
    # bridge or the raw client: agent.ainvoke() wrapped in its own fresh
    # asyncio.run() per turn, six times — not asyncio.run(async def
    # six_turns(): ...) wrapping all six, which is the "one loop, many
    # awaits" shape that was never broken. Each turn's SpyModel call is
    # inspected directly (matching test_middleware_injects_system_message_
    # ahead_of_conversation's own assertion shape) rather than the crew/
    # strands tests' pattern of asserting on tool-return values, because
    # AnonaMemory has no return value of its own to inspect — the
    # observable effect is whether a SystemMessage reached the model.
    _ok(_run_real("""
        import asyncio
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage, SystemMessage, HumanMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        from langchain.agents import create_agent
        from anona.integrations.langchain import AnonaMemory

        class SpyModel(BaseChatModel):
            calls: list = []

            def _generate(self, messages, stop=None, run_manager=None, **kw):
                self.calls.append(list(messages))
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content='answer'))])

            @property
            def _llm_type(self):
                return 'spy'

        spy = SpyModel()
        agent = create_agent(model=spy, middleware=[AnonaMemory(bridge=b)])

        injected = []
        for i in range(6):
            asyncio.run(agent.ainvoke(
                {'messages': [{'role': 'user', 'content': f'what do I like? turn {i}'}]}))
            sent = spy.calls[-1]
            injected.append(len(sent) == 2 and isinstance(sent[0], SystemMessage)
                             and sent[0].content == 'You like Python')

        assert injected == [True] * 6, injected
        retrieve_calls = [s for s in seen if s[0].endswith('/v1/retrieve')]
        assert len(retrieve_calls) == 6, retrieve_calls
        print('OK')
    """))


def test_middleware_records_every_turn_over_one_event_loop_per_turn():
    # The write side of the same shape. Unlike the read side, a lost write
    # still reaches the server either way (record_calls == 6 held even
    # against the pre-fix SDK, confirmed directly — the same "billed
    # regardless" shape the CrewAI/Strands real-socket write tests
    # document); the actual signal is the spurious "failed to store turn"
    # warning aafter_agent's remember() logs when the connection reuse
    # itself fails, even though the write it was trying to report on had
    # already succeeded.
    _ok(_run_real("""
        import asyncio, logging
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        from langchain.agents import create_agent
        from anona.integrations.langchain import AnonaMemory

        warnings = []
        class CountHandler(logging.Handler):
            def emit(self, record):
                warnings.append(record.getMessage())
        log = logging.getLogger('anona.integrations')
        log.addHandler(CountHandler())
        log.propagate = False

        class SpyModel(BaseChatModel):
            def _generate(self, messages, stop=None, run_manager=None, **kw):
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content='answer'))])

            @property
            def _llm_type(self):
                return 'spy'

        agent = create_agent(model=SpyModel(), middleware=[AnonaMemory(bridge=b)])

        for i in range(6):
            asyncio.run(agent.ainvoke(
                {'messages': [{'role': 'user', 'content': f'turn {i}'}]}))

        record_calls = [s for s in seen if s[0].endswith('/v1/record')]
        assert len(record_calls) == 6, record_calls
        # Scoped to the write-side warning specifically ("failed to store
        # turn") rather than every warning this logger might emit: each
        # turn also calls bridge.context() first via awrap_model_call, so a
        # broken connection reuse logs its own separate "memory search
        # failed" warning on the read side too -- asserting on the full
        # (unfiltered) warnings list would still catch the same underlying
        # bug here, just for the wrong reason, muddying what this test is
        # actually pinning.
        store_warnings = [w for w in warnings if 'failed to store turn' in w]
        assert store_warnings == [], store_warnings
        print('OK')
    """))
