"""Microsoft Agent Framework agent with Anona memory.

    pip install 'anona[msagent]' agent-framework-openai
    export ANONA_API_KEY=anona_live_... OPENAI_API_KEY=sk-...
    python examples/ms_agent_framework.py

`context_providers=[...]` is the whole integration. The framework calls the
provider before and after every `agent.run()`, once per run regardless of how
many tool round-trips happen inside it — so there is no per-step duplication
to work around here.
"""
import asyncio
import os

from agent_framework import Agent
from agent_framework.openai import OpenAIChatClient

from anona.integrations import MemoryBridge
from anona.integrations.ms_agent import AnonaContextProvider

bridge = MemoryBridge(
    api_key=os.environ["ANONA_API_KEY"],
    space_id=os.environ.get("ANONA_SPACE_ID", "examples-msagent"),
    user_id="customer-42",
)

agent = Agent(
    client=OpenAIChatClient("gpt-4o-mini"),
    instructions="You are a concise assistant.",
    # Your instructions stay first; the memory block is appended after them,
    # never in place of them.
    context_providers=[AnonaContextProvider(bridge=bridge)],
)


async def main() -> None:
    for turn in [
        "I'm allergic to shellfish, remember that.",
        "Suggest somewhere to eat tonight.",
    ]:
        response = await agent.run(turn)
        print(f"\n> {turn}\n{response}")


if __name__ == "__main__":
    asyncio.run(main())
    bridge.close()
