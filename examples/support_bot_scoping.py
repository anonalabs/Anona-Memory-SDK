"""One space, many end users — scoping keeps their memories apart.

    pip install anona
    export ANONA_API_KEY=anona_live_...
    python examples/support_bot_scoping.py

A support bot serves every customer out of one space. Each write carries the
customer's `user_id`; each read carries the asking customer's `user_id` and can
only see that customer. Scoped search is strict — it never returns another
user's memories, nor memories stored without a scope.
"""
import os

from anona import AnonaClient

client = AnonaClient(api_key=os.environ["ANONA_API_KEY"])
space = os.environ.get("ANONA_SPACE_ID", "examples-support-bot")

# Two customers talk to the bot on the same day.
client.record(space_id=space, content="Alice's subscription renews on the 3rd of each month.", user_id="alice")
client.record(space_id=space, content="Alice reported the export button greyed out on Safari.", user_id="alice")
client.record(space_id=space, content="Bob asked to be contacted by email only, never phone.", user_id="bob")

# Alice's session sees Alice, and only Alice.
print("-- alice asks: what have I reported? --")
for row in client.retrieve(space_id=space, query="What issues has this customer reported?", user_id="alice"):
    print("  ", row["content"])

# Bob's session cannot see Alice's renewal date, however hard it asks.
print("\n-- bob asks about renewal dates --")
rows = client.retrieve(space_id=space, query="When does my subscription renew?", user_id="bob")
print("  ", [r["content"] for r in rows] or "nothing — that fact belongs to alice")

# reason() is scoped the same way: a synthesis for Bob is built only from Bob.
print("\n-- reason, as bob --")
print(client.reason(space_id=space, query="How should we contact this customer?", user_id="bob"))

client.delete_space(space)
client.close()
