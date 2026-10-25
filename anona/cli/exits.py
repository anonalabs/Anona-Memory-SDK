"""Every exit code `anona` uses, in one place.

Agents script on exit codes, so each code names one kind of outcome and means
the same thing on every verb:

    0  OK            the command did what it was asked
    1  FAILED        the request was made and refused for a reason none of the
                     codes below names (a 409 or 422, say), or a job it waited
                     on failed; also the catch-all for the account commands
                     (start, login, logout, status)
    2  USAGE         the command was wrong; nothing was sent
    3  NOTHING_STORED a write was accepted but extracted no memory (record)
    4  AUTH          no credential, a credential refused a host, 401, or 403
    5  NOT_FOUND     404: the space, memory, job or setting does not exist
    6  RATE_LIMITED  429 after the retries ran out, or out of credits (402/429)
    7  UNAVAILABLE   a 5xx, a timeout, or the server could not be reached
    8  STILL_RUNNING --wait gave up while the job was still running

The table is also printed by `anona guide`, which reads it from EXIT_CODES
below, so the two cannot drift.
"""

from __future__ import annotations

OK = 0
FAILED = 1
USAGE = 2
NOTHING_STORED = 3
AUTH = 4
NOT_FOUND = 5
RATE_LIMITED = 6
UNAVAILABLE = 7
STILL_RUNNING = 8

EXIT_CODES: tuple[tuple[int, str, str], ...] = (
    (OK, "ok", "the command did what it was asked"),
    (FAILED, "failed", "the server refused the request for another reason (e.g. 409, 422), or a waited-on job failed"),
    (USAGE, "usage", "the command line was wrong; nothing was sent"),
    (NOTHING_STORED, "nothing_stored", "record: accepted, but no memory was extracted"),
    (AUTH, "auth", "no credential, a refused host, 401 or 403"),
    (NOT_FOUND, "not_found", "404: the space, memory, job or setting does not exist"),
    (RATE_LIMITED, "rate_limited", "429 after retries, or out of credits"),
    (UNAVAILABLE, "unavailable", "5xx, timeout, or the server could not be reached"),
    (STILL_RUNNING, "still_running", "--wait gave up while the job was still running"),
)


def for_status(status: int, *, transport: bool = False) -> int:
    """The exit code for a failed API call with this HTTP status.

    `transport` is a failure that never got an HTTP answer at all (the SDK
    gives those an informational 408/503): always UNAVAILABLE.
    """
    if transport:
        return UNAVAILABLE
    if status in (401, 403):
        return AUTH
    if status == 404:
        return NOT_FOUND
    if status in (402, 429):
        return RATE_LIMITED
    if status == 408 or status >= 500:
        return UNAVAILABLE
    return FAILED
