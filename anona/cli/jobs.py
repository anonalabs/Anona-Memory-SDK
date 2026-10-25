"""`--wait`: poll background jobs until they finish.

Shared by `record --async`, `record --batch` and `documents upload`. Every
poll is a GET of the job, which costs no credits and is safe to retry.

The submit and the polls are separate SDK calls on purpose. `_invoke` renews
an expired command-line login after a 401 and runs its function once more;
were the submit and the polling one function, a token expiring mid-poll would
re-run the submit too, and a write would land twice.
"""

from __future__ import annotations

import time

from . import exits
from .credential import redact
from .memory import Bearer, _invoke, _printable, notice

# What a job can still become: anything else is final.
_RUNNING = frozenset({"pending", "processing", "running", "queued"})

# What `jobs get` and a --wait say when the server reports a job it does not
# hold. The API answers that with HTTP 200 and `"status": "not_found"` (an id
# from another space, or one that never existed), so the SDK raises nothing
# and the CLI has to turn it into the not-found outcome itself.
NOT_FOUND_LINE = "anona: job_not_found: No job with that id in this space."


def is_not_found(doc: object) -> bool:
    """The server answered, and the answer is that it holds no such job."""
    return isinstance(doc, dict) and str(doc.get("status") or "") == "not_found"


# Poll back-off, in seconds. Module-level so a test can shorten it.
FIRST_INTERVAL = 1.0
MAX_INTERVAL = 5.0


def _p(value: object) -> str:
    return "-" if value is None or value == "" else _printable(redact(str(value)))


def outcome(docs: list[dict]) -> int:
    """The exit code a set of finished jobs adds up to.

    A failed or cancelled job fails the command; a job the server no longer
    knows is NOT_FOUND. Anything still running is STILL_RUNNING (the caller
    only asks once the wait has given up).
    """
    statuses = [str(d.get("status") or "") for d in docs]
    if any(s in _RUNNING for s in statuses):
        return exits.STILL_RUNNING
    if any(s in ("failed", "cancelled") for s in statuses):
        return exits.FAILED
    if any(s == "not_found" for s in statuses):
        return exits.NOT_FOUND
    return exits.OK


def wait(base_url: str, bearer: Bearer, space: str | None, job_ids: list[str], timeout: float) -> tuple[bool, object]:
    """Poll until every job is final or `timeout` seconds pass.

    `(True, [job documents in job_ids order])`, or `(False, exit code)` when a
    poll itself failed (already reported). Always polls at least once, so
    `--wait-timeout 0` is a single status check.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    docs: dict[str, dict] = {}
    pending = list(job_ids)
    interval = FIRST_INTERVAL
    announced = False
    while True:
        for job_id in pending:
            done, doc = _invoke(base_url, bearer, lambda client, j=job_id: client.get_job(str(space), j))
            if not done:
                return False, doc
            docs[job_id] = doc if isinstance(doc, dict) else {"job_id": job_id}
        pending = [j for j in job_ids if str(docs[j].get("status") or "") in _RUNNING]
        if not pending:
            break
        # Announced before the deadline check: whether the first poll left any
        # time is a race, and the notice must not depend on it.
        if not announced:
            notice(f"Waiting for {len(pending)} job(s) to finish...")
            announced = True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
        interval = min(interval * 1.5, MAX_INTERVAL)
    return True, [docs[j] for j in job_ids]


def render(doc: dict, job_id: str | None = None) -> None:
    """One job, as `anona jobs get` prints it."""
    line = f"Job {_p(doc.get('job_id') or job_id)}: {_p(doc.get('status'))}"
    if doc.get("memory_count") is not None:
        line += f", {_p(doc['memory_count'])} memories"
    if doc.get("items_submitted") is not None:
        line += f" from {_p(doc['items_submitted'])} items"
    print(line + ".")
    if doc.get("error"):
        print(f"Error: {_p(doc['error'])}")
    ids = doc.get("memory_ids")
    if isinstance(ids, list) and ids:
        print("Memory ids: " + ", ".join(_p(i) for i in ids))


def stored_nothing(docs: list[dict]) -> bool:
    """Every job completed and the server says it stored no memory at all.

    Only when every count is known: an unknown count (an upload, an older
    server) is not evidence that nothing was stored.
    """
    counts = [d.get("memory_count") for d in docs]
    return (
        bool(docs)
        and all(d.get("status") == "completed" for d in docs)
        and all(isinstance(c, int) and not isinstance(c, bool) for c in counts)
        and sum(c for c in counts if isinstance(c, int)) == 0
    )
