"""What a `record` response actually says happened.

The API answers `201` for a write it accepted, and acceptance is not storage:
extraction runs over the content and often produces nothing, which is a
success with an empty result rather than an error. Roughly 44% of short,
context-free statements come back that way. A caller that reads only the
status code reports those as stored, which is silent data loss dressed as
success.

This lives on its own, outside `integrations.mcp`, for two reasons. That
module imports FastMCP, which ships in the optional `[mcp]` extra, so the CLI
cannot import from it without making an optional dependency mandatory. And
the question "did this write store anything" has exactly one right answer, so
the two surfaces that ask it must not each carry their own copy to drift.
"""

from __future__ import annotations

# What to say when the write landed and extraction found nothing. It names the
# usual cause, because the useful next action is to re-record with context
# rather than to retry the same sentence.
NOTHING_EXTRACTED = (
    "Nothing was stored in space '{space_id}': no facts could be extracted from "
    "that content. Short, context-free statements often extract to nothing — "
    "re-record it with a sentence of context (what the thing is, or why it "
    "matters), which usually fixes it."
)


def stored_ids(result: object) -> list[str]:
    """The memory ids a write created, in the order the API returned them.

    ``memory_ids`` is the full set: one item usually produces more than one
    memory (the raw fact, plus any synthesis built on it), so the singular
    ``memory_id`` could only ever name part of what happened. It is read as
    the fallback for an older response that carries only the one.

    Empty for a write that stored nothing **and** for one that is still
    queued — the two are different outcomes, so ask :func:`is_queued` before
    reading an empty list as nothing.
    """
    if not isinstance(result, dict):
        return []
    many = result.get("memory_ids")
    if isinstance(many, list):
        ids = [str(item) for item in many if item]
        if ids:
            return ids
    one = result.get("memory_id")
    return [str(one)] if one else []


def is_queued(result: object) -> bool:
    """Whether the write was accepted for background extraction.

    A queued write has not run extraction yet, so it has no ids and no outcome
    to report. Reporting it as empty would be wrong in the other direction.
    """
    if not isinstance(result, dict):
        return False
    return bool(result.get("job_id")) or result.get("status") in (
        "processing",
        "pending",
        "queued",
    )


def stored_something(result: object) -> bool:
    """Whether a record persisted a memory, read off the API response.

    A non-dict is treated as stored: this is a guard against an unexpected
    response shape, and inventing a failure the API did not report would be
    worse than the thing it protects against.
    """
    if not isinstance(result, dict):
        return True
    return bool(stored_ids(result)) or is_queued(result)
