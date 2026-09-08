# When a memory call fails

Errors come back as `{"error": {"code": "...", "message": "..."}}`. The rule
above everything else: **the task continues without memory.** Mention it once,
at the end, in one line.

| Code / status | What it means | What to do |
| --- | --- | --- |
| `space_not_found` (404) | Nothing has ever been written to this space. | Treat as zero results. A `record` will create it. |
| `no_api_key` (403) | The organization has no active API key. Signing in with OAuth authenticates you, but the service still reaches memory through one of the organization's keys. | Tell the user once to create a key in the dashboard under API keys. It only has to exist; it does not have to go in the config. |
| `invalid_api_key` / `key_expired` (401) | The key is not one Anona issued, was revoked, or has passed its expiry. Expiry cannot be extended. | Stop using memory this session. Say so once. |
| `credits_exhausted` (429) | The credit allowance for the period is spent. Backoff does not help. | Stop calling. Say so once. Do not retry. |
| `rate_limited` (429) | The per-minute ceiling. It clears on its own. | Honour `Retry-After` once, then skip memory for this task. |
| `space_ambiguous` (409) | The space name exists both as yours and as one shared with you. | Use the qualified form the error names, `owner:space`. |
| `space_read_only` (403) | You have read access to this shared space, not write. | Recall still works. Skip the record. |
| `reserved_tag` (422) | A tag starts with `anona:`, which is reserved. | Rename the tag. |
| `space_access_denied` (403) | You are not a member of this space. | Do not retry. Use a space you can see; `list_spaces` shows them. |
| `service_unavailable` (503) | Temporary degradation. | One retry at most, then continue without memory. |
| Timeout | `reason` in particular can be slow on a large space. | Fall back to `retrieve`, which is much faster. |

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
