"""LlamaIndex agent with Anona as a memory block.

    pip install 'anona[llamaindex]' llama-index-llms-openai aiosqlite
    export ANONA_API_KEY=anona_live_... OPENAI_API_KEY=sk-...
    python examples/llamaindex_agent.py

`Memory` prepends this block's context to the system message on every model
call. Writes are different: LlamaIndex only waterfalls a turn down to a block
when its short-term token buffer overflows, so the *last* turn of a
conversation is never flushed on its own. The explicit `bridge.remember(...)`
below is the guaranteed-capture pattern for turn boundaries you care about.
"""
import asyncio
import os

from llama_index.core.agent.workflow import FunctionAgent
from llama_index.core.memory import Memory
from llama_index.llms.openai import OpenAI

from anona.integrations import MemoryBridge
from anona.integrations.llamaindex import AnonaMemoryBlock

bridge = MemoryBridge(
    api_key=os.environ["ANONA_API_KEY"],
    space_id=os.environ.get("ANONA_SPACE_ID", "examples-llamaindex"),
    user_id="customer-42",
)

memory = Memory.from_defaults(
    session_id="chat-1",
    # A real database, not the in-memory default: the default does not survive
    # across agent.run() calls, so nothing would ever build up to flush.
    async_database_uri="sqlite+aiosqlite:///./examples-llamaindex.db",
    memory_blocks=[AnonaMemoryBlock(bridge=bridge)],
)

agent = FunctionAgent(
    llm=OpenAI(model="gpt-4o-mini"),
    system_prompt="You are a concise assistant.",
)


async def main() -> None:
    for turn in [
        "I'm allergic to shellfish, remember that.",
        "Suggest somewhere to eat tonight.",
    ]:
        reply = await agent.run(turn, memory=memory)
        print(f"\n> {turn}\n{reply}")
        # Guaranteed capture, independent of token_limit timing.
        await bridge.remember(f"User: {turn}\nAssistant: {reply}")


if __name__ == "__main__":
    asyncio.run(main())
    bridge.close()
