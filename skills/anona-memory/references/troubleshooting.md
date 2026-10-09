# When a memory call fails

Errors come back as `{"error": {"code": "...", "message": "..."}}`. The rule
above everything else: **the task continues without memory.** Mention it once,
at the end, in one line.

| Code / status | What it means | What to do |
| --- | --- | --- |
| `space_not_found` (404) | Nothing has ever been written to this space. | Treat as zero results. A `record` will create it. |
| `no_api_key` (403) | Since 2026-10-01 a brand-new organization does NOT need a key: the gateway proves its tenancy to the memory service itself. So this now means one of two things. Either the organization revoked all of its keys, which refuses access deliberately, or something is broken on Anona's side. | Say once that memory is unavailable and continue the task without it. Do NOT tell the user to create a key as a workaround: if it is the broken case, that hides a fault Anona needs reported. Only suggest creating one if the user says they revoked their keys. |
| `invalid_api_key` / `key_expired` (401) | The key is not one Anona issued, was revoked, or has passed its expiry. Expiry cannot be extended. | Stop using memory this session. Say so once. |
| `credits_exhausted` (429) | The credit allowance for the period is spent. Backoff does not help. | Stop calling. Say so once. Do not retry. |
| `rate_limited` (429) | The per-minute ceiling. It clears on its own. | Honour `Retry-After` once, then skip memory for this task. |
| `space_ambiguous` (409) | The space name exists both as yours and as one shared with you. | Use the qualified form the error names, `owner:space`. |
| `space_read_only` (403) | You have read access to this shared space, not write. | Recall still works. Skip the record. |
| `reserved_tag` (422) | A tag starts with `anona:`, which is reserved. | Rename the tag. |
| `space_access_denied` (403) | You are not a member of this space. | Do not retry. Use a space you can see; `list_spaces` shows them. |
| `service_unavailable` (503) | Temporary degradation. | One retry at most, then continue without memory. |
| Timeout | `reason` in particular can be slow on a large space. | Fall back to `retrieve`, which is much faster. |

## The `anona` CLI specifically

| What you see | What it means | What to do |
| --- | --- | --- |
| `invalid choice: 'record'` | The installed CLI predates these commands. `pip install anona` does not upgrade. | `pip install -U anona`, or `uvx --from anona anona <command>`. `anona --help` lists what this copy has. |
| `error: externally-managed-environment` | pip refusing to write to a system Python. The default on Debian, Ubuntu and Homebrew. | `uvx --from anona anona <command>`, or `pipx install anona`. |
| `record`/`retrieve`/`reason` refuse, naming a temporary profile | You are signed in, and a login credential is scoped to MCP. | Use MCP, or an API key with the REST API. Not a fault. |
| `Not signed in.` | No credential at all. | `anona start` for a temporary profile. No account needed. |
| `The temporary profile has expired.` | Past its 72 hours. Its memories are gone. | `anona start` for a new one. Nothing to recover. |
| `Too many temporary profiles from this address today.` | A per-address daily limit. | Reuse the existing profile, or wait. Do not loop. |

## Nothing comes back and the space is not empty

- **A scoped search only sees scoped memories.** If you retrieve with a
  `user_id`, memories written without one are invisible, and the reverse holds
  too. Pick one convention per space.
- **Check the space id.** A typo is a different space, and a `record` will
  happily create it. `list_spaces` shows what actually exists.
- **The query is a search, not a filter.** Ask it the way a person would.

## Verifying the setup

```bash
curl -sS https://api.anonalabs.com/v1/spaces/ \
  -H "Authorization: Bearer $ANONA_API_KEY"
```

A JSON list means the key works. A 401 means it does not. Do this once, not on
every failure.
