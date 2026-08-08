"""Tests for examples/ — the scripts customers copy.

An example that does not run is worse than no example, and the failure mode is
silent: a wrong keyword argument in somebody else's constructor looks fine in
review and only surfaces when a customer pastes it. Two real bugs were caught
this way while writing them (`OpenAIChatClient(model_id=...)` and
`Agent(chat_client=...)` — neither argument exists in Agent Framework).

So each example is actually executed here, against the real installed
framework, in three layers:

1. importing it builds the agent and must not raise (catches constructor and
   import errors) — and must issue **zero** HTTP calls, which is what proves
   the ``if __name__ == "__main__"`` guard is load-bearing rather than
   decorative;
2. for the two tool-based adapters, the example's own objects are then driven
   directly against a fake Anona server, with no model in the loop, and the
   requests that arrive are asserted on. That is the layer that proves the
   wiring in the example is right, not merely syntactically alive.

Runs in an isolated subprocess with only the repository root on sys.path, so
the import under test is unambiguously this package.

Skips per example when its framework — or the model provider its own install
line names — is absent, the same way the adapter suites do.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_SDK_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLES = _SDK_ROOT / "examples"


def _installed(*modules: str) -> bool:
    for module in modules:
        try:
            if importlib.util.find_spec(module) is None:
                return False
        except (ImportError, ValueError):
            return False
    return True


# (filename, modules the script imports at module scope). The provider packages
# are listed because the script constructs a model at import time — without
# them the example cannot be exercised at all, which is a skip, not a failure.
EXAMPLES = [
    ("langchain_agent.py", ("langchain", "langchain_openai")),
    ("crewai_crew.py", ("crewai",)),
    ("llamaindex_agent.py", ("llama_index.core", "llama_index.llms.openai")),
    ("google_adk_agent.py", ("google.adk",)),
    ("ms_agent_framework.py", ("agent_framework", "agent_framework.openai")),
    ("strands_agent.py", ("strands",)),
]

_ENV = {
    "ANONA_API_KEY": "anona_test_example",
    "OPENAI_API_KEY": "sk-test-not-used",
    "GOOGLE_API_KEY": "test-not-used",
    "AWS_ACCESS_KEY_ID": "test-not-used",
    "AWS_SECRET_ACCESS_KEY": "test-not-used",
    "AWS_DEFAULT_REGION": "us-east-1",
}


# A fake Anona server. Real socket, not a mock transport: the examples build
# real clients, and the point is to see what they actually send.
_SERVER = """
import json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SEEN = []

class H(BaseHTTPRequestHandler):
    def _handle(self):
        n = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(n).decode() if n else ""
        SEEN.append({
            "method": self.command,
            "path": self.path,
            "body": json.loads(raw) if raw else None,
            "auth": self.headers.get("Authorization"),
        })
        body = json.dumps({
            "context": "[memory] Alice is allergic to shellfish",
            "memories": [{"memory_id": "m1", "content": "allergic to shellfish"}],
            "memory_id": "m1",
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_POST = _handle
    do_GET = _handle
    def log_message(self, *a):
        pass

_srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
BASE = "http://127.0.0.1:%d" % _srv.server_address[1]
threading.Thread(target=_srv.serve_forever, daemon=True).start()
"""


def _run(snippet: str, timeout: int = 120) -> subprocess.CompletedProcess:
    header = (
        "import sys, os\n"
        f"sys.path.insert(0, {str(_SDK_ROOT)!r})\n"
        f"os.environ.update({_ENV!r})\n"
        "import warnings\n"
        "warnings.filterwarnings('ignore')\n"
    )
    return subprocess.run(
        [sys.executable, "-c", header + _SERVER + textwrap.dedent(snippet)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _result(proc: subprocess.CompletedProcess) -> dict:
    """The RESULT line the snippet prints, or a failure with the real stderr."""
    assert proc.returncode == 0, proc.stderr[-3000:]
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[len("RESULT ") :])
    raise AssertionError(f"no RESULT line\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")


@pytest.mark.parametrize("name,modules", EXAMPLES, ids=[e[0] for e in EXAMPLES])
def test_example_constructs_and_stays_silent_on_import(name, modules):
    """Importing an example builds its agent, raises nothing, and calls nothing.

    The construction half is what catches a wrong keyword argument in a
    framework constructor. The zero-request half is what stops an example from
    quietly doing real work — an LLM call, a billed retrieve — at import time,
    which would also mean this very test was paying for model calls.
    """
    if not _installed(*modules):
        pytest.skip(f"{name}: {modules} not installed")

    proc = _run(f"""
        import json, runpy
        os.environ["ANONA_BASE_URL"] = BASE
        runpy.run_path({str(_EXAMPLES / name)!r}, run_name="not_main")
        print("RESULT " + json.dumps({{"requests": len(SEEN)}}))
    """)
    result = _result(proc)
    assert result["requests"] == 0, (
        f"{name} issued {result['requests']} HTTP call(s) on import — the "
        f"`if __name__ == \"__main__\"` guard is not covering everything"
    )


@pytest.mark.skipif(not _installed("crewai"), reason="crewai not installed")
def test_crewai_example_tools_reach_anona():
    """Drive the example's own tools, no model involved, and check the wire.

    CrewAI's memory is discretionary — the model decides whether to call these
    tools — so the thing worth asserting is that when they *are* called, the
    example's wiring sends the right thing to the right space.
    """
    proc = _run(f"""
        import json, runpy
        os.environ["ANONA_BASE_URL"] = BASE   # the example reads this itself
        mod = runpy.run_path({str(_EXAMPLES / "crewai_crew.py")!r}, run_name="not_main")
        tools = {{t.name: t for t in mod["storage"].as_tools()}}
        search = next(n for n in tools if "Search" in n)
        save = next(n for n in tools if "Save" in n)
        recalled = tools[search].run(query="what is the database standard?")
        tools[save].run(content="We standardised on Postgres 16")
        print("RESULT " + json.dumps({{
            "tool_names": sorted(tools),
            "recalled": recalled,
            "seen": SEEN,
        }}))
    """)
    result = _result(proc)

    # Namespaced names are load-bearing: CrewAI's built-in memory tool wins an
    # unprefixed collision in Crew._merge_tools.
    assert all(n.startswith("Anona: ") for n in result["tool_names"]), result["tool_names"]

    paths = [r["path"] for r in result["seen"]]
    assert any("retrieve" in p for p in paths), paths
    assert any("record" in p for p in paths), paths

    record = next(r for r in result["seen"] if "record" in r["path"])
    assert record["body"]["space_id"] == "examples-crewai"
    assert "Postgres 16" in record["body"]["content"]
    assert record["auth"] == "Bearer anona_test_example"

    # What the tool hands back to the model is the memory, not a raw payload.
    assert "shellfish" in result["recalled"]


@pytest.mark.skipif(not _installed("strands"), reason="strands not installed")
def test_strands_example_tools_reach_anona_and_carry_scope():
    """Same idea for Strands, plus the scope the example sets on its bridge.

    `user_id="customer-42"` in the example is the isolation guarantee it
    advertises in its own comments, so assert it actually reaches the wire as a
    scope field rather than being dropped.
    """
    proc = _run(f"""
        import json, runpy
        os.environ["ANONA_BASE_URL"] = BASE   # the example reads this itself
        mod = runpy.run_path({str(_EXAMPLES / "strands_agent.py")!r}, run_name="not_main")
        tools = {{t.tool_name: t for t in mod["agent"].tool_registry.registry.values()}}
        anona = sorted(n for n in tools if n.startswith("anona_"))
        mod["agent"].tool.anona_save_memory(content="Alice is allergic to shellfish")
        mod["agent"].tool.anona_recall_memory(query="what is Alice allergic to?")
        print("RESULT " + json.dumps({{"tools": anona, "seen": SEEN}}))
    """)
    result = _result(proc)

    assert result["tools"] == ["anona_recall_memory", "anona_save_memory"], result["tools"]

    record = next(r for r in result["seen"] if "record" in r["path"])
    assert record["body"]["space_id"] == "examples-strands"
    assert record["body"]["user_id"] == "customer-42", record["body"]
    retrieve = next(r for r in result["seen"] if "retrieve" in r["path"])
    assert retrieve["body"]["user_id"] == "customer-42", retrieve["body"]


def test_every_example_is_listed_in_the_readme():
    """A script nobody links to is a script nobody runs."""
    readme = (_EXAMPLES / "README.md").read_text()
    missing = [name for name, _ in EXAMPLES if name not in readme]
    assert not missing, f"examples/README.md does not mention: {missing}"

    on_disk = sorted(p.name for p in _EXAMPLES.glob("*.py"))
    assert on_disk == sorted(name for name, _ in EXAMPLES), (
        "examples/ and this test's EXAMPLES list have drifted"
    )
