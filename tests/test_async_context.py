"""Async context-block tests for this package's AnonaClient.

Runs in an isolated subprocess with only the repository root on sys.path, so
the import under test is unambiguously this package.
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
        "import json, asyncio, httpx\n"
        "from anona.client import AnonaClient\n"
        "seen = []\n"
        "def handler(request):\n"
        "    body = request.content.decode() or 'null'\n"
        "    seen.append((request.method, str(request.url), json.loads(body)))\n"
        "    return httpx.Response(200, json={'context': 'ctx-block', 'results': []})\n"
        "c = AnonaClient(api_key='k', base_url='http://t.local')\n"
        "c._async_client = httpx.AsyncClient(\n"
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


def test_async_get_context_returns_block():
    _ok(_run("""
        out = asyncio.run(c.async_get_context(space_id='s1', query='q'))
        assert out == 'ctx-block', out
        _, url, body = seen[-1]
        assert url.endswith('/v1/retrieve')
        assert body['format'] == 'block'
        print('OK')
    """))


def test_async_get_context_sends_max_tokens():
    _ok(_run("""
        asyncio.run(c.async_get_context(space_id='s1', query='q', max_tokens=500))
        _, _, body = seen[-1]
        assert body['context_max_tokens'] == 500
        print('OK')
    """))


def test_async_get_context_omits_max_tokens_when_none():
    _ok(_run("""
        asyncio.run(c.async_get_context(space_id='s1', query='q'))
        _, _, body = seen[-1]
        assert 'context_max_tokens' not in body
        print('OK')
    """))


def test_async_get_context_forwards_scope():
    _ok(_run("""
        asyncio.run(c.async_get_context(
            space_id='s1', query='q', user_id='u1', agent_id='a1', session_id='sess1'))
        _, _, body = seen[-1]
        assert body['user_id'] == 'u1'
        assert body['agent_id'] == 'a1'
        assert body['session_id'] == 'sess1'
        print('OK')
    """))


def test_async_get_context_empty_when_no_context_field():
    _ok(_run("""
        def empty(request):
            return httpx.Response(200, json={'results': []})
        c._async_client = httpx.AsyncClient(transport=httpx.MockTransport(empty))
        out = asyncio.run(c.async_get_context(space_id='s1', query='q'))
        assert out == '', repr(out)
        print('OK')
    """))


def test_version_matches_pyproject():
    import re
    root = _SDK_ROOT
    init_src = (root / "anona" / "__init__.py").read_text()
    pyproject = (root / "pyproject.toml").read_text()
    init_ver = re.search(r'__version__\s*=\s*"([^"]+)"', init_src).group(1)
    proj_ver = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M).group(1)
    assert init_ver == proj_ver, f"{init_ver} != {proj_ver}"
