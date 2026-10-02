#!/bin/sh
# Runs after Claude Code compacts a conversation. The model gets no turn before
# compaction, so this is the first moment it can act on what the summary kept:
# remind it to move the durable facts into Anona before the task resumes.
# Prints only; never calls the network, so it cannot slow or fail the session.
cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"This conversation was just compacted. Per the anona-memory skill's Compaction section: before resuming the task, record the durable facts in the summary above that were not already recorded this session, one record call per fact, into this session's space as the skill's Which space section picks it. No session_id or agent_id scope: it would hide the fact from later sessions. Facts only, never the summary itself; no secrets, nothing already in the repository, nothing still a hypothesis. Record nothing if nothing durable was settled. If Anona is not configured, skip this silently."}}
JSON
