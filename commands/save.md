---
description: Save what this session settled to Anona Memory, one fact per memory
argument-hint: "[optional topic, e.g. the deploy decisions]"
---

Save this session to Anona Memory. Follow the anona-memory skill's
"Saving a session on request" section.

Topic to cover: $ARGUMENTS
(If that is empty, cover the whole session.)

Go through the whole conversation, including any compaction summary at the top,
and record every durable fact it settled that is not yet in Anona: decisions and
their reasons, fixes and their causes, constraints, preferences and outcomes.
One `record` call per fact, into this session's space, as the skill's
"Which space" section picks it. No secrets, nothing already in the repository,
nothing still a hypothesis, and nothing you already recorded this session.
Never record the transcript or a summary of it as one memory.

Finish with exactly one line: how many memories you wrote and to which space,
or "Nothing new to save." If Anona is not configured, say so in one line, with
the fix the skill gives.
