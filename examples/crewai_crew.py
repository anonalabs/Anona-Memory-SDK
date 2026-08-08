"""CrewAI crew with Anona memory as two agent tools.

    pip install 'anona[crewai]'
    export ANONA_API_KEY=anona_live_... OPENAI_API_KEY=sk-...
    python examples/crewai_crew.py

CrewAI has no storage backend seam that ever sees the query text, so this
adapter is tools rather than a `Memory` backend. That makes recall
*discretionary*: the model decides when to call them, so the backstory has to
say when. Run it twice — the second run should recall the first run's finding.
"""
import os

from crewai import Agent, Crew, Task

from anona.integrations import MemoryBridge
from anona.integrations.crewai import AnonaStorage

bridge = MemoryBridge(
    api_key=os.environ["ANONA_API_KEY"],
    space_id=os.environ.get("ANONA_SPACE_ID", "examples-crewai"),
)
storage = AnonaStorage(bridge=bridge)

researcher = Agent(
    role="Researcher",
    goal="Answer using both new research and what the crew already knows",
    backstory=(
        "An experienced researcher who keeps notes across projects. Before "
        "starting a question, search memory for anything already known about "
        "it. After learning something worth keeping, save it for later runs."
    ),
    # The tools are named "Anona: Search memory" / "Anona: Save memory". The
    # prefix is load-bearing: Crew._merge_tools dedups by name and CrewAI's
    # own built-in memory tool wins an unprefixed collision.
    tools=storage.as_tools(),
)

crew = Crew(
    agents=[researcher],
    tasks=[
        Task(
            description=(
                "Our team standardised on Postgres 16 for new services. "
                "Record that, then tell me what we standardised on."
            ),
            expected_output="One sentence naming the database standard.",
            agent=researcher,
        )
    ],
)

if __name__ == "__main__":
    print(crew.kickoff())
    bridge.close()
