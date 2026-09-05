"""Retry policy, error shape and the rate-limit snapshot.

Runs each case in an isolated subprocess (only this package on sys.path) so
every case starts from a fresh import: the cases patch the module-level sleep
and swap the transport, and one process shared between them would let a patch
leak from one case into the next.

Sleeps are stubbed out, so a case that waits out a 60-second rate-limit window
still runs in milliseconds — and the recorded delays are themselves asserted on,
which is the only way to tell "honoured the server's hint" from "backed off on
its own schedule and got lucky".
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

_SDK_ROOT = Path(__file__).resolve().parent.parent


def _run(snippet: str) -> subprocess.CompletedProcess:
    header = (
        "import sys\n"
        f"sys.path.insert(0, {str(_SDK_ROOT)!r})\n"
        "import json, httpx\n"
        "import anona.client as ac\n"
        "from anona.client import AnonaClient, AnonaError, _ErrorTranslatingTransport\n"
        # Record what the client asked to wait rather than actually waiting.
        "slept = []\n"
        "ac.time.sleep = lambda s: slept.append(s)\n"
        "attempts = []\n"
        "def wire(client, handler, max_retries=2, max_wait=60.0):\n"
        "    client._client = httpx.Client(\n"
        "        transport=_ErrorTranslatingTransport(\n"
        "            httpx.MockTransport(handler),\n"
        "            ac._RetryPolicy(max_retries, max_wait),\n"
        "            client.rate_limit,\n"
        "        ),\n"
        "        headers={'Authorization': 'Bearer k'},\n"
        "    )\n"
        "c = AnonaClient(api_key='k', base_url='http://t.local')\n"
    )
    script = header + textwrap.dedent(snippet)
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )


def _ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout, result.stdout


# ── Retry ─────────────────────────────────────────────────────────────────────


def test_429_is_retried_and_honours_retry_after_header():
    _ok(_run("""
        def handler(request):
            attempts.append(request)
            if len(attempts) == 1:
                return httpx.Response(429, headers={'Retry-After': '17'},
                                      json={'error': {'code': 'rate_limited'}})
            return httpx.Response(200, json={'results': []})
        wire(c, handler)
        c.retrieve(space_id='s1', query='q')
        assert len(attempts) == 2, attempts
        assert slept == [17.0], slept
        print('OK')
    """))


def test_429_reads_the_wait_out_of_the_body_when_there_is_no_header():
    # The deployed API carried window_seconds in the payload before it grew the
    # header; an SDK that only understood the header would ignore it and back
    # off for a second and a half against a sixty-second window.
    _ok(_run("""
        def handler(request):
            attempts.append(request)
            if len(attempts) == 1:
                return httpx.Response(429, json={'error': {
                    'code': 'rate_limited', 'limit': 60, 'window_seconds': 60}})
            return httpx.Response(200, json={'results': []})
        wire(c, handler)
        c.retrieve(space_id='s1', query='q')
        assert len(attempts) == 2, attempts
        assert slept == [60.0], slept
        print('OK')
    """))


def test_retry_after_is_capped_by_retry_max_wait():
    _ok(_run("""
        def handler(request):
            attempts.append(request)
            if len(attempts) == 1:
                return httpx.Response(429, headers={'Retry-After': '600'}, json={})
            return httpx.Response(200, json={'results': []})
        wire(c, handler, max_wait=30.0)
        c.retrieve(space_id='s1', query='q')
        assert slept == [30.0], slept
        print('OK')
    """))


def test_429_on_a_write_is_retried_because_the_limiter_ran_before_the_route():
    _ok(_run("""
        def handler(request):
            attempts.append(request)
            if len(attempts) == 1:
                return httpx.Response(429, headers={'Retry-After': '1'}, json={})
            return httpx.Response(200, json={'memory_id': 'm1'})
        wire(c, handler)
        out = c.record(space_id='s1', content='hi')
        assert len(attempts) == 2, attempts
        assert out['memory_id'] == 'm1'
        print('OK')
    """))


def test_5xx_on_a_write_is_never_retried():
    # A 5xx means the write may well have executed; re-sending double-writes.
    _ok(_run("""
        def handler(request):
            attempts.append(request)
            return httpx.Response(503, json={'error': {'code': 'engine_unavailable'}})
        wire(c, handler)
        try:
            c.record(space_id='s1', content='hi')
        except AnonaError as e:
            assert e.status_code == 503
            assert len(attempts) == 1, attempts
            assert slept == [], slept
            print('OK')
    """))


def test_5xx_on_a_read_is_retried():
    _ok(_run("""
        def handler(request):
            attempts.append(request)
            if len(attempts) < 3:
                return httpx.Response(503, json={})
            return httpx.Response(200, json={'items': [], 'total': 0})
        wire(c, handler)
        c.list_memories(space_id='s1')
        assert len(attempts) == 3, attempts
        assert len(slept) == 2 and all(0 < s < 5 for s in slept), slept
        print('OK')
    """))


def test_uploads_are_not_retried_because_the_file_is_not_rewound():
    _ok(_run("""
        import io
        def handler(request):
            attempts.append(request)
            return httpx.Response(429, headers={'Retry-After': '1'}, json={})
        wire(c, handler)
        try:
            c.upload_file('s1', io.BytesIO(b'hello'), filename='a.txt')
        except AnonaError as e:
            assert e.status_code == 429
            assert len(attempts) == 1, attempts
            print('OK')
    """))


def test_max_retries_zero_turns_retrying_off():
    _ok(_run("""
        def handler(request):
            attempts.append(request)
            return httpx.Response(429, headers={'Retry-After': '1'}, json={})
        wire(c, handler, max_retries=0)
        try:
            c.retrieve(space_id='s1', query='q')
        except AnonaError:
            assert len(attempts) == 1, attempts
            print('OK')
    """))


def test_retries_are_bounded_and_the_last_response_is_raised():
    _ok(_run("""
        def handler(request):
            attempts.append(request)
            return httpx.Response(429, headers={'Retry-After': '2'},
                                  json={'error': {'code': 'rate_limited'}})
        wire(c, handler, max_retries=2)
        try:
            c.retrieve(space_id='s1', query='q')
        except AnonaError as e:
            assert e.status_code == 429
            assert e.code == 'rate_limited'
            assert len(attempts) == 3, attempts
            print('OK')
    """))


# ── Error shape ───────────────────────────────────────────────────────────────


def test_error_carries_code_request_id_and_retry_after():
    _ok(_run("""
        def handler(request):
            return httpx.Response(
                429,
                headers={'Retry-After': '9', 'X-Request-ID': 'req-123'},
                json={'error': {'code': 'rate_limited', 'message': 'slow down'}},
            )
        wire(c, handler, max_retries=0)
        try:
            c.retrieve(space_id='s1', query='q')
        except AnonaError as e:
            assert e.code == 'rate_limited', e.code
            assert e.request_id == 'req-123', e.request_id
            assert e.retry_after == 9, e.retry_after
            # The old surface is unchanged.
            assert e.status_code == 429
            assert e.detail['error']['message'] == 'slow down'
            print('OK')
    """))


def test_error_code_is_none_when_the_body_is_not_the_envelope():
    # Cloudflare strips the body of 502/504; the API rewrites those to 503, so a
    # bodiless failure is a real shape the SDK has to survive.
    _ok(_run("""
        def handler(request):
            return httpx.Response(503, headers={'X-Request-ID': 'req-9'},
                                  text='error code: 502')
        wire(c, handler, max_retries=0)
        try:
            c.list_spaces()
        except AnonaError as e:
            assert e.code is None and e.retry_after is None
            assert e.request_id == 'req-9'
            print('OK')
    """))


# ── Rate-limit snapshot ───────────────────────────────────────────────────────


def test_rate_limit_snapshot_is_populated_from_response_headers():
    _ok(_run("""
        def handler(request):
            return httpx.Response(200, json={'results': []}, headers={
                'X-RateLimit-Limit': '60',
                'X-RateLimit-Remaining': '7',
                'X-RateLimit-Window': '60',
                'X-Credits-Remaining': '4321',
            })
        wire(c, handler)
        c.retrieve(space_id='s1', query='q')
        assert c.rate_limit.limit == 60
        assert c.rate_limit.remaining == 7
        assert c.rate_limit.window_seconds == 60
        assert c.rate_limit.credits_remaining == 4321
        assert c.rate_limit.retry_after is None
        print('OK')
    """))


def test_rate_limit_snapshot_starts_empty_and_survives_an_unmetered_call():
    # The config routes report no budget; a call to one must not blank out what
    # the last metered call reported.
    _ok(_run("""
        assert c.rate_limit.limit is None
        calls = []
        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(200, json={'results': []},
                                      headers={'X-RateLimit-Limit': '60',
                                               'X-RateLimit-Remaining': '5',
                                               'X-RateLimit-Window': '60'})
            return httpx.Response(200, json={'space_id': 's1'})
        wire(c, handler)
        c.retrieve(space_id='s1', query='q')
        c.get_chat_settings('s1')
        assert c.rate_limit.limit == 60 and c.rate_limit.remaining == 5
        print('OK')
    """))
