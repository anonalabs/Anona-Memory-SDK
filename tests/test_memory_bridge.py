"""MemoryBridge tests — the shared core behind every framework adapter.

Runs in an isolated subprocess with only the repository root on sys.path, so
the import under test is unambiguously this package. No framework packages are
needed here — the bridge depends on none of them, which is why these tests
always run in CI.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

_SDK_ROOT = Path(__file__).resolve().parent.parent


def _run(snippet: str, status: int = 200) -> subprocess.CompletedProcess:
    header = (
        "import sys\n"
        f"sys.path.insert(0, {str(_SDK_ROOT)!r})\n"
        "import json, asyncio, httpx\n"
        "from anona.integrations._core import MemoryBridge\n"
        "seen = []\n"
        f"STATUS = {status}\n"
        "def handler(request):\n"
        "    body = request.content.decode() or 'null'\n"
        "    seen.append((request.method, str(request.url), json.loads(body)))\n"
        "    if STATUS != 200:\n"
        "        return httpx.Response(STATUS, json={'error': {'code': 'boom'}})\n"
        "    return httpx.Response(200, json={'context': 'ctx-block', 'memory_id': 'm1'})\n"
        "b = MemoryBridge(api_key='k', space_id='s1', base_url='http://t.local')\n"
        "b._client._async_client = httpx.AsyncClient(\n"
        "    transport=httpx.MockTransport(handler),\n"
        "    headers={'Authorization': 'Bearer k'})\n"
    )
    script = header + textwrap.dedent(snippet)
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )


def _ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def _run_real(snippet: str) -> subprocess.CompletedProcess:
    """Like ``_run``, but wires the bridge to a REAL localhost socket
    instead of ``httpx.MockTransport``.

    C1 and C2 (whole-branch review, ) are both event-loop-bound
    connection-pool state: ``AnonaClient``'s async ``httpx.AsyncClient`` is
    created once and its keep-alive connection ends up bound to whichever
    event loop was running on the first call that used it.
    ``httpx.MockTransport`` never opens a socket and has no such state, so
    every test above this line would pass identically whether or not either
    bug were fixed — which is exactly how both shipped in a branch with 118
    passing tests. Only a real socket, across real sequential ``asyncio.run``
    loops, can tell the difference.
    """
    header = (
        "import sys\n"
        f"sys.path.insert(0, {str(_SDK_ROOT)!r})\n"
        "import json, threading\n"
        "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
        "from anona.integrations._core import MemoryBridge, _sync\n"
        "seen = []\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        "    protocol_version = 'HTTP/1.1'\n"
        "    def log_message(self, *a): pass\n"
        "    def do_POST(self):\n"
        "        n = int(self.headers.get('Content-Length', 0))\n"
        "        body = self.rfile.read(n)\n"
        "        seen.append((self.path, json.loads(body or b'null')))\n"
        "        out = json.dumps({'context': 'ctx-block', 'memory_id': 'm1'}).encode()\n"
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
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )


def test_context_returns_block():
    _ok(_run("""
        out = asyncio.run(b.context('what do I like?'))
        assert out == 'ctx-block', out
        _, url, body = seen[-1]
        assert url.endswith('/v1/retrieve')
        assert body['format'] == 'block'
        assert body['query'] == 'what do I like?'
        print('OK')
    """))


def test_remember_posts_record():
    _ok(_run("""
        asyncio.run(b.remember('User: hi\\nAssistant: hello'))
        method, url, body = seen[-1]
        assert method == 'POST' and url.endswith('/v1/record')
        assert body['content'] == 'User: hi\\nAssistant: hello'
        print('OK')
    """))


def test_scope_forwarded_on_both_calls():
    _ok(_run("""
        b2 = MemoryBridge(api_key='k', space_id='s1', base_url='http://t.local',
                          user_id='u1', agent_id='a1', session_id='sess1')
        b2._client._async_client = b._client._async_client
        asyncio.run(b2.context('q'))
        _, _, ctx_body = seen[-1]
        asyncio.run(b2.remember('t'))
        _, _, rec_body = seen[-1]
        for body in (ctx_body, rec_body):
            assert body['user_id'] == 'u1', body
            assert body['agent_id'] == 'a1', body
            assert body['session_id'] == 'sess1', body
        print('OK')
    """))


def test_context_fails_open_on_error():
    _ok(_run("""
        out = asyncio.run(b.context('q'))
        assert out == '', repr(out)
        print('OK')
    """, status=500))


def test_remember_fails_open_on_error():
    _ok(_run("""
        asyncio.run(b.remember('t'))   # must not raise
        print('OK')
    """, status=500))


def test_context_fails_open_on_non_string_query():
    # Regression: the emptiness guard used to call .strip() *outside* the
    # try block, so a truthy non-string (an adapter forwarding a framework
    # message object instead of extracted text) hit AttributeError and
    # raised straight into the caller instead of failing open.
    _ok(_run("""
        out = asyncio.run(b.context(12345))
        assert out == '', repr(out)
        assert seen == [], seen   # never got far enough to make a call
        print('OK')
    """))


def test_context_fails_open_on_non_string_list_query():
    _ok(_run("""
        out = asyncio.run(b.context(["what", "do", "I", "like"]))
        assert out == '', repr(out)
        assert seen == [], seen
        print('OK')
    """))


def test_remember_fails_open_on_non_string_text():
    _ok(_run("""
        asyncio.run(b.remember({"role": "user", "content": "hi"}))   # must not raise
        assert seen == [], seen
        print('OK')
    """))


def test_context_empty_query_makes_no_call():
    _ok(_run("""
        out = asyncio.run(b.context('   '))
        assert out == ''
        assert seen == [], seen
        print('OK')
    """))


def test_remember_empty_text_makes_no_call():
    _ok(_run("""
        asyncio.run(b.remember(''))
        assert seen == [], seen
        print('OK')
    """))


def test_limit_and_max_tokens_forwarded():
    _ok(_run("""
        b2 = MemoryBridge(api_key='k', space_id='s1', base_url='http://t.local',
                          limit=3, max_tokens=500)
        b2._client._async_client = b._client._async_client
        asyncio.run(b2.context('q'))
        _, _, body = seen[-1]
        assert body['limit'] == 3
        assert body['context_max_tokens'] == 500
        print('OK')
    """))


def test_missing_api_key_raises():
    _ok(_run("""
        try:
            MemoryBridge(api_key='', space_id='s1')
        except ValueError as e:
            assert 'api_key' in str(e)
            print('OK')
    """))


def test_missing_space_id_raises():
    _ok(_run("""
        try:
            MemoryBridge(api_key='k', space_id='')
        except ValueError as e:
            assert 'space_id' in str(e)
            print('OK')
    """))


def test_memory_bridge_and_require_are_public_from_the_package():
    # Minor: every doc page and every adapter's own
    # usage example imports from the public anona.integrations path, not the
    # underscore-prefixed anona.integrations._core — the re-export in
    # anona/integrations/__init__.py is what makes that path work. The old
    # path must keep working too (nothing moves out from under it), and both
    # must resolve to the exact same objects, not copies.
    _ok(_run("""
        from anona.integrations import MemoryBridge as PublicBridge, require as public_require
        from anona.integrations._core import MemoryBridge as PrivateBridge, require as private_require
        assert PublicBridge is PrivateBridge
        assert public_require is private_require
        assert PublicBridge is MemoryBridge
        print('OK')
    """))


def test_require_raises_with_install_hint():
    _ok(_run("""
        from anona.integrations._core import require
        try:
            require('definitely_not_installed_xyz', 'langchain')
        except ImportError as e:
            assert "pip install 'anona[langchain]'" in str(e), str(e)
            print('OK')
    """))


def test_per_call_scope_overrides_construction_scope():
    _ok(_run("""
        b2 = MemoryBridge(api_key='k', space_id='s1', base_url='http://t.local',
                          user_id='built-in', agent_id='agent-x')
        b2._client._async_client = b._client._async_client
        asyncio.run(b2.context('q', user_id='per-call'))
        _, _, body = seen[-1]
        assert body['user_id'] == 'per-call', body
        assert body['agent_id'] == 'agent-x', body   # not overridden, still applies
        print('OK')
    """))


def test_per_call_scope_applies_to_remember():
    _ok(_run("""
        b2 = MemoryBridge(api_key='k', space_id='s1', base_url='http://t.local')
        b2._client._async_client = b._client._async_client
        asyncio.run(b2.remember('t', user_id='u9', session_id='sess9'))
        _, _, body = seen[-1]
        assert body['user_id'] == 'u9', body
        assert body['session_id'] == 'sess9', body
        print('OK')
    """))


def test_sync_runs_a_coroutine_without_a_loop():
    _ok(_run("""
        from anona.integrations._core import _sync
        async def work():
            return 42
        assert _sync(work()) == 42
        print('OK')
    """))


def test_sync_runs_a_coroutine_inside_a_running_loop():
    _ok(_run("""
        from anona.integrations._core import _sync
        async def work():
            return 42
        async def outer():
            return _sync(work())     # must not raise "loop already running"
        assert asyncio.run(outer()) == 42
        print('OK')
    """))


def test_close_closes_the_async_client():
    # Regression: MemoryBridge only ever drives AnonaClient's async methods,
    # so the connection pool actually left open is the async client — but
    # close() used to call only AnonaClient.close() (the *sync* teardown),
    # which is a no-op here and silently leaked it.
    _ok(_run("""
        asyncio.run(b.context('q'))   # exercise the async client first
        async_client = b._client._async_client
        assert async_client.is_closed is False
        b.close()
        assert async_client.is_closed is True
        print('OK')
    """))


# ---------------------------------------------------------------------------
# Real-socket regressions. Every test above
# this line uses httpx.MockTransport, which has no event loop and no
# connection pool — see _run_real's docstring for why that makes it
# structurally blind to both of these.
# ---------------------------------------------------------------------------


def test_context_sync_survives_sequential_calls_over_a_real_socket():
    # C1. context_sync() drives AnonaClient's plain sync httpx.Client, which
    # never touches an event loop, so — unlike the _sync(bridge.context(...))
    # pattern CrewAI/Strands used to use (see test_adapter_crewai.py /
    # test_adapter_strands.py for the same proof at the adapter layer) —
    # repeated sequential calls have no stale loop-bound connection to trip
    # over. 8 calls, matching the reviewer's probe count.
    _ok(_run_real("""
        results = [b.context_sync(f'q{i}') for i in range(8)]
        assert results == ['ctx-block'] * 8, results
        assert len(seen) == 8, seen
        print('OK')
    """))


def test_remember_sync_survives_sequential_calls_over_a_real_socket():
    _ok(_run_real("""
        for i in range(8):
            b.remember_sync(f't{i}')
        assert len(seen) == 8, seen
        assert [c[1]['content'] for c in seen] == [f't{i}' for i in range(8)], seen
        print('OK')
    """))


def test_close_after_async_use_on_a_since_closed_loop_does_not_raise():
    # C2, the review's primary scenario. `await bridge.context(...)` inside
    # asyncio.run(...) opens the async client bound to that loop;
    # asyncio.run() closes the loop when it returns; close() called
    # afterward used to tear the async client down on a brand-new loop and
    # raise "Event loop is closed" straight out of a documented cleanup
    # method — the exact path test_close_closes_the_async_client above
    # exercises, but that test's MockTransport client has nothing loop-bound
    # to fail on.
    _ok(_run_real("""
        import asyncio
        asyncio.run(b.context('q'))
        b.close()   # must not raise
        print('OK')
    """))


def test_close_after_sync_use_does_not_raise():
    # The new common case for CrewAI/Strands: context_sync() never opens the
    # async client at all, so close() only ever has the plain sync client to
    # tear down.
    _ok(_run_real("""
        b.context_sync('q')
        b.close()   # must not raise
        print('OK')
    """))


def test_close_twice_does_not_raise():
    _ok(_run_real("""
        import asyncio
        asyncio.run(b.context('q'))
        b.close()
        b.close()   # must not raise the second time either
        print('OK')
    """))


def test_close_with_nothing_opened_does_not_raise():
    _ok(_run_real("""
        b.close()   # neither client was ever created
        print('OK')
    """))


# ---------------------------------------------------------------------------
# Real-socket regression: AnonaClient._get_async_client()
# pooled ONE httpx.AsyncClient for the object's whole life, so its keep-alive
# connection bound to whichever event loop was running on the first call —
# every later call from a *different* loop inherited a connection it could
# not use. context_sync()/remember_sync() (C1/C2 above) never touch the
# async client at all, so they could not reproduce this by themselves; these
# tests exercise the async pair, one loop per call, the actual failing shape.
# ---------------------------------------------------------------------------


def test_context_survives_one_loop_per_call_over_a_real_socket():
    # The async sibling of test_context_sync_survives_sequential_calls_over_a_
    # real_socket above. Confirmed against the pre-fix code to land at
    # exactly 4/8 (every other call succeeds, an OK/lost/OK/lost pattern) —
    # matching the reviewer's own measurement of this exact shape ("bare
    # asyncio.run(bridge.context(...)) x8 on one bridge -> 4/8").
    _ok(_run_real("""
        import asyncio
        results = [asyncio.run(b.context(f'q{i}')) for i in range(8)]
        assert results == ['ctx-block'] * 8, results
        assert len(seen) == 8, seen
        print('OK')
    """))


def test_remember_survives_one_loop_per_call_over_a_real_socket():
    # remember() fails open on the same stale-connection exception as
    # context() does, so its observable signal is a spurious "failed to
    # store turn" warning on a write that the server actually received (and
    # billed) — same shape as the sync-side write regression above, applied
    # to the async pair.
    _ok(_run_real("""
        import asyncio, logging
        warnings = []
        class CountHandler(logging.Handler):
            def emit(self, record):
                warnings.append(record.getMessage())
        log = logging.getLogger('anona.integrations')
        log.addHandler(CountHandler())
        log.propagate = False

        for i in range(8):
            asyncio.run(b.remember(f't{i}'))

        assert warnings == [], warnings
        record_calls = [s for s in seen if s[0] == '/v1/record']
        assert len(record_calls) == 8, record_calls
        print('OK')
    """))


def test_mixed_async_and_sync_use_on_one_bridge_over_a_real_socket():
    # The shape a process running an async adapter (LangChain, LlamaIndex,
    # Google ADK, MS Agent Framework) side by side with a sync one (CrewAI,
    # Strands) on the SAME MemoryBridge would produce: context()/remember(),
    # each its own fresh asyncio.run() loop, interleaved with context_sync()/
    # remember_sync() (a plain httpx.Client, no loop involved at all).
    # AnonaClient's sync half (_client) and per-loop async half
    # (_async_clients) are independent attributes, so this mainly guards
    # against a regression that accidentally couples them — but it is also
    # the most direct evidence that context_sync()'s fix (routing around the
    # event loop entirely) and context()'s fix (keying the async client per
    # loop) don't need to agree on *how* they avoid the bug to both avoid it.
    _ok(_run_real("""
        import asyncio
        results = []
        for i in range(8):
            if i % 2 == 0:
                results.append(('async', asyncio.run(b.context(f'a{i}'))))
            else:
                results.append(('sync', b.context_sync(f's{i}')))
        assert [r for _, r in results] == ['ctx-block'] * 8, results
        assert len(seen) == 8, seen
        print('OK')
    """))
