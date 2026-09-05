"""LiteLLM-integration tests for AnonaMemory.

Covers the defect where the integration implemented the LiteLLM *proxy* hook
names (async_pre_call_hook / async_post_call_success_hook), which plain
litellm.completion() never fires — so enable() injected and stored nothing — and
it replaced litellm.callbacks, wiping the caller's other callbacks.

Runs in an isolated subprocess with a *fake* `litellm` module, so it needs no
real litellm install and every case starts from a fresh import. The API is
mocked with respx.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

_SDK_ROOT = Path(__file__).resolve().parent.parent


def _run(snippet: str) -> subprocess.CompletedProcess:
    header = (
        "import sys, types, json, asyncio\n"
        f"sys.path.insert(0, {str(_SDK_ROOT)!r})\n"
        "import httpx, respx\n"
        "from types import SimpleNamespace\n"
        # ── fake litellm module tree ──────────────────────────────────────────
        "litellm = types.ModuleType('litellm')\n"
        "litellm.callbacks = []\n"
        "integ = types.ModuleType('litellm.integrations')\n"
        "custom = types.ModuleType('litellm.integrations.custom_logger')\n"
        "class CustomLogger:\n"
        "    pass\n"
        "custom.CustomLogger = CustomLogger\n"
        "integ.custom_logger = custom\n"
        "litellm.integrations = integ\n"
        "sys.modules['litellm'] = litellm\n"
        "sys.modules['litellm.integrations'] = integ\n"
        "sys.modules['litellm.integrations.custom_logger'] = custom\n"
        "from anona.integrations.litellm import AnonaMemory\n"
        "BASE = 'http://t.local'\n"
        "def _resp_obj(text):\n"
        "    return SimpleNamespace(choices=[SimpleNamespace("
        "message=SimpleNamespace(content=text))])\n"
    )
    script = header + textwrap.dedent(snippet)
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )


def _ok(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_enable_appends_and_preserves_other_callbacks():
    _ok(_run("""
        class Other:  # a caller's own pre-existing callback
            pass
        other = Other()
        litellm.callbacks = [other]
        mem = AnonaMemory(api_key='k', space_id='s1', base_url=BASE)
        mem.enable()
        assert other in litellm.callbacks, 'wiped the caller\\'s callback'
        assert len(litellm.callbacks) == 2
        print('OK')
    """))


def test_enable_no_duplicate_registration():
    _ok(_run("""
        litellm.callbacks = []
        mem = AnonaMemory(api_key='k', space_id='s1', base_url=BASE)
        mem.enable()
        mem.enable()
        cbs = [c for c in litellm.callbacks if type(c).__name__ == '_Callback']
        assert len(cbs) == 1, len(cbs)
        print('OK')
    """))


def test_registers_sdk_mode_hook_names_not_proxy_names():
    _ok(_run("""
        litellm.callbacks = []
        AnonaMemory(api_key='k', space_id='s1', base_url=BASE).enable()
        cb = [c for c in litellm.callbacks if type(c).__name__ == '_Callback'][0]
        # The hooks litellm.completion() actually fires:
        assert hasattr(cb, 'log_pre_api_call')
        assert hasattr(cb, 'log_success_event')
        assert hasattr(cb, 'async_log_pre_api_call')
        assert hasattr(cb, 'async_log_success_event')
        # The proxy-only names it used to (wrongly) implement:
        assert not hasattr(cb, 'async_pre_call_hook')
        assert not hasattr(cb, 'async_post_call_success_hook')
        print('OK')
    """))


def test_sync_hooks_inject_then_store():
    _ok(_run("""
        with respx.mock:
            retrieve_route = respx.post(f'{BASE}/v1/retrieve').mock(
                return_value=httpx.Response(200, json={
                    'results': [{'content': 'You like Python'}]}))
            record_route = respx.post(f'{BASE}/v1/record').mock(
                return_value=httpx.Response(201, json={'memory_id': 'm1'}))
            litellm.callbacks = []
            AnonaMemory(api_key='k', space_id='s1', base_url=BASE).enable()
            cb = [c for c in litellm.callbacks
                  if type(c).__name__ == '_Callback'][0]

            messages = [{'role': 'user', 'content': 'what language do I like?'}]
            cb.log_pre_api_call('gpt-x', messages, {})
            assert retrieve_route.called, 'pre-call did not search'
            assert messages[0]['role'] == 'system'
            assert 'Python' in messages[0]['content']

            cb.log_success_event({'messages': messages},
                                 _resp_obj('Because it is expressive.'),
                                 None, None)
            assert record_route.called, 'success hook did not store'
            body = json.loads(record_route.calls[-1].request.content)
            assert 'User: what language do I like?' in body['content']
            assert 'Assistant: Because it is expressive.' in body['content']
            print('OK')
    """))


def test_async_hooks_inject_then_store():
    _ok(_run("""
        async def go():
            with respx.mock:
                retrieve_route = respx.post(f'{BASE}/v1/retrieve').mock(
                    return_value=httpx.Response(200, json={
                        'results': [{'content': 'Prefers dark mode'}]}))
                record_route = respx.post(f'{BASE}/v1/record').mock(
                    return_value=httpx.Response(201, json={'memory_id': 'm2'}))
                litellm.callbacks = []
                AnonaMemory(api_key='k', space_id='s1', base_url=BASE).enable()
                cb = [c for c in litellm.callbacks
                      if type(c).__name__ == '_Callback'][0]

                messages = [{'role': 'user', 'content': 'ui preference?'}]
                await cb.async_log_pre_api_call('gpt-x', messages, {})
                assert retrieve_route.called
                assert messages[0]['role'] == 'system'
                assert 'dark mode' in messages[0]['content']

                await cb.async_log_success_event(
                    {'messages': messages}, _resp_obj('Noted.'), None, None)
                # Awaited store, not fire-and-forget: it has completed by now.
                assert record_route.called
                body = json.loads(record_route.calls[-1].request.content)
                assert 'Assistant: Noted.' in body['content']
        asyncio.run(go())
        print('OK')
    """))


def test_store_after_false_skips_storage():
    _ok(_run("""
        with respx.mock:
            record_route = respx.post(f'{BASE}/v1/record').mock(
                return_value=httpx.Response(201, json={'memory_id': 'm1'}))
            litellm.callbacks = []
            AnonaMemory(api_key='k', space_id='s1', base_url=BASE,
                        store_after=False).enable()
            cb = [c for c in litellm.callbacks
                  if type(c).__name__ == '_Callback'][0]
            cb.log_success_event({'messages': [{'role': 'user', 'content': 'hi'}]},
                                 _resp_obj('hello'), None, None)
            assert not record_route.called
            print('OK')
    """))
