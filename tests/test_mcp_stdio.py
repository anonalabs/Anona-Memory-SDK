"""Stdio MCP-server tests.

Covers three defects: the stdio record tool discarded its result and always answered
"Stored" (misreporting the ~44% of terse writes that extract to nothing); the
four tools took no user_id/agent_id/session_id scope and reason took no model;
and a fresh AnonaClient (new TLS pool) was built per tool call.

Runs in an isolated subprocess (only this package on sys.path) so the
module-level client singleton starts fresh for every case. The API is mocked
with respx.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

_SDK_ROOT = Path(__file__).resolve().parent.parent


def _run(snippet: str) -> subprocess.CompletedProcess:
    header = (
        "import sys, os, json\n"
        f"sys.path.insert(0, {str(_SDK_ROOT)!r})\n"
        "import httpx, respx\n"
        "os.environ['ANONA_API_KEY'] = 'anona_live_testkey'\n"
        "os.environ['ANONA_BASE_URL'] = 'http://t.local'\n"
        "os.environ.pop('ANONA_SPACE_ID', None)\n"
        "from anona.integrations import mcp as m\n"
        "BASE = 'http://t.local'\n"
        "SPACE = 'space-1'\n"
    )
    script = header + textwrap.dedent(snippet)
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )


def _ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_record_reports_stored_when_memory_created():
    _ok(_run("""
        with respx.mock:
            respx.post(f'{BASE}/v1/record').mock(return_value=httpx.Response(
                201, json={'memory_id': 'm1', 'memory_ids': ['m1'],
                           'status': 'stored'}))
            out = m.record('Dunlin, the webhook sender, has a pool of 25.',
                           space_id=SPACE)
            assert 'Stored in space' in out, out
            print('OK')
    """))


def test_record_reports_nothing_extracted_when_empty():
    _ok(_run("""
        with respx.mock:
            respx.post(f'{BASE}/v1/record').mock(return_value=httpx.Response(
                201, json={'memory_id': None, 'memory_ids': None,
                           'status': 'stored'}))
            out = m.record('Falcon retry count is 3.', space_id=SPACE)
            assert 'Nothing was stored' in out, out
            assert 'sentence of context' in out
            print('OK')
    """))


def test_record_reports_stored_for_queued_write():
    _ok(_run("""
        with respx.mock:
            respx.post(f'{BASE}/v1/record').mock(return_value=httpx.Response(
                201, json={'job_id': 'j1', 'status': 'processing'}))
            out = m.record('x', space_id=SPACE)
            # A queued write has not extracted yet — must not be called empty.
            assert 'Stored in space' in out, out
            print('OK')
    """))


def test_record_sends_scope():
    _ok(_run("""
        with respx.mock:
            route = respx.post(f'{BASE}/v1/record').mock(
                return_value=httpx.Response(201, json={'memory_id': 'm1'}))
            m.record('hi', space_id=SPACE, user_id='alice',
                     agent_id='bot', session_id='sess1')
            body = json.loads(route.calls[-1].request.content)
            assert body['user_id'] == 'alice'
            assert body['agent_id'] == 'bot'
            assert body['session_id'] == 'sess1'
            print('OK')
    """))


def test_retrieve_sends_scope():
    _ok(_run("""
        with respx.mock:
            route = respx.post(f'{BASE}/v1/retrieve').mock(
                return_value=httpx.Response(200, json={'results': []}))
            m.retrieve('q', space_id=SPACE, user_id='alice', limit=3)
            body = json.loads(route.calls[-1].request.content)
            assert body['user_id'] == 'alice'
            assert body['limit'] == 3
            print('OK')
    """))


def test_reason_sends_model_and_scope():
    _ok(_run("""
        with respx.mock:
            route = respx.post(f'{BASE}/v1/reason').mock(
                return_value=httpx.Response(200, json={'insights': 'ok'}))
            out = m.reason('topic', space_id=SPACE, model='fast', user_id='alice')
            body = json.loads(route.calls[-1].request.content)
            assert body['model'] == 'fast'
            assert body['user_id'] == 'alice'
            assert out == 'ok'
            print('OK')
    """))


def test_tools_expose_scope_and_model_params():
    # Schema parity with the remote server: the params must be on the signatures
    # so FastMCP advertises them in each tool's inputSchema.
    _ok(_run("""
        import inspect
        for name in ('record', 'retrieve', 'reason'):
            params = set(inspect.signature(getattr(m, name)).parameters)
            for p in ('user_id', 'agent_id', 'session_id'):
                assert p in params, (name, p)
        assert 'model' in inspect.signature(m.reason).parameters
        print('OK')
    """))


def test_client_reused_across_calls():
    _ok(_run("""
        c1 = m._client()
        c2 = m._client()
        assert c1 is c2, 'a new client (new TLS pool) was built per call'
        print('OK')
    """))
