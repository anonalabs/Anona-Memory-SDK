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

try {
  const space = await anona.createSpace({ name: spaceName, description: "SDK smoke test" });
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

  const graph = await anona.getGraph({ spaceId: space.space_id });
  step(`graph: ${graph.total_entities} entities, ${graph.total_edges} edges`);

  const usage = await anona.getUsage();
  step(`usage: ${usage.credits_remaining}/${usage.credits_limit} credits left`);

  await anona.deleteSpace(space.space_id);
  step("space deleted");
} finally {
  await fetch(`${BASE}/v1/api-keys/${key.id}`, {
    method: "DELETE",
    headers: { authorization: `Bearer ${access_token}` },
  });
  step("api key revoked");
}

console.log("\nSmoke test passed.");
