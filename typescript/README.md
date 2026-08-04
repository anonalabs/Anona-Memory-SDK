# @anona/memory

Managed memory for AI agents. Zero dependencies, runs on Node 18+, Bun, Deno,
Cloudflare Workers and the browser.

```bash
npm i @anona/memory
```

## Quickstart

```ts
import { Anona } from "@anona/memory";

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
import { Anona } from "@anona/memory";
import { anonaMemory } from "@anona/memory/vercel";

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
import { Anona } from "@anona/memory";
import { anonaTools } from "@anona/memory/openai-agents";

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

## Errors

```ts
import { AnonaError } from "@anona/memory";

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

## API

| Method | Purpose |
| --- | --- |
| `record` | Store a memory (`background: true` to queue) |
| `recordBatch` | Up to 100 memories, always queued |
| `getJob` | Status of a queued job |
| `retrieve` | Search memories |
| `reason` | Synthesised answer across a space |
| `listSpaces` / `getSpace` / `createSpace` / `deleteSpace` | Space management |
| `listMemories` / `deleteMemory` | Memory management |
| `uploadFiles` / `listDocuments` / `getDocument` / `deleteDocument` | Documents |
| `getGraph` / `listEntities` / `getEntity` | Entity graph |
| `getUsage` | Credits and rate limit for this key |
