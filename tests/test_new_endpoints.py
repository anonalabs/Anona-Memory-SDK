"""Request shapes for the endpoints the client did not reach before.

Isolated-subprocess style, so every case starts from a fresh import and a
fresh client.

The point of the parity test at the bottom is that this client carries a sync
and an async copy of every method, and the copies have drifted before — an
argument added to `retrieve` that never reached `async_retrieve`, and so on.
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
        "from anona.client import AnonaClient\n"
        "seen = []\n"
        "def handler(request):\n"
        "    body = request.content.decode() or 'null'\n"
        "    seen.append((request.method, str(request.url), json.loads(body)))\n"
        "    if request.method == 'DELETE':\n"
        "        return httpx.Response(204)\n"
        "    return httpx.Response(200, json={'ok': True})\n"
        "c = AnonaClient(api_key='k', base_url='http://t.local')\n"
        "c._client = httpx.Client(transport=httpx.MockTransport(handler),\n"
        "                         headers={'Authorization': 'Bearer k'})\n"
    )
    script = header + textwrap.dedent(snippet)
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )


def _ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout, result.stdout


# ── Memories ──────────────────────────────────────────────────────────────────


def test_list_memories_paths_and_params():
    _ok(_run("""
        c.list_memories('s1', limit=5, offset=10, prefer_observations=False,
                        member_id='me', state='invalidated', memory_type='world')
        method, url, _ = seen[-1]
        assert method == 'GET'
        assert '/v1/spaces/s1/memories' in url
        assert 'limit=5' in url and 'offset=10' in url
        # False has to survive: it is the whole reason to pass the flag.
        assert 'prefer_observations=false' in url.lower(), url
        assert 'member_id=me' in url
        assert 'state=invalidated' in url
        # The API's query name is `type`, not `memory_type`.
        assert 'type=world' in url, url
        print('OK')
    """))


def test_get_memory_history_path():
    _ok(_run("""
        c.get_memory_history('s1', 'mem-1')
        method, url, _ = seen[-1]
        assert method == 'GET' and url.endswith('/v1/spaces/s1/memories/mem-1/history')
        print('OK')
    """))


def test_update_memory_sends_only_what_was_passed():
    _ok(_run("""
        c.update_memory('s1', 'mem-1', text='corrected', state='invalidated',
                        reason='typo')
        method, url, body = seen[-1]
        assert method == 'PATCH' and url.endswith('/v1/spaces/s1/memories/mem-1')
        assert body == {'text': 'corrected', 'state': 'invalidated', 'reason': 'typo'}
        print('OK')
    """))


def test_cancel_job_path():
    _ok(_run("""
        c.cancel_job('s1', 'job-1')
        method, url, _ = seen[-1]
        assert method == 'DELETE' and url.endswith('/v1/spaces/s1/jobs/job-1')
        print('OK')
    """))


def test_get_usage_path():
    _ok(_run("""
        c.get_usage()
        method, url, _ = seen[-1]
        assert method == 'GET' and url.endswith('/v1/usage/me')
        print('OK')
    """))


# ── Spaces / documents ────────────────────────────────────────────────────────


def test_get_space_and_get_document_paths():
    _ok(_run("""
        c.get_space('s1')
        assert seen[-1][0] == 'GET' and seen[-1][1].endswith('/v1/spaces/s1')
        c.get_document('s1', 'doc-1')
        assert seen[-1][1].endswith('/v1/spaces/s1/documents/doc-1')
        print('OK')
    """))


def test_qualified_shared_space_id_is_encoded_into_one_segment():
    # A share is addressed `owner:name`, and the colon must stay inside the
    # segment or the path stops matching the route.
    _ok(_run("""
        c.get_space('acme:notes')
        _, url, _ = seen[-1]
        assert url.endswith('/v1/spaces/acme%3Anotes'), url
        print('OK')
    """))


# ── Reason settings ───────────────────────────────────────────────────────────


def test_reason_settings_get_set_reset():
    _ok(_run("""
        c.get_reason_settings('s1')
        assert seen[-1][0] == 'GET' and seen[-1][1].endswith('/v1/spaces/s1/reason-settings')
        c.set_reason_settings('s1', model='balanced')
        assert seen[-1][0] == 'PUT' and seen[-1][2] == {'model': 'balanced'}
        # Clearing the override is a null, not an omission — PUT is a full replace.
        c.set_reason_settings('s1')
        assert seen[-1][2] == {'model': None}
        c.reset_reason_settings('s1')
        assert seen[-1][0] == 'DELETE'
        print('OK')
    """))


# ── Memory models / profile / catalog ─────────────────────────────────────────


def test_memory_model_lifecycle_paths():
    _ok(_run("""
        c.list_memory_models('s1', limit=5)
        assert seen[-1][0] == 'GET' and '/v1/spaces/s1/models' in seen[-1][1]
        c.create_memory_model('s1', name='n', query='q')
        assert seen[-1][0] == 'POST' and seen[-1][2] == {'name': 'n', 'query': 'q'}
        c.get_memory_model('s1', 'm1')
        assert seen[-1][1].endswith('/v1/spaces/s1/models/m1')
        c.update_memory_model('s1', 'm1', name='n2')
        assert seen[-1][0] == 'PATCH' and seen[-1][2] == {'name': 'n2'}
        c.refresh_memory_model('s1', 'm1')
        assert seen[-1][0] == 'POST' and seen[-1][1].endswith('/models/m1/refresh')
        c.clear_memory_model('s1', 'm1')
        assert seen[-1][1].endswith('/models/m1/clear')
        c.get_memory_model_history('s1', 'm1')
        assert seen[-1][1].endswith('/models/m1/history')
        c.delete_memory_model('s1', 'm1')
        assert seen[-1][0] == 'DELETE'
        print('OK')
    """))


def test_space_profile_and_model_catalog_paths():
    _ok(_run("""
        c.get_space_profile('s1')
        assert seen[-1][1].endswith('/v1/spaces/s1/profile')
        c.list_catalog_models()
        assert seen[-1][1].endswith('/v1/models')
        print('OK')
    """))


# ── Search arguments that had no way through before ───────────────────────────


def test_retrieve_sends_the_full_filter_set():
    _ok(_run("""
        c.retrieve(space_id='s1', query='q', top_k=50, memory_type=['world'],
                   tags=['a'], tags_match='all_strict', prefer_observations=False,
                   min_score=0.3, member_id='me')
        _, url, body = seen[-1]
        assert url.endswith('/v1/retrieve')
        assert body['top_k'] == 50
        assert body['memory_type'] == ['world']
        assert body['tags'] == ['a'] and body['tags_match'] == 'all_strict'
        assert body['prefer_observations'] is False
        assert body['min_score'] == 0.3
        assert body['member_id'] == 'me'
        print('OK')
    """))


def test_unfiltered_retrieve_stays_byte_identical():
    # The no-performance-regression guarantee: an unscoped, unfiltered search
    # must build the payload it always did.
    _ok(_run("""
        c.retrieve(space_id='s1', query='q')
        _, _, body = seen[-1]
        assert body == {'space_id': 's1', 'query': 'q', 'limit': 10,
                        'mode': 'accurate'}, body
        print('OK')
    """))


def test_record_sends_context():
    _ok(_run("""
        c.record(space_id='s1', content='hi', context='from a support ticket')
        _, _, body = seen[-1]
        assert body['context'] == 'from a support ticket'
        print('OK')
    """))


def test_get_context_sends_block_order_and_budget():
    _ok(_run("""
        c.get_context('s1', 'q', max_tokens=500, block_order='stable', tags=['a'])
        _, url, body = seen[-1]
        assert url.endswith('/v1/retrieve')
        assert body['format'] == 'block'
        assert body['context_max_tokens'] == 500
        assert body['block_order'] == 'stable'
        assert body['tags'] == ['a']
        print('OK')
    """))


def test_retrieve_receipt_carries_the_filters_too():
    _ok(_run("""
        c.retrieve_receipt(space_id='s1', query='q', tags=['a'], min_score=0.5)
        _, _, body = seen[-1]
        assert body['receipt'] is True
        assert body['tags'] == ['a'] and body['min_score'] == 0.5
        print('OK')
    """))


# ── Sync/async parity ─────────────────────────────────────────────────────────


def test_every_public_method_has_both_a_sync_and_an_async_form():
    _ok(_run("""
        import inspect
        names = [n for n, _ in inspect.getmembers(AnonaClient, inspect.isfunction)
                 if not n.startswith('_')]
        sync = {n for n in names if not n.startswith('async_')} - {'close', 'aclose'}
        asyn = {n[len('async_'):] for n in names if n.startswith('async_')}
        assert sync == asyn, sorted(sync ^ asyn)
        assert len(sync) > 45, len(sync)
        print('OK')
    """))


def test_async_forms_send_the_same_request_as_the_sync_ones():
    _ok(_run("""
        import asyncio, httpx
        seen_a = []
        def handler_a(request):
            body = request.content.decode() or 'null'
            seen_a.append((request.method, str(request.url), json.loads(body)))
            return httpx.Response(200, json={'ok': True})
        c._async_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler_a),
            headers={'Authorization': 'Bearer k'})
        async def main():
            await c.async_list_memories('s1', prefer_observations=False)
            await c.async_update_memory('s1', 'm1', text='t')
            await c.async_get_usage()
            await c.async_list_catalog_models()
            await c.async_retrieve(space_id='s1', query='q', tags=['a'])
            await c.aclose()
        asyncio.run(main())
        assert 'prefer_observations=false' in seen_a[0][1].lower(), seen_a[0][1]
        assert seen_a[1][0] == 'PATCH' and seen_a[1][2] == {'text': 't'}
        assert seen_a[2][1].endswith('/v1/usage/me')
        assert seen_a[3][1].endswith('/v1/models')
        assert seen_a[4][2]['tags'] == ['a']
        print('OK')
    """))
