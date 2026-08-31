"""Configure a space: webhooks instead of polling, and per-space defaults.

    pip install anona
    export ANONA_API_KEY=anona_live_...
    python examples/webhooks_and_settings.py

Three per-space configuration surfaces, all free to call:

- **webhooks** — Anona calls your endpoint when memories are stored or
  consolidated, so an async pipeline needs no polling loop.
- **chat settings** — defaults for the drop-in proxy (how many memories to
  inject, token budget, auto-record), so stock SDKs need no per-call knobs.
- **extraction settings** — how aggressively text becomes memories.
"""
import os

from anona import AnonaClient, AnonaError

client = AnonaClient(api_key=os.environ["ANONA_API_KEY"])
space = os.environ.get("ANONA_SPACE_ID", "examples-config")
try:
    client.create_space(space)
except AnonaError:
    pass  # left over from an earlier run — we only need it to exist

# -- webhooks: full lifecycle ------------------------------------------------
hook = client.create_webhook(
    space_id=space,
    url="https://example.com/anona-hook",   # your HTTPS endpoint
    event_types=["memory.created"],
)
# The signing secret appears once, here — store it and verify every delivery.
print("registered:", hook["id"], hook["event_types"])

client.update_webhook(space_id=space, webhook_id=hook["id"], enabled=False)
print("paused it; current webhooks:", [w["id"] for w in client.list_webhooks(space_id=space)])

# Deliveries are inspectable after the fact — status, attempts, response codes.
deliveries = client.list_webhook_deliveries(space_id=space, webhook_id=hook["id"])
print("deliveries so far:", len(deliveries.get("deliveries", [])))

client.delete_webhook(space_id=space, webhook_id=hook["id"])

# -- proxy defaults ----------------------------------------------------------
client.set_chat_settings(space_id=space, memory_limit=8, auto_record=True)
print("chat defaults:", client.get_chat_settings(space_id=space))
client.reset_chat_settings(space_id=space)

# -- extraction --------------------------------------------------------------
client.set_extraction_settings(space_id=space, mode="verbose")
print("extraction:", client.get_extraction_settings(space_id=space))
client.reset_extraction_settings(space_id=space)

client.delete_space(space)
client.close()
