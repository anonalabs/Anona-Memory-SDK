"""LangChain / LangGraph agent with Anona memory.

    pip install 'anona[langchain]' langchain-openai
    export ANONA_API_KEY=anona_live_... OPENAI_API_KEY=sk-...
    python examples/langchain_agent.py

One middleware is the whole integration. It fetches a context block once per
turn, injects it as a system message ahead of every model call in that turn,
and stores the finished turn when the run ends. Run it twice: the second run
answers from what the first one stored.
"""
import asyncio
import os

from langchain.agents import create_agent

from anona.integrations import MemoryBridge
from anona.integrations.langchain import AnonaMemory

bridge = MemoryBridge(
    api_key=os.environ["ANONA_API_KEY"],
    # Only needed to point at a different deployment; the default is
    # https://api.anonalabs.com.
    base_url=os.environ.get("ANONA_BASE_URL", "https://api.anonalabs.com"),
    space_id=os.environ.get("ANONA_SPACE_ID", "examples-langchain"),
    # Scope every read and write to one end user. Memories written under this
    # user are never returned to another one, so a single space can back your
    # whole customer base.
    user_id="customer-42",
)

agent = create_agent(
    model="gpt-4o-mini",
    system_prompt="You are a concise assistant.",
    middleware=[AnonaMemory(bridge=bridge)],
)


async def main() -> None:
    for turn in [
        "I'm allergic to shellfish, remember that.",
        "Suggest somewhere to eat tonight.",
    ]:
        result = await agent.ainvoke({"messages": [{"role": "user", "content": turn}]})
        print(f"\n> {turn}\n{result['messages'][-1].content}")


if __name__ == "__main__":
    asyncio.run(main())
    # close() is synchronous and tears down both HTTP clients. Call it after
    # the loop has finished rather than inside main(): closing an async client
    # from inside the loop that owns it is the one shape it cannot do cleanly.
    bridge.close()
