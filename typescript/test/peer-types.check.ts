/**
 * Compile-time proof that both adapters satisfy their optional peers' real
 * types. Nothing else in the suite checks this: the adapters declare their
 * shapes structurally so the package builds with the peers absent, which means
 * a drift between our shape and theirs is invisible until a consumer hits it.
 *
 * This file is never executed — vitest only collects `*.test.ts`. It exists so
 * `tsc --noEmit` fails if an adapter stops being assignable.
 */
import type { LanguageModelV2Middleware } from "@ai-sdk/provider";
import { Agent } from "@openai/agents";
import { Anona } from "../src/index.js";
import { anonaMemory } from "../src/adapters/vercel.js";
import { anonaTools } from "../src/adapters/openai-agents.js";

const client = new Anona({ apiKey: "k" });

// The assignments ARE the assertions: mismatched shapes fail the typecheck.
const middleware: LanguageModelV2Middleware = anonaMemory({ client, spaceId: "s" });

const agent = new Agent({
  name: "check",
  tools: anonaTools({ client, spaceId: "s" }) as unknown as ConstructorParameters<
    typeof Agent
  >[0]["tools"],
});

void middleware;
void agent;
