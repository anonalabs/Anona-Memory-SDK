import { Anona } from "../dist/index.mjs";

const anona = new Anona({ apiKey: process.env.ANONA_API_KEY! });

const space = await anona.createSpace({ name: `demo-${Date.now()}` });
await anona.record({ spaceId: space.space_id, content: "Alice prefers email over phone." });

const memories = await anona.retrieve({
  spaceId: space.space_id,
  query: "how should I contact Alice?",
});
console.log(memories.map((m) => m.content));

await anona.deleteSpace(space.space_id);
