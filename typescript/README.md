# @anona-labs/memory

Managed memory for AI agents. Zero dependencies, runs on Node 18+, Bun, Deno,
Cloudflare Workers and the browser.

```bash
npm i @anona-labs/memory
```

**Documentation** — [TypeScript SDK](https://docs.anonalabs.com/sdk/javascript) ·
[Quickstart](https://docs.anonalabs.com/quickstart) ·
[Concepts](https://docs.anonalabs.com/concepts) ·
[API reference](https://docs.anonalabs.com/api-reference/overview) ·
[MCP server](https://docs.anonalabs.com/mcp-integration)

Looking for Python? `pip install anona` — see
[docs.anonalabs.com/sdk/python](https://docs.anonalabs.com/sdk/python).

## Agent Skills

Two installable [Agent Skills](https://docs.anonalabs.com/integrations/skills)
that teach a coding agent to use Anona without being told to — recall before a
task, record after it — in Claude Code, Codex, Hermes, OpenCode or Cursor.

```bash
curl -fsSL https://raw.githubusercontent.com/anonalabs/Anona-Memory-SDK/main/skills/install.sh | bash
```

Or `npx skills add anonalabs/Anona-Memory-SDK`, or the Claude Code plugin
marketplace (`/plugin marketplace add anonalabs/Anona-Memory-SDK`). Source and
options: [`skills/`](skills/).

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
- **`asOf` and `queryTimestamp` are not the same knob.** `asOf` is a hard
  cutoff on when a memory was *recorded*; `queryTimestamp` only re-ranks and
  never removes a result. Reach for `asOf` when the cutoff has to be enforced.
- **`occurredAfter` / `occurredBefore` bound a third thing again:** when the
  event *happened*. That is the one question `asOf` cannot answer, since a year
  of history imported this morning has one record time and twelve months of
  event time. Either bound alone is an open-ended window, and the test is an
  overlap, so an event straddling an edge is inside.
- **A null `occurred_start` on a result is not a missing date.** Those fields
  are filled only from a date found in the memory's own text; the `timestamp`
  you recorded it with comes back as `timestamp`, and is what the window above
  matches against.

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

### Labels: tag your memories as they are written

Filtering recall is only as good as the tags a memory carries, and by default
every one of them has to be supplied by hand on the write that created it. A
label taxonomy hands that job to extraction: name a dimension once, and every
memory is classified along it as it is written.

```ts
await anona.setExtractionSettings({
  spaceId: "engineering",
  labels: [{
    key: "name",
    type: "multi-text",
    tag: true,
    description:
      "Every name the subject of this memory is known by, including " +
      "abbreviations, acronyms and short forms. Write them lowercase, words " +
      "separated by single spaces, with no punctuation.",
  }],
});
```

A group with `tag: true` is written onto the memory as the tag
`"<key>:<value>"`, so `retrieve` can filter on it through `tagGroups`. A memory
about Kubernetes then carries `name:kubernetes`, `name:k8s` and `name:kube`, and
any of the three finds it:

```ts
await anona.retrieve({
  spaceId: "engineering",
  query: "what did we decide about k8s",
  tagGroups: [{ tags: ["name:k8s"], match: "any_strict" }],
});
```

Four group types, in two pairs. `value` and `multi-values` pick from a `values`
list you supply, one or several. `text` and `multi-text` take whatever the
memory itself supplies, one value or all of them, for a vocabulary you cannot
enumerate in advance. `multi-text` is the one identity needs: a thing rarely has
a single surface form, and `text` would keep only the first.

Tags match as exact strings, so describe the format you want in the group's
`description` and normalise your query the same way.

Some things are refused rather than stored, because each would look saved and do
nothing: an enumerated group with no `values`; a `text`/`multi-text` group *with*
`values`; two groups sharing a `key`; a key that is not already in tag form
(lowercase letters, digits, `-`, `_`); and `freeFormEntities: false` with no
labels, which would keep no entities at all rather than only labelled ones.
Twelve groups maximum, since the taxonomy rides the extraction prompt on every
write.

## Compound tag filters

`tags` is a flat list under a single match mode, so it can say "any of these" or
"all of these" and nothing else. `tagGroups` takes a boolean expression instead,
for the moment a filter has two clauses that combine differently, or a clause
that excludes.

```ts
await anona.retrieve({
  spaceId: "support",
  query: "what is left to do",
  tagGroups: [
    { or: [{ tags: ["project:alpha"] }, { tags: ["project:beta"] }] },
    { not: { tags: ["status:archived"] } },
  ],
});
```

Groups in the list are AND-ed. Each is either a leaf `{ tags, match }` or one of
`{ and }`, `{ or }`, `{ not }`, nested as deep as you need. `match` on a leaf
takes the same values as `tagsMatch` and defaults to `"any_strict"`.

Three things worth knowing:

- **The non-strict modes treat an untagged memory as matching**, so a leaf
  written with `"any"` or `"all"` widens an expression rather than narrowing it,
  and both are refused inside a `not` for that reason.
- **The filter is compiled into the search**, not applied to its output, so an
  excluded memory never competes for a slot.
- **It composes rather than replaces.** `tags`, `userId`, `agentId` and
  `sessionId` are all AND-ed onto your expression.

Limits: 25 leaves and 5 levels of nesting per request. `reason` accepts the same
argument.

## API

| Method | Purpose |
| --- | --- |
| `record` | Store a memory — pass `background: true` to queue it, which is ~10× faster |
| `recordBatch` | Up to 100 memories, always queued |
| `getJob` | Status of a queued job |
| `retrieve` | Search memories — `tagGroups` filters with a boolean expression, see below |
| `getContext` | The same search, returned as one prompt-ready string |
| `reason` | Synthesised answer across a space |
| `listSpaces` / `getSpace` / `createSpace` / `deleteSpace` | Space management |
| `listMemories` / `getMemoryHistory` / `updateMemory` / `deleteMemory` | Memory management |
| `uploadFiles` / `listDocuments` / `getDocument` / `deleteDocument` | Documents |
| `getUserProfile` / `askAboutUser` | What a space knows about one end user |
| `getGraph` / `listEntities` / `getEntity` | Entity graph |
| `getExtractionSettings` / `setExtractionSettings` / `resetExtractionSettings` | Steer what a write keeps — see below |
| `getChatSettings` / `setChatSettings` / `resetChatSettings` | Per-space defaults for the drop-in proxy endpoints |
| `createWebhook` / `listWebhooks` / `updateWebhook` / `deleteWebhook` | Webhook management |
| `listWebhookDeliveries` | Recent delivery attempts, for debugging a receiver |
| `getUsage` | Credits and rate limit for this key |
| `cancelJob` | Stop the parts of a queued job that have not started |
| `getReasonSettings` / `setReasonSettings` / `resetReasonSettings` | The model `reason` uses for a space |
| `listMemoryModels` / `createMemoryModel` / `getMemoryModel` / `updateMemoryModel` / `deleteMemoryModel` | Standing questions a space keeps an answer to |
| `refreshMemoryModel` / `clearMemoryModel` / `getMemoryModelHistory` | Re-answer, wipe, or read earlier versions |
| `getSpaceProfile` | A space's mission and disposition |
| `listCatalogModels` | The LLMs this deployment will answer with, and what each costs |
| `retrieveReceipt` / `getReceipt` / `explain` | A search plus the receipt id for it, and why it returned what it did |

## Documentation

Full guides and the complete REST reference live at
[docs.anonalabs.com](https://docs.anonalabs.com):

| Page | What it covers |
| --- | --- |
| [TypeScript SDK](https://docs.anonalabs.com/sdk/javascript) | Every method on the `Anona` client |
| [Quickstart](https://docs.anonalabs.com/quickstart) | First key to first memory |
| [Concepts](https://docs.anonalabs.com/concepts) | Spaces, scoping, observations, credits |
| [API reference](https://docs.anonalabs.com/api-reference/overview) | The REST API this client wraps |
| [MCP server](https://docs.anonalabs.com/mcp-integration) | Local stdio and remote transports |

## License

MIT
