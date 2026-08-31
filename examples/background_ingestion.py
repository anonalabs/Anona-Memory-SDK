"""High-volume writes: queue them, keep serving, poll for the outcome.

    pip install anona
    export ANONA_API_KEY=anona_live_...
    python examples/background_ingestion.py

A blocking `record` waits for fact extraction — an LLM call — before
returning. In a request path that latency belongs elsewhere: pass
`background=True` (or use `record_batch`, which is always asynchronous) to get
a `job_id` immediately, and poll `get_job` for the stored memory ids.
"""
import os
import time

from anona import AnonaClient

client = AnonaClient(api_key=os.environ["ANONA_API_KEY"])
space = os.environ.get("ANONA_SPACE_ID", "examples-ingestion")


def wait(job_id: str) -> dict:
    while True:
        job = client.get_job(space_id=space, job_id=job_id)
        if job["status"] in ("completed", "failed"):
            return job
        time.sleep(5)


# One write, queued: sub-second return instead of waiting on extraction.
queued = client.record(
    space_id=space,
    content="The incident review moved to Wednesdays.",
    background=True,
)
print("queued instantly as job", queued["job_id"])

# A hundred at a time: record_batch is the bulk form, always asynchronous.
batch = client.record_batch(
    space_id=space,
    items=[
        {"content": "Sprint 41 closed with 34 points."},
        {"content": "Sprint 42 planning is Thursday.", "user_id": "scrum-bot"},
    ],
)
print("batch accepted:", batch["accepted"], "items")

done = wait(queued["job_id"])
print("single write stored", done["memory_count"], "memories:", done["memory_ids"])

done = wait(batch["job_id"])
print("batch stored", done["memory_count"], "memories")

client.delete_space(space)
client.close()
