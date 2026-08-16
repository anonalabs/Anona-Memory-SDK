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

## Errors

```ts
import { AnonaError } from "@anona-labs/memory";

try {
  await anona.retrieve({ spaceId: "nope", query: "x" });
} catch (error) {
  if (error instanceof AnonaError) {
    console.error(error.statusCode, error.code, error.requestId);
  }
}
```

429 and 5xx are retried twice by default with jittered backoff. 4xx is never
retried.

**A 503 with no `requestId` may be a Cloudflare-mangled 502 or 504.** The edge
strips the body of those two statuses, so the API rewrites them to 503 before
they leave. Report such a failure with a timestamp rather than treating it as a
malformed response.

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
- **`asOf` and `queryTimestamp` are not the same knob.** `asOf` is a hard
  cutoff on when a memory was *recorded*; `queryTimestamp` only re-ranks and
  never removes a result. Reach for `asOf` when the cutoff has to be enforced.

## Scoping one space to many users

```ts
await anona.record({
  spaceId: "support",
  content: "Alice prefers email",
  userId: "alice",
});

// Only Alice's memories come back. The filter is strict, so memories stored
// without a scope are not returned to a scoped search.
await anona.retrieve({ spaceId: "support", query: "how to contact", userId: "alice" });
```

`userId`, `agentId` and `sessionId` nest in that order, and a call that passes
none of them behaves exactly as it always did.

## Prompt-ready context

```ts
const context = await anona.getContext({
  spaceId: "support",
  query: "what does Alice need",
  maxTokens: 500,
});
```

The same search as `retrieve`, already formatted, with the token budget applied
server-side rather than by a loop that does not have one. Returns `""` when
nothing matched, so it can go straight into a system prompt.

## Extraction settings

Recording a memory is not storage. Your text goes through one pass that decides
which facts are worth keeping, and only what survives is stored — so a detail
dropped there is not ranked low later, it is not there at all. These settings
point that pass at your domain.

```ts
await anona.setExtractionSettings({
  spaceId: "engineering",
  guidance:
    "Engineering log. Always capture service names, metric values with units, " +
    "and the named owner. Treat incidents as dated events. Skip standup small talk.",
});
```

`guidance` is added to the standard rules and applies in every mode — start
there. `mode` is `"concise"` (the default), `"verbose"`, `"verbatim"`, or
`"custom"`; `customPrompt` replaces the standard rules and only applies while
the mode is `"custom"`.

`setExtractionSettings` **replaces** the record, so anything you leave out is
cleared. Settings apply to writes made after the call — stored memories are
never re-extracted, so changing these never rewrites history. Unhelpful guidance
produces no error; extraction simply keeps different things.

## API

| Method | Purpose |
| --- | --- |
| `record` | Store a memory — pass `background: true` to queue it, which is ~10× faster |
| `recordBatch` | Up to 100 memories, always queued |
| `getJob` | Status of a queued job |
| `retrieve` | Search memories |
| `getContext` | The same search, returned as one prompt-ready string |
| `reason` | Synthesised answer across a space |
| `listSpaces` / `getSpace` / `createSpace` / `deleteSpace` | Space management |
| `listMemories` / `getMemoryHistory` / `updateMemory` / `deleteMemory` | Memory management |
| `uploadFiles` / `listDocuments` / `getDocument` / `deleteDocument` | Documents |
| `getGraph` / `listEntities` / `getEntity` | Entity graph |
| `getExtractionSettings` / `setExtractionSettings` / `resetExtractionSettings` | Steer what a write keeps — see below |
| `getChatSettings` / `setChatSettings` / `resetChatSettings` | Per-space defaults for the drop-in proxy endpoints |
| `createWebhook` / `listWebhooks` / `updateWebhook` / `deleteWebhook` | Webhook management |
| `listWebhookDeliveries` | Recent delivery attempts, for debugging a receiver |
| `getUsage` | Credits and rate limit for this key |
