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

import ast
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
            # The routed shapes, for space_routing.py. Harmless to the rest:
            # nothing else reads them.
            "results": [{"memory_id": "m1", "content": "Acme is on net-30"}],
            "routed_to": [
                {"space_id": "billing", "confidence": 0.9, "stage": "model", "reason": "invoice terms"}
            ],
            "searched": [
                {"space_id": "billing", "confidence": 0.9, "stage": "model", "reason": "invoice terms"}
            ],
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_POST = _handle
    do_GET = _handle
    # space_routing.py sets a routing profile (PUT) and cleans its spaces up
    # (DELETE); without these the example cannot be executed here at all.
    do_PUT = _handle
    do_DELETE = _handle
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


def test_space_routing_example_runs_and_routes():
    """The one SDK-only example executed here, because it is about the wire.

    `route: "auto"` is invisible in a diff — an example that names a space by
    accident, or sends `space_id` alongside `route`, looks exactly like a
    correct one on the page. So this runs the script end to end against the
    fake server and reads what it actually sent.
    """
    proc = _run(f"""
        import json, runpy
        os.environ["ANONA_BASE_URL"] = BASE
        runpy.run_path({str(_EXAMPLES / "space_routing.py")!r}, run_name="__main__")
        print("RESULT " + json.dumps({{"seen": SEEN}}))
    """)
    seen = _result(proc)["seen"]

    # The routing profile is declared before anything is routed against it.
    profile = next(r for r in seen if "routing-settings" in r["path"])
    assert profile["method"] == "PUT"
    assert profile["body"]["charter"]
    assert profile["body"]["topics"]

    routed_write = next(r for r in seen if r["path"].endswith("/v1/record"))
    assert routed_write["body"]["route"] == "auto"
    # Naming a space as well would be a 422 route_conflict.
    assert "space_id" not in routed_write["body"]
    assert routed_write["body"]["fallback_space_id"]

    retrieves = [r for r in seen if r["path"].endswith("/v1/retrieve")]
    routed_reads = [r for r in retrieves if r["body"].get("route") == "auto"]
    # The routed read, and the abstention — which deliberately names no
    # fallback, because on a read the fallback space is the worst place to look.
    assert len(routed_reads) == 2, [r["body"] for r in retrieves]
    assert all("space_id" not in r["body"] for r in routed_reads)
    assert all("fallback_space_id" not in r["body"] for r in routed_reads[1:])
    # And the addressed read still names its space and no route.
    addressed = [r for r in retrieves if "space_id" in r["body"]]
    assert len(addressed) == 1 and "route" not in addressed[0]["body"]


def test_every_example_is_listed_in_the_readme():
    """A script nobody links to is a script nobody runs."""
    readme = (_EXAMPLES / "README.md").read_text()
    missing = [name for name, _ in EXAMPLES if name not in readme]
    assert not missing, f"examples/README.md does not mention: {missing}"

    # Every script on disk, not just the executable ones. `EXAMPLES` above is
    # deliberately only the framework examples -- the ones that can be driven
    # without a live server -- so comparing the directory against it fails the
    # moment an SDK-only example is added, which is what it had been doing.
    # The property actually worth holding is that nothing on disk is unlinked.
    on_disk = sorted(p.name for p in _EXAMPLES.glob("*.py"))
    unlinked = [name for name in on_disk if name not in readme]
    assert not unlinked, f"examples/ scripts nobody links to: {unlinked}"


def _deleting_examples() -> list[Path]:
    """Every example script that destroys a space."""
    out = []
    for path in sorted(_EXAMPLES.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "delete_space"
            ):
                out.append(path)
                break
    return out


def test_an_example_never_deletes_a_space_its_caller_named():
    """A destructive script must not take its space id from the environment.

    Every scenario script ends in `delete_space`, so whatever names that space
    is what gets destroyed. They used to read `ANONA_SPACE_ID` — which the
    README told people to export — so `ANONA_SPACE_ID=my-space` plus any
    example deleted `my-space`, in production, with no prompt. The id is
    generated per run instead, and this asserts the two can never meet again.
    """
    assert _deleting_examples(), "no example calls delete_space; did the API change?"

    offenders = [
        path.name
        for path in _deleting_examples()
        if "ANONA_SPACE_ID" in path.read_text()
    ]
    assert not offenders, (
        "these examples delete a space whose id can come from the caller's "
        f"environment: {offenders}"
    )


def test_a_deleting_example_generates_its_space_id_per_run():
    """Not just caller-proof — a fresh id, so two runs never fight.

    A constant id would satisfy the test above while still letting one run
    delete the space another run is mid-way through using.
    """
    stale = [
        path.name
        for path in _deleting_examples()
        if "uuid.uuid4()" not in path.read_text()
    ]
    assert not stale, (
        f"these examples delete a space with a fixed id: {stale} — generate it "
        "per run with uuid.uuid4() so concurrent runs cannot collide"
    )


def test_an_example_only_ever_deletes_the_space_it_built():
    """The deleted name must be the variable the script derived itself.

    `space_routing.py` is the reason this is separate: it also PUTs a charter
    and topics onto its space, so a caller-supplied id there rewrote their
    routing configuration before anything was deleted. Both halves have to
    read from a locally-built id.
    """
    for path in _deleting_examples():
        tree = ast.parse(path.read_text())
        # Names bound from f-strings at module level: the generated space ids.
        built = {
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.JoinedStr)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        assert built, f"{path.name} builds no space id of its own"
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "delete_space"
                and node.args
            ):
                arg = node.args[0]
                # Either the generated name, or a loop variable over the
                # generated names (space_routing deletes two).
                if isinstance(arg, ast.Name) and arg.id not in built:
                    assert any(
                        isinstance(n, ast.For)
                        and isinstance(n.target, ast.Name)
                        and n.target.id == arg.id
                        and any(
                            isinstance(e, ast.Name) and e.id in built
                            for e in ast.walk(n.iter)
                        )
                        for n in ast.walk(tree)
                    ), f"{path.name} deletes '{arg.id}', which it did not build"
