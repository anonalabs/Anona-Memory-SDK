"""A file becomes answerable memory — upload, wait, ask.

    pip install anona
    export ANONA_API_KEY=anona_live_...
    python examples/document_qa.py

Uploaded documents land in the same space as recorded memories, so one
`retrieve` call answers from both. Ingestion is asynchronous: the upload
returns job ids, and a job reaching `completed` means that file is searchable.
"""
import os
import time

from anona import AnonaClient, AnonaError

client = AnonaClient(api_key=os.environ["ANONA_API_KEY"])
space = os.environ.get("ANONA_SPACE_ID", "examples-document-qa")
try:
    client.create_space(space)
except AnonaError:
    pass  # left over from an earlier run — we only need it to exist

policy = (
    "Refund policy: hardware purchases can be returned within 45 days. "
    "Software subscriptions are refundable within 14 days of first charge. "
    "Enterprise contracts follow their negotiated terms."
)
job = client.upload_file(space_id=space, file=policy.encode(), filename="refund-policy.txt")

for job_id in job["job_ids"]:
    while True:
        status = client.get_job(space_id=space, job_id=job_id)["status"]
        print("ingestion:", status)
        if status in ("completed", "failed"):
            break
        time.sleep(5)

# Ask a question the file answers.
for row in client.retrieve(space_id=space, query="How long do I have to return hardware?"):
    where = f" (from {row['document_id']})" if row.get("document_id") else ""
    print(f"  {row['content']}{where}")

# The document is a first-class object: list it, and delete it to remove every
# memory extracted from it in one call.
for doc in client.list_documents(space_id=space):
    print("document:", doc["document_id"], "->", doc["memory_count"], "memories")

client.delete_space(space)
client.close()
