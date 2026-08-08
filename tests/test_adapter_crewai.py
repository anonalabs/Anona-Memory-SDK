"""CrewAI memory-tools adapter tests.

Skips when crewai is absent. Subprocess isolation for the same reason as
the other tests here: the import under test must resolve to this package.

CrewAI's ``ExternalMemory``/``Storage`` (``save``/``search``/``reset`` on a
class dropped into ``Crew(memory=...)``) does not exist in the installed
package (crewai 1.15.13) or in CrewAI's current docs — replaced by a
``Memory`` class whose pluggable ``StorageBackend`` only ever receives a
pre-computed embedding vector in ``search()``, never the query text, so a
backend can't forward real search to Anona through that seam (see the
module docstring in ``anona/integrations/crewai.py`` for the full trail).
``AnonaStorage`` keeps a plain, directly-testable ``save``/``search``/
``reset`` — the first six tests below exercise that directly — but the
actual integration point is :meth:`AnonaStorage.as_tools`, two
``@tool``-decorated functions an agent calls itself. The last two tests
build a real, compiled ``Crew``/``Agent``/``Task`` with a scripted stub LLM
and run it end to end: a direct-call test of ``AnonaStorage`` alone would
never exercise CrewAI's own tool-argument validation or its ReAct
tool-calling loop, which is exactly where the two bug classes this task
was told to guard against would show up — over-eager save granularity, and
a non-string argument reaching Anona.
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
    not _installed("crewai"), reason="crewai not installed"
)


def _run(snippet: str) -> subprocess.CompletedProcess:
    header = (
        "import sys\n"
        f"sys.path.insert(0, {str(_SDK_ROOT)!r})\n"
        "import json, httpx, logging\n"
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
        # AnonaStorage.search()/.save() now drive AnonaClient's *sync*
        # methods (context_sync/remember_sync — see C1), so the
        # sync httpx.Client needs the same mock as the async one. Without
        # this, an un-mocked sync client would make a REAL request to
        # http://t.local, fail (no such host), and fail open — several
        # tests below would then pass for the wrong reason (a real network
        # error) instead of the mocked response they claim to exercise.
        "b._client._client = httpx.Client(\n"
        "    transport=httpx.MockTransport(handler),\n"
        "    headers={'Authorization': 'Bearer k'})\n"
        "from anona.integrations.crewai import AnonaStorage\n"
        "s = AnonaStorage(bridge=b)\n"
    )
    script = header + textwrap.dedent(snippet)
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )


def _ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_save_records():
    _ok(_run("""
        s.save('the deadline moved to Friday', {'agent': 'planner'})
        method, url, body = seen[-1]
        assert method == 'POST' and url.endswith('/v1/record')
        assert 'deadline moved to Friday' in body['content']
        print('OK')
    """))


def test_search_returns_result_dicts():
    _ok(_run("""
        out = s.search('what do I like?')
        assert isinstance(out, list) and len(out) == 1, out
        assert out[0]['context'] == 'You like Python', out
        _, url, body = seen[-1]
        assert url.endswith('/v1/retrieve')
        assert body['format'] == 'block'
        print('OK')
    """))


def test_search_empty_when_no_memories():
    _ok(_run("""
        def empty(request):
            return httpx.Response(200, json={'context': ''})
        b._client._async_client = httpx.AsyncClient(transport=httpx.MockTransport(empty))
        b._client._client = httpx.Client(transport=httpx.MockTransport(empty))
        assert s.search('q') == []
        print('OK')
    """))


def test_search_fails_open():
    _ok(_run("""
        def boom(request):
            return httpx.Response(500, json={'error': {'code': 'boom'}})
        b._client._async_client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
        b._client._client = httpx.Client(transport=httpx.MockTransport(boom))
        assert s.search('q') == []
        print('OK')
    """))


def test_reset_is_a_noop_that_warns():
    _ok(_run("""
        import io
        stream = io.StringIO()
        h = logging.StreamHandler(stream)
        log = logging.getLogger('anona.integrations')
        log.addHandler(h); log.setLevel(logging.WARNING)
        s.reset()                       # must not raise
        assert seen == [], seen         # and must not delete anything
        assert 'dashboard' in stream.getvalue().lower(), stream.getvalue()
        print('OK')
    """))


def test_scope_forwarded():
    _ok(_run("""
        b2 = MemoryBridge(api_key='k', space_id='s1', base_url='http://t.local',
                          user_id='u1')
        b2._client._async_client = b._client._async_client
        b2._client._client = b._client._client
        from anona.integrations.crewai import AnonaStorage
        AnonaStorage(bridge=b2).save('x', None)
        _, _, body = seen[-1]
        assert body['user_id'] == 'u1'
        print('OK')
    """))


def test_save_skips_non_string_value():
    # Regression guard for the LangChain adapter's worst bug (a raw list
    # stringified into a stored memory): a non-string value must be
    # skipped, not str()-coerced into garbage.
    _ok(_run("""
        s.save(['a', 'b'], None)
        s.save({'type': 'text', 'text': 'hi'}, None)
        assert seen == [], seen
        print('OK')
    """))


def test_search_skips_non_string_query():
    _ok(_run("""
        assert s.search(['a', 'b']) == []
        assert s.search(None) == []
        assert seen == [], seen
        print('OK')
    """))


def test_import_error_names_the_extra():
    _ok(_run("""
        import builtins
        real = builtins.__import__
        def fake(name, *a, **kw):
            if name.startswith('crewai'):
                raise ImportError('nope')
            return real(name, *a, **kw)
        builtins.__import__ = fake
        for mod in [m for m in list(sys.modules) if m.startswith('crewai')]:
            del sys.modules[mod]
        if 'anona.integrations.crewai' in sys.modules:
            del sys.modules['anona.integrations.crewai']
        try:
            from anona.integrations.crewai import AnonaStorage
            AnonaStorage(bridge=b)
        except ImportError as e:
            assert "pip install 'anona[crewai]'" in str(e), str(e)
            print('OK')
    """))


def test_as_tools_shape():
    # Direct sanity check on the tool pair's own shape, cheap and fast,
    # ahead of the full real-crew tests below. Names are prefixed "Anona: "
    # so neither can collide with CrewAI's own built-in memory tools
    # (crewai/tools/memory_tools.py: "Search memory" / "Save to memory") —
    # see test_our_search_tool_survives_crewai_builtin_memory_tools below.
    _ok(_run("""
        tools = s.as_tools()
        assert len(tools) == 2, tools
        names = {t.name for t in tools}
        assert names == {'Anona: Search memory', 'Anona: Save memory'}, names
        search_tool, save_tool = (
            (tools[0], tools[1]) if tools[0].name == 'Anona: Search memory' else (tools[1], tools[0])
        )
        assert set(search_tool.args_schema.model_fields) == {'query'}
        assert set(save_tool.args_schema.model_fields) == {'content'}
        print('OK')
    """))


# Dedented on its own, independently of whatever test-specific snippet it
# gets concatenated with below. textwrap.dedent() strips the *minimum*
# common indentation across an entire string — concatenating this (written
# at one indentation level in this module) with a test's snippet (written
# at a deeper indentation level inside a function body) and dedenting the
# combined result once would strip only the smaller of the two, leaving
# the test-specific half still indented and silently nested inside
# StubLLM's class body instead of following it. Pre-dedenting each piece
# separately avoids that.
_STUB_LLM = textwrap.dedent("""
    from crewai.llms.base_llm import BaseLLM
    from crewai import Agent, Task, Crew

    class StubLLM(BaseLLM):
        script: list = []
        calls: list = []

        def call(self, messages, tools=None, callbacks=None,
                  available_functions=None, from_task=None, from_agent=None,
                  response_model=None):
            self.calls.append(messages)
            idx = len(self.calls) - 1
            if idx < len(self.script):
                return self.script[idx]
            return 'Thought: done\\nFinal Answer: fallback'
""")


def test_real_crew_search_and_save_tools_hit_anona():
    # Exercises call granularity (item 1) and argument shape (item 2) from
    # the verification gate against a REAL compiled Crew/Agent/Task, not a
    # direct method call on AnonaStorage — a direct-call test would pass
    # even if the tool wiring were broken (wrong kwarg name, wrong
    # args_schema, tool never actually reachable by the agent). The stub
    # LLM scripts one search call, one save call, then a final answer;
    # CrewAI's own ReAct loop parses the actions, validates the tool
    # arguments against each tool's auto-derived args_schema, and invokes
    # search_memory/save_memory itself.
    _ok(_run(_STUB_LLM + textwrap.dedent("""
        tools = s.as_tools()
        stub = StubLLM(
            model='stub/stub',
            script=[
                'Thought: check memory first\\nAction: Anona: Search memory\\n'
                'Action Input: {"query": "what does the user like?"}',
                'Thought: remember the new deadline\\nAction: Anona: Save memory\\n'
                'Action Input: {"content": "the deadline moved to Friday"}',
                'Thought: I now know the final answer\\n'
                'Final Answer: The user likes Python. Saved the new deadline.',
            ],
        )
        agent = Agent(
            role='Assistant', goal='Answer using memory',
            backstory='A helpful assistant with access to memory tools.',
            llm=stub, tools=tools, verbose=False,
        )
        task = Task(
            description="What does the user like? Also remember the deadline moved to Friday.",
            expected_output='A short answer.', agent=agent,
        )
        crew = Crew(agents=[agent], tasks=[task], verbose=False)
        result = crew.kickoff()

        assert 'likes Python' in str(result), result
        retrieve_calls = [c for c in seen if c[1].endswith('/v1/retrieve')]
        record_calls = [c for c in seen if c[1].endswith('/v1/record')]
        assert len(retrieve_calls) == 1, retrieve_calls   # not called once per step
        assert len(record_calls) == 1, record_calls       # not called once per step
        assert retrieve_calls[0][2]['query'] == 'what does the user like?', retrieve_calls
        assert record_calls[0][2]['content'] == 'the deadline moved to Friday', record_calls
        print('OK')
    """)))


def test_real_crew_tool_rejects_non_string_argument():
    # A malformed Action Input (query as a list of content blocks, the same
    # shape that broke the LangChain adapter) must never reach Anona.
    # CrewAI validates the tool call against the auto-derived args_schema
    # (query: str) before AnonaStorage.search ever runs, surfaces the
    # rejection to the agent as a tool error, and the agent recovers — the
    # crew must finish normally, not crash.
    _ok(_run(_STUB_LLM + textwrap.dedent("""
        tools = s.as_tools()
        stub = StubLLM(
            model='stub/stub',
            script=[
                'Thought: bad shape\\nAction: Anona: Search memory\\n'
                'Action Input: {"query": [{"type": "text", "text": "hi"}]}',
                'Thought: retry with a string\\nAction: Anona: Search memory\\n'
                'Action Input: {"query": "what do I like?"}',
                'Thought: I now know the final answer\\nFinal Answer: recovered fine',
            ],
        )
        agent = Agent(
            role='Assistant', goal='Answer using memory',
            backstory='A helpful assistant with access to memory tools.',
            llm=stub, tools=tools, verbose=False,
        )
        task = Task(description='Test bad tool-call shapes.',
                     expected_output='A short answer.', agent=agent)
        crew = Crew(agents=[agent], tasks=[task], verbose=False)
        result = crew.kickoff()

        assert 'recovered fine' in str(result), result
        retrieve_calls = [c for c in seen if c[1].endswith('/v1/retrieve')]
        assert len(retrieve_calls) == 1, retrieve_calls          # only the valid retry
        assert retrieve_calls[0][2]['query'] == 'what do I like?', retrieve_calls
        print('OK')
    """)))


def test_our_search_tool_survives_crewai_builtin_memory_tools():
    # Regression (F1, fix round 1): CrewAI auto-injects its own memory tools
    # (crewai/tools/memory_tools.py, RecallMemoryTool/RememberTool) whenever
    # Crew(memory=True) or an agent-level memory=True is set, and
    # Crew._merge_tools dedups by sanitized tool name, keeping whichever tool
    # object was passed to it *last* — the auto-injected built-in, not ours.
    # Reproduced directly (crew._prepare_tools(agent, task, our_tools)) before
    # fixing: with our search tool named "Search memory", identical to
    # CrewAI's own RecallMemoryTool, the merged list's "Search memory" entry
    # was CrewAI's RecallMemoryTool, not AnonaStorage's — a customer with an
    # existing memory=True crew would get answers from CrewAI's local
    # LanceDB/OpenAI-backed memory with no error, no warning, nothing in any
    # log. Needs a real Crew with memory=True *and* our tools attached, and a
    # real kickoff() (not a direct _prepare_tools call) so the same merge
    # CrewAI performs before every task actually runs.
    _ok(_run(_STUB_LLM + textwrap.dedent("""
        tools = s.as_tools()
        search_name = [t.name for t in tools if 'earch' in t.name][0]
        stub = StubLLM(
            model='stub/stub',
            script=[
                f'Thought: search\\nAction: {search_name}\\n'
                'Action Input: {"query": "what do I like?"}',
                'Thought: done\\nFinal Answer: got it',
            ],
        )
        agent = Agent(
            role='Assistant', goal='Answer using memory',
            backstory='A helpful assistant with access to memory tools.',
            llm=stub, tools=tools, memory=True, verbose=False,
        )
        task = Task(description='What do I like?', expected_output='short', agent=agent)
        crew = Crew(agents=[agent], tasks=[task], memory=True, verbose=False)
        result = crew.kickoff()

        # If CrewAI's built-in shadowed ours, seen stays empty — the built-in
        # answers from its own local store and Anona's mock transport is
        # never hit.
        retrieve_calls = [c for c in seen if c[1].endswith('/v1/retrieve')]
        assert len(retrieve_calls) == 1, ('our search tool was shadowed', seen)
        assert retrieve_calls[0][2]['query'] == 'what do I like?', retrieve_calls
        print('OK')
    """)))


# ---------------------------------------------------------------------------
# Real-socket regression. Every test above this
# line uses httpx.MockTransport, which never opens a socket and has no
# event-loop-bound state -- structurally unable to catch this bug, which is
# exactly why it shipped past 118 passing tests. See
# tests/sdk_public/test_memory_bridge.py's _run_real docstring and
# anona/integrations/_core.py's MemoryBridge.context_sync docstring for the
# full mechanism.
# ---------------------------------------------------------------------------


def _run_real(snippet: str) -> subprocess.CompletedProcess:
    """Like ``_run``, but a REAL localhost socket instead of MockTransport."""
    header = (
        "import sys\n"
        f"sys.path.insert(0, {str(_SDK_ROOT)!r})\n"
        "import json, threading\n"
        "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
        "from anona.integrations._core import MemoryBridge\n"
        "seen = []\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    protocol_version = 'HTTP/1.1'\n"
        "    def log_message(self, *a): pass\n"
        "    def do_POST(self):\n"
        "        n = int(self.headers.get('Content-Length', 0))\n"
        "        body = self.rfile.read(n)\n"
        "        seen.append((self.path, json.loads(body or b'null')))\n"
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
        "from anona.integrations.crewai import AnonaStorage\n"
        "s = AnonaStorage(bridge=b)\n"
    )
    script = header + textwrap.dedent(snippet)
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )


def test_search_survives_sequential_real_socket_calls():
    # Before the fix, AnonaStorage.search() ran _sync(bridge.context(...))
    # -- a fresh asyncio.run() event loop per call, reusing AnonaClient's
    # pooled async httpx client whose keep-alive connection stays bound to
    # whichever loop created it (the first one). Every other sequential
    # call inherited a connection bound to a now-dead loop and failed,
    # silently, into the bridge's fail-open `except Exception` -- a real
    # crew agent's second "Anona: Search memory" call would come back empty
    # while the API had already executed (and billed) the retrieve.
    # Calling the SAME method repeatedly is what reproduces this --
    # confirmed directly against the unfixed code (4/8 real results, 4/8
    # silently None, an OK/lost/OK/lost pattern matching the review's
    # measurement); alternating search() with save() on the same bridge
    # does not reliably land on the failing parity, so it stayed 8/8 even
    # unfixed and would have made a weak regression test. This test calls
    # nothing but AnonaStorage's own public surface, so it holds regardless
    # of implementation.
    _ok(_run_real("""
        results = [s.search(f'q{i}') for i in range(8)]
        contexts = [r[0]['context'] if r else None for r in results]
        assert contexts == ['You like Python'] * 8, contexts
        print('OK')
    """))


def test_save_survives_sequential_real_socket_calls_without_logging_failure():
    # Same root cause as the search regression above, applied to the write
    # side. save() never surfaces success/failure through its return value
    # (MemoryBridge.remember fails open by design), so the observable
    # signal here is the warning it logs on a call that hit the
    # stale-connection exception -- confirmed directly against the unfixed
    # code: repeated save() calls all still reach the server on loopback
    # (a write "lost" by this bug is still billed -- production, over real
    # TLS rather than loopback, is likely worse, not better, per the
    # review), but 4 of 8 logged "anona: failed to store turn": a spurious,
    # customer-visible failure log for writes that had actually succeeded.
    _ok(_run_real("""
        import logging
        warnings = []
        class CountHandler(logging.Handler):
            def emit(self, record):
                warnings.append(record.getMessage())
        log = logging.getLogger('anona.integrations')
        log.addHandler(CountHandler())
        log.propagate = False

        for i in range(8):
            s.save(f't{i}')

        assert warnings == [], warnings
        record_calls = [c for c in seen if c[0] == '/v1/record']
        assert len(record_calls) == 8, record_calls
        print('OK')
    """))
