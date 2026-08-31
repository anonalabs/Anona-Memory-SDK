"""The entity graph a space builds on its own, and how to read it.

    pip install anona
    export ANONA_API_KEY=anona_live_...
    python examples/knowledge_graph.py

Extraction names the entities in every memory as it stores them. The graph API
exposes that as nodes plus co-occurrence edges — two entities appearing in the
same memory, weighted by how often. Edges are not typed predicates; read them
as "these keep showing up together", which is exactly what a UI needs.
"""
import os

from anona import AnonaClient

client = AnonaClient(api_key=os.environ["ANONA_API_KEY"])
space = os.environ.get("ANONA_SPACE_ID", "examples-graph")

for fact in [
    "Mira owns the payments service and reviews every schema change.",
    "The payments service depends on the ledger database.",
    "Mira and Tomas pair on the ledger database migrations.",
    "Tomas maintains the deploy pipeline.",
]:
    client.record(space_id=space, content=fact)

graph = client.get_graph(space_id=space)
print(f"-- {len(graph['nodes'])} nodes, {len(graph['edges'])} edges --")
for edge in graph["edges"]:
    print(f"  {edge['source_label']} — {edge['target_label']}  (x{edge['weight']})")

# Entities are addressable on their own. `observations` fills as the space
# consolidates what it has stored about each one.
entities = client.list_entities(space_id=space)
first = client.get_entity(space_id=space, entity_id=entities[0]["id"])
print(f"\n-- {first['name']}: mentioned {first['mention_count']}x --")
for obs in first["observations"]:
    print("  ", obs)

client.delete_space(space)
client.close()
