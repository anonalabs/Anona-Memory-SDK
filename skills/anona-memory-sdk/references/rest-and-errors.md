# REST surface and errors

Base URL `https://api.anonalabs.com`. `https://memory.anonalabs.com` also works
and keeps working indefinitely; existing code needs no change.

Auth is `Authorization: Bearer anona_live_...`. Every data-plane endpoint also
accepts `x-api-key`, which is how the Anthropic SDK sends credentials. The
Bearer scheme is not optional: `Authorization: anona_live_...` is rejected.

## Endpoints worth knowing

| Method and path | Does |
| --- | --- |
| `POST /v1/record` | Store one memory. `"async": true` returns a `job_id` instead of blocking. |
| `POST /v1/record/batch` | Up to 100 items, always queued. |
| `POST /v1/retrieve` | Semantic search. Answers `{"results": [...]}`. |
| `POST /v1/reason` | Synthesis over the space. Answers `{"insights": "..."}`. |
| `GET /v1/spaces/` | Spaces this key can see. |
| `POST /v1/spaces/` | Create a space. Rejects a `space_id` field: the name *is* the id. |
| `GET /v1/spaces/{id}/memories` | Browse rather than search. |
| `POST /v1/spaces/{id}/documents` | Upload files. Scope rides form fields, not JSON. |
| `GET /v1/spaces/{id}/jobs/{job_id}` | Poll a queued write. |
| `GET /v1/spaces/{id}/graph` | Entity graph. Edges are co-occurrence, not typed predicates. |
| `GET /v1/spaces/{id}/users/{user_id}/profile` | What the space knows about one end user. |
| `POST /v1/chat/completions` | Drop-in OpenAI Chat proxy, with recall and recording around the turn. |
| `POST /v1/responses` | Drop-in OpenAI Responses proxy. |
| `POST /v1/messages` | Drop-in Anthropic Messages proxy. |
| `GET /v1/models` | The LLM catalog. Not to be confused with `/v1/spaces/{id}/models`, which is memory models. |

Request bodies on the data plane reject unknown fields. A misspelled field name
is a `422 validation_error`, not a silently dropped value.

A space id containing a slash or a space needs URL encoding in the path. A
shared space whose bare name collides with one of your own is addressed
`owner:space`, with a colon.

## Error envelope

```json
{"error": {"code": "invalid_api_key", "message": "Invalid API key", "request_id": "req_abc123"}}
```

Branch on `code`, never on `message`. Quote `request_id` to support.

| Status | Code | Retry |
| --- | --- | --- |
| 401 | `missing_auth`, `invalid_api_key`, `key_expired`, `invalid_token`, `session_required` | No |
| 403 | `space_access_denied`, `space_read_only`, `space_owner_only`, `space_unavailable`, `insufficient_role`, `org_suspended` | No |
| 404 | `space_not_found` | No |
| 409 | `space_ambiguous` | No, use the qualified `owner:space` id |
| 422 | `validation_error`, `reserved_tag` | No |
| 429 | `rate_limited` | Yes, with backoff |
| 429 | `credits_exhausted` | No, backoff does not help |
| 500 | `internal_error` | Yes |
| 503 | `service_unavailable` | Yes, after a short delay |

Model-related, on `/v1/reason` and the proxy endpoints: `400 unsupported_model`
(not served here; the message lists what is), `429 model_throttled` (per-model
capacity, another model may have headroom, and Anona never silently
substitutes), `503 model_unavailable` (catalogued but not invokable, ours to
fix), `400 invalid_tunable` (an `X-Anona-*` header could not be parsed).

`session_required` is the one that reads like a bad key and is not. Exports,
space members, API keys, org, usage, billing and invitations are control-plane
endpoints authenticated by a dashboard session. A perfectly valid API key is
simply the wrong kind of credential there.

## Retrying

Retry 429, 500 and 503 with exponential backoff and jitter, honouring
`Retry-After`. Never retry any other 4xx: it will fail identically. Both SDKs
already do this; do not wrap them in a second retry loop.

A 503 with no `request_id` may be an edge-rewritten 502 or 504. Report it with a
timestamp rather than treating it as a malformed response.
