"""Why a search did not return the memory you expected.

    pip install anona
    export ANONA_API_KEY=anona_live_...
    python examples/context_receipts.py

A search returns a ranked list. What it does not tell you is what it *nearly*
returned: near-duplicates collapsed, low scorers dropped, everything past the
limit cut, and, before any of that, candidates the search itself ranked and
discarded. A context receipt is that decision written down.

The receipt carries ids, scores and reason codes only, never memory content,
which is what makes a receipt id safe to paste into a support ticket.

The question this exists for is the last one below: "I know that memory is in
there, so why isn't it in my results?" Until `explain`, a memory the search
never matched looked exactly like a memory that did not exist.
"""
import os
import time

from anona import AnonaClient

client = AnonaClient(api_key=os.environ["ANONA_API_KEY"])
space = os.environ.get("ANONA_SPACE_ID", "examples-context-receipts")

job = client.record_batch(
    space_id=space,
    items=[
        {"content": "Priya owns the billing service and is on call for it."},
        {"content": "Billing runs on Postgres 16 behind pgbouncer."},
        {"content": "The office coffee machine is a Rancilio Silvia."},
    ],
)
while True:
    finished = client.get_job(space_id=space, job_id=job["job_id"])
    if finished["status"] in ("completed", "failed"):
        break
    time.sleep(5)

# A completed write reports the ids it created, which is how this script knows
# what *should* be findable without having to search for it first.
written = finished.get("memory_ids") or []

# `retrieve` is unchanged and still returns a plain list. `retrieve_receipt` is
# the same search, and returns the receipt id alongside the results, because a
# bare list has nowhere to put it. `receipt_detail="full"` also asks the search
# to account for its own cuts: without it the receipt can only describe what
# happened after the search returned.
res = client.retrieve_receipt(
    space_id=space,
    query="who is on call for billing?",
    limit=1,
    receipt_detail="full",
)
print("-- results --")
for row in res.memories:
    print("  ", row["content"])

# The result stands in for the list wherever one was expected.
print(f"\n{len(res)} returned, receipt {res.receipt_id}")

receipt = client.get_receipt(res.receipt_id)
print("\n-- what was cut, and why --")
for entry in receipt["excluded"]:
    print(f"   {entry['reason']:<14} {entry['detail']}")
if not receipt["excluded"]:
    print("   nothing was cut")

# `excluded_truncated` is how a capped manifest admits it is capped, so an
# incomplete list never reads as a complete one.
if receipt["excluded_truncated"]:
    print("   ...and more, not listed:", receipt["excluded_truncated"])

# Now the real question: every memory written above that did not come back.
returned = {row["memory_id"] for row in res.memories}
missing = [mid for mid in written if mid not in returned]

print("\n-- accounting for the ones that did not come back --")
for memory_id in missing:
    verdict = client.explain(res.receipt_id, memory_id)
    print(f"\n   {memory_id}")
    print(f"   outcome: {verdict['outcome']}")
    if verdict["outcome"] == "excluded":
        # It was considered and dropped. `stage` says where.
        print(f"   stage:   {verdict['stage']} — {verdict['detail']}")
        # Which kind of matching found it, and how well. A memory found by
        # `keyword` but not `semantic` shares words with your query but not
        # meaning; the reverse is the more usual case.
        found_by = {arm: rank for arm, rank in verdict["arms"].items() if rank is not None}
        print(f"   found by: {found_by or 'nothing'}")
    elif verdict["outcome"] == "not_retrieved":
        # No stage matched it at all. This is the one worth acting on: a
        # bigger `limit` or a lower score floor will not bring it back, because
        # nothing ever found it. The query wording, or the scope you searched,
        # is what to change.
        print("   nothing matched it — this is the query, not a threshold")

client.delete_space(space)
client.close()
