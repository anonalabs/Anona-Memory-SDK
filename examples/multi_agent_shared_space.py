"""Two agents, one space: shared knowledge with per-agent working sets.

    pip install anona
    export ANONA_API_KEY=anona_live_...
    python examples/multi_agent_shared_space.py

`agent_id` works like `user_id`, but for the writer: a researcher agent and a
writer agent can share one space, keep their own working notes separate, and
still hand knowledge across when a read asks without a scope.
"""
import os

from anona import AnonaClient

client = AnonaClient(api_key=os.environ["ANONA_API_KEY"])
space = os.environ.get("ANONA_SPACE_ID", "examples-multi-agent")

# The researcher stores what it found, under its own agent scope.
client.record(
    space_id=space,
    content="Competitor X launched usage-based pricing at $0.80 per thousand calls.",
    agent_id="researcher",
)
client.record(
    space_id=space,
    content="Competitor X's changelog shows weekly releases since March.",
    agent_id="researcher",
)

# The writer keeps its own drafting notes, separately scoped.
client.record(
    space_id=space,
    content="Draft angle: lead with predictable pricing, avoid feature tables.",
    agent_id="writer",
)

# Scoped read: the writer's own notes only.
print("-- writer's working set --")
for row in client.retrieve(space_id=space, query="drafting notes", agent_id="writer"):
    print("  ", row["content"])

# Unscoped read: the whole space — this is the handoff. The writer's prompt
# can pull the researcher's findings without knowing who wrote them.
print("\n-- everything, for the handoff --")
for row in client.retrieve(space_id=space, query="What do we know about competitor X?"):
    print("  ", row["content"])

client.delete_space(space)
client.close()
