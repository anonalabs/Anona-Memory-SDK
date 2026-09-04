/**
 * Why a search did not return the memory you expected.
 *
 * `retrieve` gives you a ranked list. It cannot tell you what it nearly
 * returned, or what it never even considered. `retrieveReceipt` returns the
 * same results plus a receipt id, and `explain` accounts for one specific
 * memory against that search.
 *
 * The outcome worth acting on is `not_retrieved`: nothing matched the memory
 * at all, so a bigger limit or a lower score floor will not bring it back.
 */
import { Anona } from "../dist/index.mjs";

const anona = new Anona({ apiKey: process.env.ANONA_API_KEY! });

const space = await anona.createSpace({ name: `receipts-${Date.now()}` });
await anona.record({
  spaceId: space.space_id,
  content: "Priya owns the billing service and is on call for it.",
});
await anona.record({
  spaceId: space.space_id,
  content: "The office coffee machine is a Rancilio Silvia.",
});

// `receiptDetail: "full"` also asks the search to account for its own cuts.
// Without it the receipt only describes what happened after the search
// returned, which cannot explain a memory the search never matched.
const res = await anona.retrieveReceipt({
  spaceId: space.space_id,
  query: "who is on call for billing?",
  limit: 1,
  receiptDetail: "full",
});
console.log(res.memories.map((m) => m.content));

if (res.receipt_id) {
  const receipt = await anona.getReceipt(res.receipt_id);
  for (const cut of receipt.excluded) {
    console.log(`cut: ${cut.reason} — ${cut.detail}`);
  }

  // Ask about a memory that did not come back.
  const returned = new Set(res.memories.map((m) => m.memory_id));
  const missing = receipt.excluded
    .map((e) => e.memory_id)
    .filter((id) => !returned.has(id));

  for (const memoryId of missing) {
    const verdict = await anona.explain(res.receipt_id, memoryId);
    console.log(memoryId, verdict.outcome, verdict.stage ?? "", verdict.detail ?? "");
    if (verdict.outcome === "not_retrieved") {
      console.log("  nothing matched it — this is the query, not a threshold");
    }
  }
}

await anona.deleteSpace(space.space_id);
