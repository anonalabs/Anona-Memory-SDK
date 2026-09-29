"""Let Anona pick the space: `route="auto"` on a write, and on a read.

    pip install anona
    export ANONA_API_KEY=anona_live_...
    python examples/space_routing.py

Naming a space is the normal way to work, and nothing here changes it. But an
agent that files everything it learns has to decide where each thing belongs,
and that decision is usually better made against what the spaces are actually
for. `route="auto"` hands it over: the write answers with `routed_to`, the read
answers with `searched`, and both say which stage decided — a `rule` you wrote,
the `model`, or the `fallback`.

Read those fields. A misroute is a perfectly successful write to the wrong
space, and nothing else tells it apart from a correct one.

Routing matches against each space's profile, which is REST-only today — the
SDK does not model it yet, so this script sets it with a plain HTTP call.
"""
import os

import httpx

from anona import AnonaClient

BASE_URL = os.environ.get("ANONA_BASE_URL", "https://api.anonalabs.com")
KEY = os.environ["ANONA_API_KEY"]

client = AnonaClient(api_key=KEY, base_url=BASE_URL)
billing = os.environ.get("ANONA_SPACE_ID", "examples-routing-billing")
oncall = f"{billing}-oncall"

for space in (billing, oncall):
    client.create_space(name=space)

# Routing has nothing to match on until a space says what it is for. A charter
# is a sentence; topics are the words a question about this space would use.
# Both are declared — the server also observes the terms you write and infers
# more from the entity graph, and those layers are its own.
for space, charter, topics in (
    (billing, "Invoicing, payment terms and collections.", ["invoice", "payment", "net-30", "dunning"]),
    (oncall, "Production incidents, pager rotations and postmortems.", ["incident", "pager", "outage", "rollback"]),
):
    httpx.put(
        f"{BASE_URL}/v1/spaces/{space}/routing-settings",
        headers={"Authorization": f"Bearer {KEY}"},
        json={"charter": charter, "topics": topics, "auto_route_enabled": True},
    ).raise_for_status()

# A write that names no space. `fallback_space_id` is where it lands if nothing
# fits — on a write that is right, because everything has to be stored somewhere.
print("-- routed write --")
result = client.record(
    content="Acme is on net-30 and has never paid late.",
    route="auto",
    fallback_space_id=billing,
)
for target in result["routed_to"]:
    print(f"  stored in {target['space_id']} by {target['stage']}: {target['reason']}")

# A read that names no space. `searched` says where it looked, and why.
print("\n-- routed read --")
memories = client.retrieve(query="what are Acme's payment terms?", route="auto")
for looked in memories.searched:
    print(f"  searched {looked['space_id']} by {looked['stage']}: {looked['reason']}")
for row in memories:
    print("  ", row["content"])

# A question no space is for. Note there is no `fallback_space_id` here, and
# that it has no default on a read: the fallback space is where everything that
# fitted nowhere ends up, which makes it the least topically coherent space you
# own and so the worst place to look for an answer. Searching it would return
# irrelevant memories that read exactly like real ones. So routing abstains —
# `searched` is still one entry, with a null `space_id` and the reason, because
# an empty list has nowhere to carry that.
print("\n-- abstention --")
nothing = client.retrieve(query="what is the airspeed of an unladen swallow?", route="auto")
looked = nothing.searched[0]
print(f"  searched: {looked['space_id']} ({looked['reason']})")
print(f"  results: {list(nothing) or 'none, correctly'}")

# `searched` is None on an addressed read — there was no decision to report.
print("\n-- addressed read, unchanged --")
addressed = client.retrieve(space_id=billing, query="payment terms")
print("  searched:", addressed.searched)

for space in (billing, oncall):
    client.delete_space(space)
client.close()
