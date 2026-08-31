"""Dated history: import it, filter on when things happened, replay the past.

    pip install anona
    export ANONA_API_KEY=anona_live_...
    python examples/time_travel.py

Every memory carries two clocks: when the event happened (`timestamp`, yours
to set) and when it was recorded (the system's). Three controls use them:
`occurred_after`/`occurred_before` filter on event time, `as_of` replays what
the space knew at an instant of record time, and `query_timestamp` moves the
"now" that recency and phrases like "last quarter" resolve against.
"""
import os
import time

from anona import AnonaClient

client = AnonaClient(api_key=os.environ["ANONA_API_KEY"])
space = os.environ.get("ANONA_SPACE_ID", "examples-time-travel")

# A backfill: both recorded today, but they HAPPENED months apart.
job = client.record_batch(
    space_id=space,
    items=[
        {"content": "The June offsite settled on the pricing overhaul.", "timestamp": "2026-06-10T09:00:00Z"},
        {"content": "The December board meeting approved the London office.", "timestamp": "2025-12-04T15:00:00Z"},
    ],
)
while True:
    status = client.get_job(space_id=space, job_id=job["job_id"])["status"]
    if status in ("completed", "failed"):
        break
    time.sleep(5)

# Event-time window: only what happened in June.
print("-- what was decided in June? --")
for row in client.retrieve(
    space_id=space,
    query="what was decided?",
    occurred_after="2026-06-01T00:00:00Z",
    occurred_before="2026-07-01T00:00:00Z",
):
    print("  ", row["content"])

# Point-in-time: a year ago the space knew nothing — both rows were recorded
# today, whatever their event dates say. `as_of` is about record time.
rows = client.retrieve(space_id=space, query="what was decided?", as_of="2025-01-01T00:00:00Z")
print("\n-- as of 2025-01-01 the space knew --")
print("  ", [r["content"] for r in rows] or "nothing, correctly")

# Moved "now": rank against June 2026, so "recent" means recent-to-then.
print("\n-- with now moved to 2026-06-15 --")
for row in client.retrieve(space_id=space, query="recent decisions", query_timestamp="2026-06-15T00:00:00Z"):
    print("  ", row["content"])

client.delete_space(space)
client.close()
