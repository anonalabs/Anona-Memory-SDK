/**
 * Requires `ai` v5 and a provider package, e.g.:
 *   npm i ai @ai-sdk/openai
 */
import { openai } from "@ai-sdk/openai";
import { generateText, wrapLanguageModel } from "ai";
import { Anona } from "../dist/index.mjs";
import { anonaMemory } from "../dist/adapters/vercel.mjs";

const anona = new Anona({ apiKey: process.env.ANONA_API_KEY! });

const model = wrapLanguageModel({
  model: openai("gpt-4o"),
  middleware: anonaMemory({ client: anona, spaceId: "support", limit: 8 }),
});

const { text } = await generateText({ model, prompt: "How should I contact Alice?" });
console.log(text);
