/**
 * Every exit code `anona` uses, in one place. Mirrors anona/cli/exits.py.
 *
 * Agents script on exit codes, so each code names one kind of outcome and means
 * the same thing on every verb. The table is also printed by `anona guide`,
 * which reads it from EXIT_CODES, so the two cannot drift.
 */
export const OK = 0;
export const FAILED = 1;
export const USAGE = 2;
export const NOTHING_STORED = 3;
// A 401 is AUTH, so is `session_required` (the command needs the command-line
// login and an API key cannot do it), and so is a dashboard-only action, which
// is refused without a request.
export const AUTH = 4;
export const NOT_FOUND = 5;
export const RATE_LIMITED = 6;
export const UNAVAILABLE = 7;
export const STILL_RUNNING = 8;

export const EXIT_CODES: ReadonlyArray<readonly [number, string, string]> = [
  [OK, "ok", "the command did what it was asked"],
  [FAILED, "failed", "the server refused the request for another reason (e.g. 409, 422), or a waited-on job failed"],
  [USAGE, "usage", "the command line was wrong; nothing was sent"],
  [NOTHING_STORED, "nothing_stored", "record: accepted, but no memory was extracted"],
  [AUTH, "auth", "no credential, a refused host, 401 or 403"],
  [NOT_FOUND, "not_found", "404: the space, memory, job or setting does not exist"],
  [RATE_LIMITED, "rate_limited", "429 after retries, or out of credits"],
  [UNAVAILABLE, "unavailable", "5xx, timeout, or the server could not be reached"],
  [STILL_RUNNING, "still_running", "--wait gave up while the job was still running"],
];

/**
 * What a process killed by SIGPIPE reports (128 + 13), as `git` does. Not in
 * the table: it is what a reader closing the pipe early exits with, never an
 * outcome a script branches on.
 */
export const BROKEN_PIPE_EXIT = 141;

/**
 * The exit code for a failed API call with this HTTP status. `transport` is a
 * failure that never got an HTTP answer at all: always UNAVAILABLE.
 */
export function forStatus(status: number, transport = false): number {
  if (transport) return UNAVAILABLE;
  if (status === 401 || status === 403) return AUTH;
  if (status === 404) return NOT_FOUND;
  if (status === 402 || status === 429) return RATE_LIMITED;
  if (status === 408 || status >= 500) return UNAVAILABLE;
  return FAILED;
}
