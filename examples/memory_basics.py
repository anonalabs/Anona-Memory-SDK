"""The core loop: record facts, search them, reason across them.

    pip install anona
    export ANONA_API_KEY=anona_live_...
    python examples/memory_basics.py

Three calls carry almost every integration: `record` stores knowledge,
`retrieve` finds what is relevant to a question, and `reason` synthesizes an
answer across everything the space holds. `get_context` is `retrieve` shaped
for prompts — one ready-to-paste string instead of ranked rows.
"""
import os

from anona import AnonaClient

client = AnonaClient(api_key=os.environ["ANONA_API_KEY"])
space = os.environ.get("ANONA_SPACE_ID", "examples-basics")

client.record(space_id=space, content="Dana leads the mobile team and prefers RFC-style proposals.")
client.record(space_id=space, content="The mobile team ships on a two-week train, cutting on Mondays.")
client.record(space_id=space, content="Dana rejected the last proposal for missing a rollback plan.")

# Ranked rows, each with a relevance score.
print("-- retrieve --")
for row in client.retrieve(space_id=space, query="How should I pitch a change to Dana?"):
    print(f"  {row['relevance_score']:.2f}  {row['content']}")

# One synthesized answer across all of it.
print("\n-- reason --")
print(client.reason(space_id=space, query="What should I prepare before proposing a change to the mobile team?"))

# The same recall, shaped for pasting into a prompt you build yourself.
print("\n-- get_context --")
print(client.get_context(space_id=space, query="pitching a change to Dana", max_tokens=300))

client.delete_space(space)
client.close()
