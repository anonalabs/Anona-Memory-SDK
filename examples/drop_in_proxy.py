"""The drop-in proxy: your OpenAI code, plus memory across conversations.

    pip install anona openai
    export ANONA_API_KEY=anona_live_...
    python examples/drop_in_proxy.py

Point a stock OpenAI client at Anona and every chat call recalls relevant
memories into the prompt and stores the turn back — no other code change. This
script proves the loop: conversation one mentions a fact, conversation two is a
fresh message list, and the assistant still knows.
"""
import os
import time

from openai import OpenAI

from anona import AnonaClient

space = os.environ.get("ANONA_SPACE_ID", "examples-proxy")
base_url = os.environ.get("ANONA_BASE_URL", "https://api.anonalabs.com")

llm = OpenAI(
    api_key=os.environ["ANONA_API_KEY"],   # your Anona key, not an OpenAI key
    base_url=f"{base_url}/v1",
    default_headers={"X-Anona-Space-Id": space},  # set once, applies to every call
)

# Conversation one: mention something worth remembering.
first = llm.chat.completions.create(
    model="balanced",  # a tier alias; or any id from GET /v1/models
    messages=[{"role": "user", "content": "My cat is named Miso and she only eats salmon."}],
)
print("conversation 1:", first.choices[0].message.content)

# The turn is recorded in the background of the call; give extraction a moment.
time.sleep(10)

# Conversation two: a brand-new message list. No history passed. The proxy
# retrieves the stored fact and injects it before the model answers.
second = llm.chat.completions.create(
    model="balanced",
    messages=[{"role": "user", "content": "What is my cat's name and what does she eat?"}],
)
print("conversation 2:", second.choices[0].message.content)

# Cleanup with the regular SDK client.
admin = AnonaClient(api_key=os.environ["ANONA_API_KEY"], base_url=base_url)
admin.delete_space(space)
admin.close()
