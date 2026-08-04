/**
 * Memory as tools for the OpenAI Agents SDK.
 *
 * Tools are emitted as plain JSON-Schema definitions, which the Agents SDK
 * accepts directly. That keeps this adapter free of both `zod` and
 * `@openai/agents` at runtime, so the package keeps its zero-dependency
 * guarantee and does not version-couple to a fast-moving peer.
 */
import type { Anona } from "../client.js";

export interface AgentTool {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  strict: boolean;
  execute(args: Record<string, unknown>): Promise<string>;
}

export interface AnonaToolsConfig {
  client: Anona;
  spaceId: string;
  /** How many memories `recall` returns. Default 8. */
  limit?: number;
}

export function anonaTools(config: AnonaToolsConfig): AgentTool[] {
  const limit = config.limit ?? 8;

  return [
    {
      name: "remember",
      description:
        "Store a fact worth remembering about the user or the conversation, so it is " +
        "available in future sessions.",
      strict: true,
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          content: { type: "string", description: "The fact to remember, in plain language." },
        },
        required: ["content"],
      },
      async execute(args) {
        try {
          await config.client.record({
            spaceId: config.spaceId,
            content: String(args.content ?? ""),
          });
          return "Stored.";
        } catch (error) {
          // Returning the text keeps the agent loop intact — a thrown error
          // would abort the run instead of letting the model react.
          return `Could not store that memory: ${
            error instanceof Error ? error.message : String(error)
          }`;
        }
      },
    },
    {
      name: "recall",
      description: "Search stored memories for anything relevant to a question or topic.",
      strict: true,
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: {
          query: { type: "string", description: "What to look for." },
        },
        required: ["query"],
      },
      async execute(args) {
        try {
          const memories = await config.client.retrieve({
            spaceId: config.spaceId,
            query: String(args.query ?? ""),
            limit,
          });
          if (memories.length === 0) return "No relevant memories found.";
          return memories
            .map((memory) => memory.content)
            .filter((content): content is string => Boolean(content))
            .map((content) => `- ${content}`)
            .join("\n");
        } catch (error) {
          return `Could not search memories: ${
            error instanceof Error ? error.message : String(error)
          }`;
        }
      },
    },
  ];
}
