# TypeScript client

`npm install @anona-labs/memory`. ESM and CJS, with types.

```ts
import { Anona, AnonaError } from "@anona-labs/memory";

const anona = new Anona({
  apiKey: process.env.ANONA_API_KEY!,
  baseUrl: "https://api.anonalabs.com", // default
  timeoutMs: 30_000,                    // per attempt, the default
  maxRetries: 2,                        // 429 and 5xx only
});
```

The class is `Anona`, not `AnonaClient`. Every method takes one options object
and returns a promise. Most accept a `signal` for cancellation.

## Memories

| Method | Options |
| --- | --- |
| `record` | `{ spaceId, content, context?, timestamp?, metadata?, tags?, userId?, agentId?, sessionId?, background? }` |
| `recordBatch` | `{ spaceId, items }` - up to 100, always queued |
| `retrieve` | `{ spaceId, query, limit?, topK?, mode?, memoryType?, userId?, agentId?, sessionId?, tags?, tagsMatch?, preferObservations?, minScore?, asOf?, queryTimestamp?, occurredAfter?, occurredBefore? }` |
| `getContext` | `retrieve` options plus `maxTokens` - one prompt-ready block |
| `reason` | `{ spaceId, query, userId?, agentId?, sessionId?, model? }` |
| `listMemories`, `getMemoryHistory`, `updateMemory`, `deleteMemory` | browse and edit |
| `getJob` | `{ spaceId, jobId }` |

`mode` is `"accurate"` (default, neurally reranked) or `"fast"` (skips the
rerank for much lower latency). `topK` is an alias for `limit` and wins when
both are set. `preferObservations` defaults true, collapsing a consolidated
memory and the raw facts behind it into one result; set it false to see the raw
evidence too.

## Everything else

`listSpaces`, `getSpace`, `createSpace`, `deleteSpace`, `uploadFiles`,
`listDocuments`, `getDocument`, `deleteDocument`, `retrieveReceipt`,
`getReceipt`, `explain`, `getGraph`, `listEntities`, `getEntity`,
`getUserProfile`, `askAboutUser`, `getUsage`, and the settings and webhook
families (`getExtractionSettings` / `setExtractionSettings` /
`resetExtractionSettings`, the same for chat settings, and
`createWebhook` / `listWebhooks` / `updateWebhook` / `deleteWebhook` /
`listWebhookDeliveries`).

Upload limits are exported as constants: `MAX_FILE_BYTES`, `MAX_FILES`,
`MAX_UPLOAD_BYTES`.

## Errors

```ts
try {
  await anona.retrieve({ spaceId: "my-space", query: "..." });
} catch (err) {
  if (err instanceof AnonaError) {
    err.statusCode;  // 404
    err.code;        // "space_not_found"
    err.requestId;
    err.retryAfter;  // seconds, when the server sent one
  }
}
```

Retries cover 429 and 5xx only and honour `Retry-After`. Any other 4xx is never
retried, because it will fail the same way again.

**Raise `timeoutMs` for `reason`.** The default is 30s per attempt and a loaded
synthesis legitimately runs longer than that, so the default cuts off a call the
server is still answering. Pass a per-call `timeoutMs`, or construct a second
client for synthesis. The Python client defaults to 120s for this reason; the
TypeScript one does not.

## Adapters

```ts
import { anonaMemory } from "@anona-labs/memory/vercel";       // Vercel AI SDK
import { anonaTools } from "@anona-labs/memory/openai-agents"; // OpenAI Agents
```

Both take a space and an optional scope, and wire recall and recording around
the model call.
