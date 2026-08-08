"""AWS Strands tool-based adapter tests.

Verification-gate context (see ``anona/integrations/strands.py``'s module
docstring for the full writeup): several tests here drive a real
``strands.Agent`` through a scripted tool-calling turn, not just direct calls
to the functions ``anona_tools`` returns. ``MockedModelProvider`` below is
ported from strands-agents' own test suite
(``strands-py/tests/fixtures/mocked_model_provider.py`` in
``strands-agents/sdk-python``) — it is not shipped in the installed wheel, so
it is reproduced here rather than imported, but it is what the framework's
own maintainers use to exercise this exact code path without live model
credentials: only the raw model response is scripted, everything above it
(tool registration, Pydantic input validation against each tool's own
schema, ``DecoratedFunctionTool.stream``, result wrapping, the follow-up
model call after a tool result) runs for real.
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
    not _installed("strands"), reason="strands-agents not installed"
)


def _run(snippet: str) -> subprocess.CompletedProcess:
    header = (
        "import sys\n"
        f"sys.path.insert(0, {str(_SDK_ROOT)!r})\n"
        "import json, httpx\n"
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
        # anona_recall_memory/anona_save_memory now drive AnonaClient's
        # *sync* methods (context_sync/remember_sync -- see C1), so
        # the sync httpx.Client needs the same mock as the async one.
        # Without this, an un-mocked sync client would make a REAL request
        # to http://t.local, fail (no such host), and fail open -- several
        # tests below would then pass for the wrong reason (a real network
        # error) instead of the mocked response they claim to exercise.
        "b._client._client = httpx.Client(\n"
        "    transport=httpx.MockTransport(handler),\n"
        "    headers={'Authorization': 'Bearer k'})\n"
        "from anona.integrations.strands import anona_tools\n"
        "tools = anona_tools(bridge=b)\n"
        "by_name = {getattr(t, 'tool_name', getattr(t, '__name__', '')): t for t in tools}\n"
        "\n"
        "# --- Real-Agent scaffolding -------------------------------------------\n"
        "from strands import Agent\n"
        "from strands.models import Model\n"
        "\n"
        "class MockedModelProvider(Model):\n"
        "    def __init__(self, agent_responses):\n"
        "        self.agent_responses = list(agent_responses)\n"
        "        self.index = 0\n"
        "    def format_chunk(self, event):\n"
        "        return event\n"
        "    def format_request(self, messages, tool_specs=None, system_prompt=None):\n"
        "        return None\n"
        "    def get_config(self):\n"
        "        pass\n"
        "    def update_config(self, **model_config):\n"
        "        pass\n"
        "    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):\n"
        "        pass\n"
        "    async def stream(self, messages, tool_specs=None, system_prompt=None, tool_choice=None, **kwargs):\n"
        "        for event in self._events(self.agent_responses[self.index]):\n"
        "            yield event\n"
        "        self.index += 1\n"
        "    def _events(self, message):\n"
        "        stop_reason = 'end_turn'\n"
        "        yield {'messageStart': {'role': 'assistant'}}\n"
        "        for content in message['content']:\n"
        "            if 'text' in content:\n"
        "                yield {'contentBlockStart': {'start': {}}}\n"
        "                yield {'contentBlockDelta': {'delta': {'text': content['text']}}}\n"
        "                yield {'contentBlockStop': {}}\n"
        "            if 'toolUse' in content:\n"
        "                stop_reason = 'tool_use'\n"
        "                yield {'contentBlockStart': {'start': {'toolUse': {\n"
        "                    'name': content['toolUse']['name'],\n"
        "                    'toolUseId': content['toolUse']['toolUseId'],\n"
        "                }}}}\n"
        "                yield {'contentBlockDelta': {'delta': {'toolUse': {\n"
        "                    'input': json.dumps(content['toolUse']['input'])\n"
        "                }}}}\n"
        "                yield {'contentBlockStop': {}}\n"
        "        yield {'messageStop': {'stopReason': stop_reason}}\n"
        "\n"
        "def tool_use_msg(tool_use_id, name, **kw):\n"
        "    return {'role': 'assistant',\n"
        "            'content': [{'toolUse': {'toolUseId': tool_use_id, 'name': name, 'input': kw}}]}\n"
        "\n"
        "def text_msg(t):\n"
        "    return {'role': 'assistant', 'content': [{'text': t}]}\n"
    )
    script = header + textwrap.dedent(snippet)
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )


def _ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# Direct-call tests. Fast, and enough for edge cases (scope forwarding, the
# exact request body) that don't need a real Agent turn to observe. Gate item
# 3 (argument shapes at the tool-call boundary) and item 4 (return-value
# consumption) are covered by the real-Agent tests further down, not here.
# ---------------------------------------------------------------------------


def test_returns_two_named_tools():
    _ok(_run("""
        assert len(tools) == 2, tools
        assert 'anona_recall_memory' in by_name, list(by_name)
        assert 'anona_save_memory' in by_name, list(by_name)
        print('OK')
    """))


def test_recall_returns_context():
    _ok(_run("""
        out = by_name['anona_recall_memory']('what do I like?')
        assert 'You like Python' in str(out), out
        _, url, body = seen[-1]
        assert url.endswith('/v1/retrieve')
        assert body['query'] == 'what do I like?', body
        print('OK')
    """))


def test_save_records():
    _ok(_run("""
        by_name['anona_save_memory']('the deadline moved to Friday')
        method, url, body = seen[-1]
        assert method == 'POST' and url.endswith('/v1/record')
        assert body['content'] == 'the deadline moved to Friday', body
        print('OK')
    """))


def test_recall_fails_open():
    _ok(_run("""
        def boom(request):
            return httpx.Response(500, json={'error': {'code': 'boom'}})
        b._client._async_client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
        b._client._client = httpx.Client(transport=httpx.MockTransport(boom))
        out = by_name['anona_recall_memory']('q')
        assert 'No relevant memories' in str(out), out
        print('OK')
    """))


def test_scope_forwarded():
    _ok(_run("""
        b2 = MemoryBridge(api_key='k', space_id='s1', base_url='http://t.local',
                          user_id='u1')
        b2._client._async_client = b._client._async_client
        b2._client._client = b._client._client
        t2 = {getattr(t, 'tool_name', getattr(t, '__name__', '')): t
              for t in anona_tools(bridge=b2)}
        t2['anona_save_memory']('x')
        _, _, body = seen[-1]
        assert body['user_id'] == 'u1'
        print('OK')
    """))


# ---------------------------------------------------------------------------
# Real-Agent tests -- the verification gate's actual mandate. Each of these
# drives strands.Agent end to end against MockedModelProvider: the framework
# registers the tools, validates the (scripted) model's tool-call input
# against each tool's own Pydantic schema, invokes the real
# anona_recall_memory/anona_save_memory functions, wraps their return value,
# and feeds the result back for a second real model call -- nothing here
# calls the tool functions directly.
# ---------------------------------------------------------------------------


def test_real_agent_recall_round_trip():
    """A scripted tool_use for anona_recall_memory, then a final answer.

    Confirms: the argument reaching the retrieve call is the plain string the
    (fake) model put in ``input.query`` -- not a dict, not a repr -- and that
    the *real* retrieved block is what actually reached the agent as the
    tool's result. That last part is checked by inspecting the ``toolResult``
    content in ``agent.messages`` directly, the same pattern
    ``test_real_agent_recall_fails_open_end_to_end`` below uses -- not by
    checking the final answer's text, which ``MockedModelProvider`` scripts
    independently of whatever the tool actually returned (its second-turn
    text is a fixed string regardless of the first turn's tool result, so it
    cannot prove this and previously didn't: mutating
    ``anona_recall_memory`` to discard ``bridge.context()``'s result and
    always return the empty-case string left this assertion passing).
    """
    _ok(_run("""
        model = MockedModelProvider([
            tool_use_msg('t1', 'anona_recall_memory', query='what do I like?'),
            text_msg('You like Python, based on memory.'),
        ])
        agent = Agent(model=model, tools=tools)
        agent('What do I like?')

        retrieve_calls = [s for s in seen if s[1].endswith('/v1/retrieve')]
        assert len(retrieve_calls) == 1, seen
        _, _, body = retrieve_calls[0]
        assert body['query'] == 'what do I like?', body
        assert isinstance(body['query'], str)

        tool_results = [
            c['toolResult']
            for m in agent.messages
            for c in m.get('content', [])
            if 'toolResult' in c
        ]
        matches = [r for r in tool_results if 'You like Python' in str(r)]
        assert matches, agent.messages
        assert matches[0]['status'] == 'success', matches[0]
        print('OK')
    """))


def test_real_agent_save_round_trip():
    """A scripted tool_use for anona_save_memory stores the exact content.

    A sibling adapter's worst bug was storing a stringified Python object
    (``"User: [{'type': 'text', 'text': '...'}]"``) because something
    non-string reached the record call. This pins that the content Strands'
    own input validation hands the tool -- and that reaches /v1/record -- is
    the plain string the model supplied, nothing wrapped or stringified.
    """
    _ok(_run("""
        model = MockedModelProvider([
            tool_use_msg('t1', 'anona_save_memory', content='the deadline moved to Friday'),
            text_msg('Got it, saved.'),
        ])
        agent = Agent(model=model, tools=tools)
        agent('Remember: the deadline moved to Friday')

        record_calls = [s for s in seen if s[1].endswith('/v1/record')]
        assert len(record_calls) == 1, seen
        _, _, body = record_calls[0]
        assert body['content'] == 'the deadline moved to Friday', body
        print('OK')
    """))


def test_real_agent_recall_and_save_in_one_turn():
    """Two toolUse blocks in one assistant message -- both execute.

    Strands supports more than one tool call per model turn (confirmed
    directly, separately from this suite, against a real Agent); this pins
    that both of our tools survive that path without the exact call count
    the plan cares about (one retrieve, one record) drifting.
    """
    _ok(_run("""
        model = MockedModelProvider([
            {'role': 'assistant', 'content': [
                {'toolUse': {'toolUseId': 't1', 'name': 'anona_recall_memory',
                             'input': {'query': 'what do I like?'}}},
                {'toolUse': {'toolUseId': 't2', 'name': 'anona_save_memory',
                             'input': {'content': 'asked about likes'}}},
            ]},
            text_msg('Done.'),
        ])
        agent = Agent(model=model, tools=tools)
        agent('go')

        routes = sorted(s[1].rsplit('/v1/', 1)[-1] for s in seen)
        assert routes == ['record', 'retrieve'], seen
        print('OK')
    """))


def test_real_agent_recall_fails_open_end_to_end():
    """Anona down -> the tool reports "no memories", the agent still answers.

    Drives the full pipeline (not a direct call): the retrieve call 500s,
    MemoryBridge.context() catches it and returns "", the tool wraps that as
    "No relevant memories found." -- as an ordinary *successful* tool result,
    confirmed by inspecting agent.messages, not an error one -- and the
    scripted second model call still produces a final answer.
    """
    _ok(_run("""
        def boom(request):
            return httpx.Response(500, json={'error': {'code': 'boom'}})
        b._client._async_client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
        b._client._client = httpx.Client(transport=httpx.MockTransport(boom))

        model = MockedModelProvider([
            tool_use_msg('t1', 'anona_recall_memory', query='what do I like?'),
            text_msg('I could not find anything specific, but in general...'),
        ])
        agent = Agent(model=model, tools=tools)
        result = agent('What do I like?')
        assert 'in general' in str(result), result

        tool_results = [
            c['toolResult']
            for m in agent.messages
            for c in m.get('content', [])
            if 'toolResult' in c
        ]
        matches = [r for r in tool_results if 'No relevant memories found' in str(r)]
        assert matches, agent.messages
        assert matches[0]['status'] == 'success', matches[0]
        print('OK')
    """))


def test_tool_name_collision_last_registered_silently_wins():
    """Pins the collision mechanism documented in the adapter's module
    docstring: two @tool-decorated functions sharing one name do not raise
    when registered on an Agent -- whichever is later in ``tools=[...]``
    silently overwrites the earlier one in the registry, no exception, no
    visible warning. This is why anona_tools() namespaces its tool names
    (``anona_recall_memory``/``anona_save_memory``) instead of using the
    brief's original bare ``recall_memory``/``save_memory`` -- confirmed
    Strands ships no built-in tool with either exact namespaced name, but the
    mechanism a bare name would be exposed to is real and silent either way.
    """
    _ok(_run("""
        from strands import tool as strands_tool

        @strands_tool(name='dup_probe')
        def ours(query: str) -> str:
            return 'OURS'

        @strands_tool(name='dup_probe')
        def theirs(query: str) -> str:
            return 'THEIRS'

        agent_a = Agent(model='unused', tools=[ours, theirs])
        agent_b = Agent(model='unused', tools=[theirs, ours])
        assert agent_a.tool_registry.registry['dup_probe']._tool_func.__name__ == 'theirs'
        assert agent_b.tool_registry.registry['dup_probe']._tool_func.__name__ == 'ours'
        print('OK')
    """))


def test_import_error_names_the_extra():
    # sys.modules['strands'] = None is the documented way to force the next
    # importlib.import_module('strands') -- what require() actually calls --
    # to raise ImportError. A patch of builtins.__import__ was tried first
    # and does NOT reliably catch this: importlib.import_module() resolves a
    # top-level name via sys.meta_path directly, never calling
    # builtins.__import__ for that top-level name at all, so a patch keyed on
    # the exact literal name 'strands' passes vacuously (confirmed: no
    # exception raised, anona_tools() just succeeds). A broader patch
    # (matching any name starting with "strands.") looked like it worked --
    # it does raise ImportError -- but for the wrong reason: strands' own
    # __init__ makes plain `import` statements for its submodules
    # internally, which *do* go through builtins.__import__, so the broader
    # patch was actually intercepting an internal submodule import deep in
    # strands' own load sequence, not simulating "not installed" at all.
    # sys.modules[name] = None sidesteps both failure modes.
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(_SDK_ROOT)!r})\n"
        "sys.modules['strands'] = None\n"
        "from anona.integrations._core import MemoryBridge\n"
        "b = MemoryBridge(api_key='k', space_id='s')\n"
        "from anona.integrations.strands import anona_tools\n"
        "try:\n"
        "    anona_tools(bridge=b)\n"
        "    print('NO ERROR RAISED')\n"
        "except ImportError as e:\n"
        "    assert \"anona[strands]\" in str(e), str(e)\n"
        "    print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    _ok(result)


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
        "from anona.integrations.strands import anona_tools\n"
        "tools = anona_tools(bridge=b)\n"
        "by_name = {getattr(t, 'tool_name', getattr(t, '__name__', '')): t for t in tools}\n"
    )
    script = header + textwrap.dedent(snippet)
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )


def test_recall_survives_sequential_real_socket_calls():
    # Before the fix, anona_recall_memory ran _sync(bridge.context(...)) --
    # a fresh asyncio.run() event loop per call, reusing AnonaClient's
    # pooled async httpx client whose keep-alive connection stays bound to
    # whichever loop created it (the first one). Every other sequential
    # call inherited a connection bound to a now-dead loop and failed,
    # silently, into the bridge's fail-open `except Exception` -- a real
    # agent's second anona_recall_memory call in one conversation would
    # come back "No relevant memories found." while the API had already
    # executed (and billed) the retrieve. Calling the SAME tool repeatedly
    # is what reproduces this -- confirmed directly (4/8 real, 4/8 silently
    # "No relevant memories found", an OK/lost/OK/lost pattern matching the
    # review's measurement); alternating with anona_save_memory on the same
    # bridge does not reliably land on the failing parity. This test calls
    # nothing but the tool function anona_tools() actually returns, so it
    # holds regardless of implementation.
    _ok(_run_real("""
        results = [by_name['anona_recall_memory'](f'q{i}') for i in range(8)]
        assert results == ['You like Python'] * 8, results
        print('OK')
    """))


def test_save_survives_sequential_real_socket_calls_without_logging_failure():
    # Same root cause as the recall regression above, applied to the write
    # side. anona_save_memory always returns "Saved." regardless of success
    # (MemoryBridge.remember fails open by design), so the observable
    # signal here is the warning it logs on a call that hit the
    # stale-connection exception -- confirmed directly against the unfixed
    # code: repeated anona_save_memory calls all still reach the server on
    # loopback (a write "lost" by this bug is still billed -- production,
    # over real TLS rather than loopback, is likely worse, not better, per
    # the review), but 4 of 8 logged "anona: failed to store turn": a
    # spurious, customer-visible failure log for writes that had actually
    # succeeded.
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
            by_name['anona_save_memory'](f't{i}')

        assert warnings == [], warnings
        record_calls = [c for c in seen if c[0] == '/v1/record']
        assert len(record_calls) == 8, record_calls
        print('OK')
    """))
