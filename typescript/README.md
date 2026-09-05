# @anona-labs/memory

Managed memory for AI agents. Zero dependencies, runs on Node 18+, Bun, Deno,
Cloudflare Workers and the browser.

```bash
npm i @anona-labs/memory
```

## Quickstart

```ts
import { Anona } from "@anona-labs/memory";

const anona = new Anona({ apiKey: process.env.ANONA_API_KEY! });

await anona.createSpace({ name: "support" });
await anona.record({ spaceId: "support", content: "Alice prefers email over phone." });

const memories = await anona.retrieve({ spaceId: "support", query: "how should I contact Alice?" });
console.log(memories[0]?.content);
```

## Vercel AI SDK

```ts
import { openai } from "@ai-sdk/openai";
import { generateText, wrapLanguageModel } from "ai";
import { Anona } from "@anona-labs/memory";
import { anonaMemory } from "@anona-labs/memory/vercel";

const anona = new Anona({ apiKey: process.env.ANONA_API_KEY! });

const model = wrapLanguageModel({
  model: openai("gpt-4o"),
  middleware: anonaMemory({ client: anona, spaceId: "support" }),
});

const { text } = await generateText({ model, prompt: "How should I contact Alice?" });
```

Requires `ai` v5 or newer.

## OpenAI Agents SDK

```ts
import { Agent, run } from "@openai/agents";
import { Anona } from "@anona-labs/memory";
import { anonaTools } from "@anona-labs/memory/openai-agents";

const anona = new Anona({ apiKey: process.env.ANONA_API_KEY! });
const agent = new Agent({
  name: "support",
  tools: anonaTools({ client: anona, spaceId: "support" }),
});

await run(agent, "What do you know about Alice?");
```

## Uploading files

```ts
import { readFile } from "node:fs/promises";

await anona.uploadFiles({
  spaceId: "support",
  files: [{ data: await readFile("handbook.pdf"), filename: "handbook.pdf" }],
  tags: ["handbook"],
});
```

Limits: 25 MB per file, 50 MB per request, 20 files per request — all checked
before anything is sent. Ingestion is asynchronous; poll the returned job ids
with `getJob`.

## Correcting or retiring a memory

```ts
// Fix the content
await anona.updateMemory({ spaceId, memoryId, text: "Alice moved to Berlin in March" });

// Retire it without losing it — reversible, unlike deleteMemory
await anona.updateMemory({ spaceId, memoryId, state: "invalidated", reason: "superseded" });
await anona.updateMemory({ spaceId, memoryId, state: "active" }); // put it back

// How the system's understanding of it evolved
const { history } = await anona.getMemoryHistory({ spaceId, memoryId });
```

`getMemoryHistory` reflects supersession inside the memory layer, not your own
edits — a memory you just changed still reports an empty history. The `reason`
you pass to `updateMemory` is what gets retained for audit.

`state: "invalidated"` drops a memory out of retrieve, consolidation and
reasoning while keeping it for audit. Prefer it over `deleteMemory`, which is
permanent. Memories the system synthesised from your raw facts cannot be
edited — they are derived, so the API rejects the attempt.

## What a space knows about one end user

If you scope writes with `userId`, a space accumulates a per-user history.
These two read it back without you reconstructing it from a wide-open search:

```ts
const profile = await anona.getUserProfile({ spaceId: "support", userId: "alice_123" });
console.log(profile.memory_count, profile.last_active);

// Prompt-ready instead of a list
const { context } = await anona.getUserProfile({
  spaceId: "support",
  userId: "alice_123",
  format: "block",
  contextMaxTokens: 500,
});

// One synthesised answer, from this user's memories only
const { insights, model } = await anona.askAboutUser({
  spaceId: "support",
  userId: "alice_123",
  query: "How does she prefer to be contacted?",
});
```

- **An unknown user is not a 404.** A `userId` is a scope tag created by the
  first write naming it, not a resource you register, so there is nothing for a
  typo to fall outside of — a user nobody has recorded under comes back with
  `memory_count: 0` and an empty `memories`. An unknown *space* is still a 404.
- **`memory_count` can go down.** Consolidation folds several raw facts into
  one note and the default view counts the note, so a profile read during an
  import can go 115 → 67 → 15 while the corpus behind it grows the whole time.
  It is "how many distinct things we know about this user", not an ingestion
  counter — do not build a progress bar on it.
- **`askAboutUser` reports the model that answered** in `model`, which is not
  necessarily the one you asked for, since omitting it resolves a default.
  Reconcile the credits on the call against that field.

## Errors

```ts
import { AnonaError } from "@anona-labs/memory";

try {
  await anona.retrieve({ spaceId: "nope", query: "x" });
} catch (error) {
  if (error instanceof AnonaError) {
    console.error(error.statusCode, error.code, error.requestId, error.retryAfter);
  }
}
```

429 and 5xx are retried twice by default. 4xx is never retried, and neither is a
5xx on a write (`record`, `recordBatch`, `uploadFiles`, `createSpace`,
`createWebhook`, `reason`) — a 5xx can arrive after the work landed, so
replaying it would store the memory twice.

**A 429 waits as long as the server says to.** The rate-limit window is a whole
minute, so a client backing off on its own schedule tops out in the low seconds,
spends both retries landing on the same full bucket, and fails anyway. The
client reads `Retry-After`, falling back to the `window_seconds` the rate-limit
body carries, and waits that out (capped at 60s). `error.retryAfter` carries the
same number if you would rather schedule it yourself.

**A 503 with no `requestId` may be a Cloudflare-mangled 502 or 504.** The edge
strips the body of those two statuses, so the API rewrites them to 503 before
they leave. Report such a failure with a timestamp rather than treating it as a
malformed response.

## Staying inside the rate limit

Every metered response reports the budget it left you, so you can pace a bulk
job instead of discovering the ceiling by hitting it.

```ts
await anona.retrieve({ spaceId: "support", query: "billing" });
const { remaining, limit, windowSeconds, creditsRemaining } = anona.rateLimit;
if (remaining !== undefined && remaining < 5) {
  await new Promise((r) => setTimeout(r, (windowSeconds ?? 60) * 1000));
}
```

Fields are `undefined` until a metered call has been made. The unmetered routes
— spaces, settings, webhooks — report no budget and leave the last reading in
place rather than blanking it. `getUsage()` asks the API directly instead, which
costs a request but does not need one to have happened first.

## Notes that save debugging time

- **A space's id is its name.** There is no separate id to assign, and passing
  one is rejected. A name with spaces or slashes is legal — the client encodes
  it for you.
- **`relevance_score` can exceed 1.0.** It is a product of four factors, not a
  normalised probability. It is `null` for memories returned outside a ranked
  recall.
- **`retrieve` deduplicates by default.** Raw evidence facts are omitted when a
  consolidation already covers them. Pass `preferObservations: false` to see
  both layers.

## API

| Method | Purpose |
| --- | --- |
| `record` | Store a memory — pass `background: true` to queue it, which is ~10× faster |
| `recordBatch` | Up to 100 memories, always queued |
| `getJob` | Status of a queued job |
| `retrieve` | Search memories |
| `reason` | Synthesised answer across a space |
| `listSpaces` / `getSpace` / `createSpace` / `deleteSpace` | Space management |
| `listMemories` / `getMemoryHistory` / `updateMemory` / `deleteMemory` | Memory management |
| `uploadFiles` / `listDocuments` / `getDocument` / `deleteDocument` | Documents |
| `getUserProfile` / `askAboutUser` | What a space knows about one end user |
| `getGraph` / `listEntities` / `getEntity` | Entity graph |
| `getUsage` | Credits and rate limit for this key |
| `cancelJob` | Stop the parts of a queued job that have not started |
| `getReasonSettings` / `setReasonSettings` / `resetReasonSettings` | The model `reason` uses for a space |
| `listMemoryModels` / `createMemoryModel` / `getMemoryModel` / `updateMemoryModel` / `deleteMemoryModel` | Standing questions a space keeps an answer to |
| `refreshMemoryModel` / `clearMemoryModel` / `getMemoryModelHistory` | Re-answer, wipe, or read earlier versions |
| `getSpaceProfile` | A space's mission and disposition |
| `listCatalogModels` | The LLMs this deployment will answer with, and what each costs |
| `getChatSettings` / `getExtractionSettings` (+ set/reset) | Per-space configuration |
| `createWebhook` / `listWebhooks` / `updateWebhook` / `deleteWebhook` / `listWebhookDeliveries` | Webhooks |
| `getReceipt` / `explain` | Why a search returned what it did |
