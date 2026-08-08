"""Google ADK agent with Anona as its memory service.

    pip install 'anona[adk]'
    export ANONA_API_KEY=anona_live_... GOOGLE_API_KEY=...
    python examples/google_adk_agent.py

Nothing in ADK is automatic in either direction, and that is ADK's design, not
this adapter's limitation:

  * reading  — attach the `load_memory` tool and the model decides when to
    search. (`preload_memory` searches before *every* model call instead —
    a one-tool turn costs two retrieves for the same query.)
  * writing  — a finished run does not store itself. `after_agent_callback`
    fires once per run, after any tool loop, which is the place to do it.
"""
import asyncio
import os

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import load_memory
from google.genai import types

from anona.integrations import MemoryBridge
from anona.integrations.google_adk import AnonaMemoryService

APP_NAME = "examples-adk"
USER_ID = "customer-42"

bridge = MemoryBridge(
    api_key=os.environ["ANONA_API_KEY"],
    # Only needed to point at a different deployment; the default is
    # https://api.anonalabs.com.
    base_url=os.environ.get("ANONA_BASE_URL", "https://api.anonalabs.com"),
    space_id=os.environ.get("ANONA_SPACE_ID", "examples-adk"),
)


async def save_to_memory(callback_context):
    await callback_context.add_session_to_memory()


agent = LlmAgent(
    name="assistant",
    model="gemini-2.0-flash",
    instruction="You are a concise assistant. Search memory before answering "
    "questions about the user.",
    tools=[load_memory],
    after_agent_callback=save_to_memory,
)

sessions = InMemorySessionService()
runner = Runner(
    agent=agent,
    app_name=APP_NAME,
    session_service=sessions,
    # ADK's own scope (user_id, app_name, session id) is forwarded per call —
    # the bridge does not need to be constructed with a user_id here.
    memory_service=AnonaMemoryService(bridge=bridge),
)


async def ask(session_id: str, text: str) -> None:
    await sessions.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
    message = types.Content(role="user", parts=[types.Part(text=text)])
    print(f"\n> {text}")
    async for event in runner.run_async(
        user_id=USER_ID, session_id=session_id, new_message=message
    ):
        if event.is_final_response() and event.content:
            print("".join(p.text or "" for p in event.content.parts))


async def main() -> None:
    # Two separate sessions on purpose: the second can only answer from what
    # the memory service stored, since it shares no conversation history.
    await ask("session-1", "I'm allergic to shellfish, remember that.")
    await ask("session-2", "What should I avoid at dinner?")


if __name__ == "__main__":
    asyncio.run(main())
    bridge.close()
