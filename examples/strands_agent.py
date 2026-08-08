"""AWS Strands agent with Anona memory as two tools.

    pip install 'anona[strands]'
    export ANONA_API_KEY=anona_live_... AWS_PROFILE=...
    python examples/strands_agent.py

`anona_tools()` returns `anona_recall_memory` and `anona_save_memory`. The
model decides when to call them, so the system prompt has to say when — that
sentence is part of the integration, not decoration.
"""
import os

from strands import Agent

from anona.integrations import MemoryBridge
from anona.integrations.strands import anona_tools

bridge = MemoryBridge(
    api_key=os.environ["ANONA_API_KEY"],
    space_id=os.environ.get("ANONA_SPACE_ID", "examples-strands"),
    user_id="customer-42",
)

agent = Agent(
    tools=anona_tools(bridge),
    system_prompt=(
        "You have long-term memory. Call anona_recall_memory before answering "
        "questions about the user, and anona_save_memory when you learn "
        "something worth keeping."
    ),
)

if __name__ == "__main__":
    for turn in [
        "I'm allergic to shellfish, remember that.",
        "Suggest somewhere to eat tonight.",
    ]:
        print(f"\n> {turn}\n{agent(turn)}")

    bridge.close()
