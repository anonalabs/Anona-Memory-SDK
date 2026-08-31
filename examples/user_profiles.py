"""Everything the space knows about one end user, and questions about them.

    pip install anona
    export ANONA_API_KEY=anona_live_...
    python examples/user_profiles.py

Once writes are scoped per user (see support_bot_scoping.py), two calls turn
that scope into product features: `get_user_profile` returns everything known
about one user — as data, or as a prompt-ready block — and `ask_about_user`
answers a question from that user's memories only.
"""
import os

from anona import AnonaClient

client = AnonaClient(api_key=os.environ["ANONA_API_KEY"])
space = os.environ.get("ANONA_SPACE_ID", "examples-profiles")

for fact in [
    "Priya is on the annual plan and pays by invoice.",
    "Priya's team is in Berlin, so she prefers morning meetings CET.",
    "Priya asked for SSO onboarding docs twice this quarter.",
]:
    client.record(space_id=space, content=fact, user_id="priya")

profile = client.get_user_profile(space_id=space, user_id="priya")
print(f"-- profile: {profile['memory_count']} things known --")
for m in profile["memories"]:
    print("  ", m["text"])

# The same profile as one prompt-ready block, budgeted to fit your prompt.
block = client.get_user_profile(space_id=space, user_id="priya", format="block", context_max_tokens=300)
print("\n-- as a prompt block --")
print(block["context"])

# A question answered from this user's memories alone. The response names the
# model that actually answered — that is what the call's credits were billed at.
answer = client.ask_about_user(space_id=space, user_id="priya", query="How should we schedule a call with her?")
print("\n-- ask_about_user --")
print(answer["insights"])
print("answered by:", answer["model"])

# A user nobody has written about is not an error — a user is a scope tag, not
# a resource you register. Empty profile, memory_count 0.
ghost = client.get_user_profile(space_id=space, user_id="nobody-yet")
print("\nunknown user memory_count:", ghost["memory_count"])

client.delete_space(space)
client.close()
