/**
 * End-to-end verification against a live deployment.
 *
 * Logs in, mints a temporary API key, exercises the whole client, then revokes
 * the key and deletes the space it created.
 *
 * Credentials come from the environment and are never written to a file:
 *   ANONA_SMOKE_EMAIL=... ANONA_SMOKE_PASSWORD=... npm run smoke
 */
import { Anona } from "../dist/index.mjs";

const BASE = process.env.ANONA_BASE_URL ?? "https://api.anonalabs.com";
const email = process.env.ANONA_SMOKE_EMAIL;
const password = process.env.ANONA_SMOKE_PASSWORD;

if (!email || !password) {
  console.error("Set ANONA_SMOKE_EMAIL and ANONA_SMOKE_PASSWORD before running the smoke test.");
  process.exit(1);
}

async function json<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, init);
  if (!response.ok) {
    throw new Error(`${init.method ?? "GET"} ${path} → ${response.status}: ${await response.text()}`);
  }
  return (await response.json()) as T;
}

const step = (name: string) => console.log(`✓ ${name}`);

const { access_token } = await json<{ access_token: string }>("/auth/login", {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ email, password }),
});
step("login");

const key = await json<{ id: string; api_key: string }>("/v1/api-keys", {
  method: "POST",
  headers: { "content-type": "application/json", authorization: `Bearer ${access_token}` },
  body: JSON.stringify({ name: `smoke-${Date.now()}`, env: "live" }),
});
step("api key minted");

const anona = new Anona({ apiKey: key.api_key, baseUrl: BASE });
const spaceName = `smoke-${Date.now()}`;

// Declared outside the try so the finally block can delete the space even when
// an assertion fails partway through. A failed run used to leak a live space.
let createdSpaceId: string | undefined;

try {
  const space = await anona.createSpace({ name: spaceName, description: "SDK smoke test" });
  createdSpaceId = space.space_id;
  step(`space created: ${space.space_id}`);

  await anona.record({ spaceId: space.space_id, content: "Alice prefers email over phone." });
  step("record");

  const batch = await anona.recordBatch({
    spaceId: space.space_id,
    items: [{ content: "Bob works in Berlin." }, { content: "Carol joined in March." }],
  });
  step(`recordBatch accepted ${batch.accepted}`);

  const upload = await anona.uploadFiles({
    spaceId: space.space_id,
    files: [{ data: new Blob(["Dave is the on-call engineer."]), filename: "notes.txt" }],
    tags: ["smoke"],
  });
  step(`upload queued ${upload.job_ids.length} job(s)`);

  // Extraction is asynchronous; give it a moment before asserting on recall.
  await new Promise((resolve) => setTimeout(resolve, 8000));

  const memories = await anona.retrieve({ spaceId: space.space_id, query: "how to contact Alice" });
  step(`retrieve returned ${memories.length} result(s)`);
  if (memories.length === 0) throw new Error("retrieve returned nothing after ingestion");

  const insights = await anona.reason({ spaceId: space.space_id, query: "who works where?" });
  step(`reason: ${String(insights.insights).slice(0, 60)}…`);

  const listed = await anona.listMemories({ spaceId: space.space_id, limit: 5 });
  step(`listMemories total ${listed.total}`);

  // updateMemory and getMemoryHistory MUST be exercised against the real API.
  // Both were once built against routes that did not exist in production, and
  // nothing caught it: the unit tests mock fetch, so they passed against
  // imaginary endpoints. A 404/405 here is the whole point of this script.
  // Only raw facts can be curated. `listMemories` also returns observations —
  // the syntheses the engine derives from those facts — and the API rejects an
  // edit to one, because a synthesis must not drift from its evidence. Picking
  // the first item blind fails with a 400 roughly half the time.
  const target = listed.items.find((m) => m.id && m.type !== "note");
  if (!target?.id) {
    throw new Error(
      `listMemories returned no curatable memory (types: ${listed.items
        .map((m) => m.type)
        .join(", ")})`,
    );
  }

  const updated = await anona.updateMemory({
    spaceId: space.space_id,
    memoryId: target.id,
    context: "verified by the SDK smoke test",
    reason: "smoke test",
  });
  step(`updateMemory edited ${updated.id ?? target.id}`);

  await anona.updateMemory({
    spaceId: space.space_id,
    memoryId: target.id,
    state: "invalidated",
    reason: "smoke test: checking the reversible retire path",
  });
  await anona.updateMemory({
    spaceId: space.space_id,
    memoryId: target.id,
    state: "active",
    reason: "smoke test: restoring",
  });
  step("updateMemory invalidate + restore round-tripped");

  const history = await anona.getMemoryHistory({
    spaceId: space.space_id,
    memoryId: target.id,
  });
  step(`getMemoryHistory returned ${history.history.length} revision(s)`);
  // Deliberately NOT asserting a non-empty history: verified against production
  // 2026-08-05 that editing a memory — including its `text` — leaves the history
  // empty, so change-tracking does not reflect curation edits. What this call
  // must prove is that the route exists and answers in the documented shape;
  // that is the regression this guards against, since the method was once
  // shipped against a route that did not exist.
  if (history.memory_id !== target.id || !Array.isArray(history.history)) {
    throw new Error(
      `getMemoryHistory returned an unexpected shape: ${JSON.stringify(history)}`,
    );
  }

  const graph = await anona.getGraph({ spaceId: space.space_id });
  step(`graph: ${graph.total_entities} entities, ${graph.total_edges} edges`);

  const usage = await anona.getUsage();
  step(`usage: ${usage.credits_remaining}/${usage.credits_limit} credits left`);

} finally {
  if (createdSpaceId) {
    try {
      await anona.deleteSpace(createdSpaceId);
      step("space deleted");
    } catch (error) {
      console.error(`! could not delete space ${createdSpaceId}:`, error);
    }
  }

  await fetch(`${BASE}/v1/api-keys/${key.id}`, {
    method: "DELETE",
    headers: { authorization: `Bearer ${access_token}` },
  });
  step("api key revoked");
}

console.log("\nSmoke test passed.");
