/**
 * Vercel AI SDK middleware: recall before the model call, record after it.
 *
 * Requires `ai >= 5` at runtime — this is a `LanguageModelV2Middleware`. The
 * types below are declared structurally rather than imported from `ai`, so
 * this package typechecks and builds with the peer absent.
 */
import type { Anona } from "../client.js";
import type { SearchResult } from "../types.js";

interface TextPart {
  type: "text";
  text: string;
}

interface PromptMessage {
  role: "system" | "user" | "assistant" | "tool";
  content: string | TextPart[] | unknown;
}

interface CallParams {
  prompt: PromptMessage[];
  [key: string]: unknown;
}

export interface AnonaMemoryConfig {
  client: Anona;
  spaceId: string;
  /** How many memories to inject. Default 8. */
  limit?: number;
  /** Recall mode. "fast" trades some relevance for latency. */
  mode?: "accurate" | "fast";
  /**
   * Wait for the write to land before returning. Default false: recording is
   * fire-and-forget so memory never adds latency to a response.
   */
  await?: boolean;
  /** Record the turn at all. Default true. */
  record?: boolean;
  /** Override how recalled memories are rendered into the system block. */
  format?: (memories: SearchResult[]) => string;
}

function textOf(content: PromptMessage["content"]): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .filter((part): part is TextPart => (part as TextPart)?.type === "text")
      .map((part) => part.text)
      .join("\n");
  }
  return "";
}

function lastUserText(prompt: PromptMessage[]): string {
  for (let i = prompt.length - 1; i >= 0; i--) {
    const message = prompt[i]!;
    if (message.role === "user") return textOf(message.content);
  }
  return "";
}

function defaultFormat(memories: SearchResult[]): string {
  const lines = memories
    .map((memory) => memory.content)
    .filter((content): content is string => Boolean(content))
    .map((content) => `- ${content}`);
  return `Relevant memories about this user and context:\n${lines.join("\n")}`;
}

export function anonaMemory(config: AnonaMemoryConfig) {
  const shouldRecord = config.record !== false;
  const format = config.format ?? defaultFormat;

  async function recall(params: CallParams): Promise<CallParams> {
    const query = lastUserText(params.prompt ?? []);
    if (!query) return params;

    let memories: SearchResult[];
    try {
      memories = await config.client.retrieve({
        spaceId: config.spaceId,
        query,
        limit: config.limit ?? 8,
        mode: config.mode,
      });
    } catch {
      // Memory is an enhancement, never a hard dependency of the model call.
      // A recall failure must not fail the user's request.
      return params;
    }

    if (memories.length === 0) return params;

    return {
      ...params,
      prompt: [
        { role: "system", content: [{ type: "text", text: format(memories) }] },
        ...params.prompt,
      ],
    };
  }

  async function remember(params: CallParams, answer: string): Promise<void> {
    if (!shouldRecord) return;
    const question = lastUserText(params.prompt ?? []);
    if (!question && !answer) return;

    const write = config.client
      .record({
        spaceId: config.spaceId,
        content: `User: ${question}\nAssistant: ${answer}`,
        background: true,
      })
      .catch(() => {
        // Same reasoning as recall: a failed write must not surface as a
        // failed completion.
      });

    if (config.await) await write;
  }

  return {
    async transformParams({ params }: { params: any }): Promise<any> {
      return recall(params as CallParams);
    },

    async wrapGenerate({
      doGenerate,
      params,
    }: {
      doGenerate: () => PromiseLike<any>;
      params: any;
    }) {
      const result = await doGenerate();
      const answer = Array.isArray(result.content)
        ? result.content
            .filter((part: any): part is TextPart => (part as TextPart)?.type === "text")
            .map((part: TextPart) => part.text)
            .join("")
        : "";
      await remember(params as CallParams, answer);
      return result;
    },

    async wrapStream({
      doStream,
      params,
    }: {
      doStream: () => PromiseLike<any>;
      params: any;
    }) {
      const { stream, ...rest } = await doStream();
      let answer = "";

      // Record once the stream completes, not at first token — the turn is
      // not finished until the last chunk lands.
      const transform = new TransformStream({
        transform(chunk: { type?: string; delta?: string }, controller) {
          if (chunk?.type === "text-delta" && typeof chunk.delta === "string") {
            answer += chunk.delta;
          }
          controller.enqueue(chunk);
        },
        flush() {
          return remember(params as CallParams, answer);
        },
      });

      return { stream: stream.pipeThrough(transform as never), ...rest };
    },
  };
}
