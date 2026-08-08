"""Tests for this package's AnonaClient lifecycle (lazy clients, close/aclose).

Runs each check in a fresh subprocess with only the repository root on
sys.path, so every test imports this package and nothing else that happens to
be installed under the same top-level name.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

_SDK_ROOT = Path(__file__).resolve().parent.parent


def _run(snippet: str) -> subprocess.CompletedProcess:
    header = f"import sys\nsys.path.insert(0, {str(_SDK_ROOT)!r})\n"
    script = header + textwrap.dedent(snippet)
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_clients_are_not_created_until_first_use():
    result = _run("""
        from anona.client import AnonaClient
        c = AnonaClient(api_key="test")
        assert c._client is None
        assert c._async_client is None
        print("OK")
    """)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_sync_call_only_creates_sync_client():
    result = _run("""
        from anona.client import AnonaClient
        c = AnonaClient(api_key="test", base_url="http://127.0.0.1:1")
        try:
            c.list_spaces()
        except Exception:
            pass  # connection refused is expected — just checking lazy init
        assert c._client is not None
        assert c._async_client is None
        c.close()
        print("OK")
    """)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_close_is_safe_when_nothing_was_opened():
    """Regression: close()/aclose() used to unconditionally touch the other
    protocol's client, which was always created eagerly in __init__. Now that
    creation is lazy, closing before any call must not raise."""
    result = _run("""
        import asyncio
        from anona.client import AnonaClient

        c = AnonaClient(api_key="test")
        c.close()  # must not raise even though nothing was ever opened

        c2 = AnonaClient(api_key="test")
        asyncio.run(c2.aclose())  # same, for the async side
        print("OK")
    """)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_context_manager_only_closes_what_it_opened():
    result = _run("""
        from anona.client import AnonaClient
        with AnonaClient(api_key="test", base_url="http://127.0.0.1:1") as c:
            try:
                c.list_spaces()
            except Exception:
                pass
        # __exit__ already ran — the sync client it opened should be closed,
        # and the never-opened async client should still be untouched (None).
        assert c._async_client is None
        print("OK")
    """)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_upload_file_rejects_oversized_file():
    # The per-file cap is enforced client-side before any network call, so an
    # oversized file fails fast with a 413 AnonaError instead of a wasted upload.
    result = _run("""
        from anona.client import AnonaClient, AnonaError
        c = AnonaClient(api_key="test")
        big = b"x" * (c.MAX_FILE_BYTES + 1)
        try:
            c.upload_file("spc", big, filename="huge.bin")
            print("NO_ERROR")
        except AnonaError as e:
            print("RAISED", e.status_code)
    """)
    assert result.returncode == 0, result.stderr
    assert "RAISED 413" in result.stdout


# ---------------------------------------------------------------------------
# Real-socket regression. AnonaClient._get_async_client()
# used to pool ONE httpx.AsyncClient for the object's whole life. Its
# keep-alive connection — and the anyio locks httpcore's pool guards it
# with — binds to whichever event loop was running on the *first* call that
# used it, so a host that creates a fresh loop per call (asyncio.run() once
# per turn is the ordinary shape for a CLI, a synchronous Flask/Django view,
# or a Celery/RQ worker driving one of the async framework adapters) hands
# the *second* call's new loop a connection pool built for a now-dead one —
# deterministically: RuntimeError: Event loop is closed, or "bound to a
# different event loop" if the first loop is somehow still alive elsewhere.
# httpx.MockTransport never opens a socket and has no such state — every
# test above this line would pass identically whether or not this bug were
# fixed, which is how it shipped. Only a real socket, across real event
# loops, can tell the difference — same pattern as the C1/C2 real-socket
# sections in test_memory_bridge.py / test_adapter_crewai.py /
# test_adapter_strands.py.
# ---------------------------------------------------------------------------


def _run_real(snippet: str) -> subprocess.CompletedProcess:
    """Like ``_run``, but a REAL localhost socket instead of subprocess-local
    mocking — no transport is mocked at all here, since the point is to
    exercise AnonaClient's real connection-pooling machinery end to end."""
    header = (
        "import sys\n"
        f"sys.path.insert(0, {str(_SDK_ROOT)!r})\n"
        "import json, threading\n"
        "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
        "from anona.client import AnonaClient\n"
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
        "        out = json.dumps({'context': 'ctx-block', 'results': []}).encode()\n"
        "        self.send_response(200)\n"
        "        self.send_header('Content-Type', 'application/json')\n"
        "        self.send_header('Content-Length', str(len(out)))\n"
        "        self.end_headers()\n"
        "        self.wfile.write(out)\n"
        "srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)\n"
        "port = srv.server_address[1]\n"
        "threading.Thread(target=srv.serve_forever, daemon=True).start()\n"
        "BASE = f'http://127.0.0.1:{port}'\n"
        "c = AnonaClient(api_key='k', base_url=BASE)\n"
    )
    script = header + textwrap.dedent(snippet)
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )


def test_repeated_asyncio_run_survives_on_one_client():
    # The core repro, at the AnonaClient level (MemoryBridge's context_sync/
    # remember_sync already have their own version of this in
    # test_memory_bridge.py — this one is the async side, no bridge
    # involved). asyncio.run() once per call, sequentially, on ONE
    # AnonaClient: confirmed against the pre-fix code to land at exactly
    # 4/8 (every other call), matching the reviewer's own measurement of
    # "bare asyncio.run(bridge.context(...)) x8 on one bridge -> 4/8" one
    # layer down, with the fail-open try/except removed so a stale-
    # connection failure surfaces as a real exception instead of a silent
    # empty string.
    _ok(_run_real("""
        import asyncio
        results = []
        for i in range(8):
            out = asyncio.run(c.async_get_context(space_id='s1', query=f'q{i}'))
            results.append(out)
        assert results == ['ctx-block'] * 8, results
        assert len(seen) == 8, seen
        print('OK')
    """))


def test_repeated_asyncio_run_survives_gaps_past_keepalive_expiry():
    # httpx's default keepalive_expiry is 5s. The bug is deterministic (a
    # dead connection stays dead, whether or not it would also have expired
    # out of the pool on its own), not a race against that timer — so every
    # gap here, including one comfortably past it, must be clean.
    _ok(_run_real("""
        import asyncio, time
        for gap in (0, 1, 6):
            results = []
            for i in range(4):
                out = asyncio.run(c.async_get_context(space_id='s1', query=f'g{gap}-{i}'))
                results.append(out)
                time.sleep(gap)
            assert results == ['ctx-block'] * 4, (gap, results)
        print('OK')
    """))


def test_concurrent_threads_each_with_their_own_loop():
    # Several threads through one AnonaClient at once, each running its own
    # asyncio.run() loop — the shape a thread-pool-driven caller produces
    # (see anona/integrations/_core.py's _sync(), used from a thread pool
    # when close() is called from inside a running loop). A different OS
    # thread means a different loop, hence a different dict key in the fix —
    # this must not corrupt or starve another thread's calls on the same
    # client. Barrier-synchronizes thread start to maximize overlap.
    _ok(_run_real("""
        import asyncio, threading
        results = {}
        errors = {}
        barrier = threading.Barrier(8)
        def worker(i):
            barrier.wait()
            try:
                out = []
                for j in range(5):
                    out.append(asyncio.run(
                        c.async_get_context(space_id='s1', query=f't{i}-{j}')))
                results[i] = out
            except Exception as e:
                errors[i] = f'{type(e).__name__}: {e}'
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == {}, errors
        assert all(v == ['ctx-block'] * 5 for v in results.values()), results
        print('OK')
    """))


def test_aclose_after_many_sequential_loops_does_not_raise_and_stays_bounded():
    # Companion to the leak concern the fix's own docstring calls out: a
    # caller that creates one event loop per call (this test's whole shape)
    # must not accumulate one never-closed AsyncClient per call forever.
    # Dead loops are swept on every _get_async_client() call, so the
    # client's internal per-loop map should never grow past a small,
    # bounded size — checked directly here since it is otherwise
    # unobservable from outside the class.
    _ok(_run_real("""
        import asyncio
        sizes = []
        for i in range(30):
            asyncio.run(c.async_get_context(space_id='s1', query=f'q{i}'))
            sizes.append(len(c._async_clients))
        assert max(sizes) <= 1, sizes   # never more than the one live loop
        asyncio.run(c.aclose())   # must not raise
        print('OK')
    """))
