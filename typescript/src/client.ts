import { HttpClient, seg } from "./http.js";
import type { RateLimitSnapshot } from "./http.js";
import type {
  AskUserResult,
  BatchRecordResult,
  ChatSettings,
  DocumentItem,
  DocumentListPage,
  EntityDetail,
  EntityListPage,
  ExtractionSettings,
  Graph,
  InsightsResult,
  JobStatus,
  JobCancelResult,
  CatalogModelList,
  MemoryModel,
  MemoryModelHistory,
  MemoryModelJob,
  MemoryModelList,
  MemoryModelTrigger,
  ReasonSettings,
  SpaceProfile,
  ContextReceipt,
  MemoryExplanation,
  MemoryHistory,
  MemoryItem,
  MemoryListPage,
  RecordResult,
  RetrieveWithReceipt,
  SearchResult,
  Space,
  UploadResult,
  UsageSnapshot,
  UserProfile,
  Webhook,
  WebhookDeliveryPage,
  WebhookEventType,
} from "./types.js";

/** Mirrors the API's per-file cap, so an oversized file fails before upload. */
export const MAX_FILE_BYTES = 25 * 1024 * 1024;
/** Mirrors the API's total-per-request cap. */
export const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;
/** Mirrors the API's per-request file count cap. */
export const MAX_FILES = 20;

export interface UploadFile {
  /** File bytes. A Node Buffer is a Uint8Array, so it works unchanged. */
  data: Blob | ArrayBuffer | Uint8Array;
  filename: string;
}

export interface UploadOptions {
  spaceId: string;
  /** 1–20 files, 25 MB each, 50 MB total. */
  files: UploadFile[];
  /**
   * Retain strategy. Defaults server-side to the space's file-RAG strategy,
   * which stores raw chunks rather than running fact extraction, so a document
   * does not pollute conversational memory.
   */
  strategy?: string;
  /** Applied to every file in this upload; `retrieve` can scope to them later. */
  tags?: string[];
  signal?: AbortSignal;
}

function toBlob(data: UploadFile["data"]): Blob {
  if (data instanceof Blob) return data;
  // A Uint8Array view may be a window onto a larger buffer, so slice to its
  // own bytes rather than sending the whole underlying ArrayBuffer.
  if (data instanceof Uint8Array) {
    return new Blob([data.slice()]);
  }
  return new Blob([data]);
}

function byteLength(data: UploadFile["data"]): number {
  if (data instanceof Blob) return data.size;
  if (data instanceof Uint8Array) return data.byteLength;
  return data.byteLength;
}

export interface AnonaOptions {
  /** An API key from the dashboard: `anona_live_…` or `anona_test_…`. */
  apiKey: string;
  /**
   * Defaults to `https://api.anonalabs.com`, which routes straight to the API.
   * `https://memory.anonalabs.com` also works and keeps working indefinitely.
   */
  baseUrl?: string;
  /** Per-attempt timeout. Default 30s. */
  timeoutMs?: number;
  /** Retries for 429/5xx only. Default 2. */
  maxRetries?: number;
  /** Injectable fetch, for tests or a custom transport. */
  fetch?: typeof fetch;
}

export interface RecordOptions {
  spaceId: string;
  content: string;
  /** Extra framing stored alongside the content. */
  context?: string;
  /** ISO 8601 instant the content refers to. Defaults to now. */
  timestamp?: string;
  metadata?: Record<string, unknown>;
  /** Visibility-scope tags; `retrieve` can filter on these. */
  tags?: string[];
  /**
   * Hierarchical scope inside the space. A memory written under a `userId` is
   * only returned to a `retrieve` carrying the same one, so a single space can
   * serve many end users without their memories mixing.
   */
  userId?: string;
  agentId?: string;
  sessionId?: string;
  /** Queue the write and return a job id instead of blocking on extraction. */
  background?: boolean;
  signal?: AbortSignal;
}

export interface BatchItem {
  content: string;
  context?: string;
  timestamp?: string;
  metadata?: Record<string, unknown>;
  tags?: string[];
}

export interface RecordBatchOptions {
  spaceId: string;
  /** 1–100 items. */
  items: BatchItem[];
  signal?: AbortSignal;
}

/** How multiple `tags` combine when filtering. */
export type TagsMatch = "any" | "all" | "any_strict" | "all_strict" | "exact";

/** `RetrieveOptions`, plus how deep the receipt for that search should go. */
export interface RetrieveReceiptOptions extends RetrieveOptions {
  /**
   * `"basic"` (default) records the cuts made after the search returned.
   * `"full"` also records the ones the search made internally, so a memory
   * that never made it out of ranking is accounted for rather than simply
   * absent. `"full"` can cost latency on the first call for a given query,
   * because those decisions are not part of a cached answer.
   */
  receiptDetail?: "basic" | "full";
}

export interface RetrieveOptions {
  spaceId: string;
  query: string;
  /** 1–100. Default 10 server-side. */
  limit?: number;
  /** Alias for `limit`, honoured by the API. Wins when both are set. */
  topK?: number;
  /**
   * "accurate" (default) neurally reranks for best relevance. "fast" skips
   * that pass — much lower latency, somewhat lower relevance quality.
   */
  mode?: "accurate" | "fast";
  /** Filter by memory type: fact / note / experience / summary. */
  memoryType?: string[];
  /**
   * Hierarchical scope inside the space. A memory written under a `userId` is
   * only returned to a `retrieve` carrying the same one, so a single space can
   * serve many end users without their memories mixing.
   */
  userId?: string;
  agentId?: string;
  sessionId?: string;
  tags?: string[];
  tagsMatch?: TagsMatch;
  /**
   * Default true: collapse a consolidated memory and the raw facts it was
   * derived from into just the consolidation. Both layers exist by design, so
   * returning both reads as duplicates. Set false to get the raw evidence too.
   */
  preferObservations?: boolean;
  /** Drop hits scoring below this floor. Scores are roughly 0..1+. */
  minScore?: number;
  /**
   * Anchor recency scoring to this ISO 8601 instant instead of now, and
   * resolve relative dates in the query against it. Scoring only — it
   * re-ranks, it never removes a result. Use {@link asOf} for that.
   */
  queryTimestamp?: string;
  /**
   * Point-in-time recall: return only memories **recorded** at or before this
   * ISO 8601 instant, so the answer is what the space knew then rather than
   * what it knows now. Inclusive. Filters on when a memory was recorded, not
   * on when the event it describes happened.
   */
  asOf?: string;
  /**
   * Event-time window (ISO 8601): keep only memories describing something that
   * **happened** inside it. This is what {@link asOf} cannot address — history
   * imported today all shares one record time and spans years of event time.
   *
   * Either bound alone is an open-ended window, and the test is an overlap, so
   * an event straddling an edge is inside. A memory is matched on the window
   * its own text described, falling back to the `timestamp` it was recorded
   * with — which carries most of the work, since a memory whose text named no
   * date has no window of its own and is still filtered correctly.
   */
  occurredAfter?: string;
  occurredBefore?: string;
  /**
   * Return only what this member wrote; pass `"me"` for your own. Attribution
   * is stamped server-side and exists only in spaces shared across
   * organizations, so in a space you alone own this matches nothing extra.
   */
  memberId?: string;
  signal?: AbortSignal;
}

/**
 * Edit a memory, or retire it.
 *
 * `state: "invalidated"` drops a memory out of retrieve, consolidation and
 * reasoning while keeping it for audit; `"active"` puts it back. That is a
 * supersession, not a delete — reversible by design, and the reason to prefer
 * it over `deleteMemory`.
 *
 * Observations (memories the system synthesised from raw facts) cannot be
 * edited: they are derived, so the API rejects the attempt rather than letting
 * a synthesis drift away from its evidence.
 */
export interface UpdateMemoryOptions {
  spaceId: string;
  memoryId: string;
  text?: string;
  context?: string;
  occurredStart?: string;
  occurredEnd?: string;
  memoryType?: string;
  entities?: string[];
  state?: "active" | "invalidated";
  /** Free-text note kept on the memory's history, so an audit shows why. */
  reason?: string;
  signal?: AbortSignal;
}

/**
 * Drop keys whose value is `undefined`.
 *
 * The data-plane request models are `extra="forbid"` and distinguish an absent
 * field from an explicit null, so sending `{"tags": undefined}` — which
 * JSON.stringify would omit anyway — is fine, but sending `{"tags": null}`
 * is not the same thing as not sending it.
 */
function compact<T extends Record<string, unknown>>(obj: T): Partial<T> {
  return Object.fromEntries(
    Object.entries(obj).filter(([, value]) => value !== undefined),
  ) as Partial<T>;
}

export class Anona {
  protected readonly http: HttpClient;

  constructor(options: AnonaOptions) {
    if (!options?.apiKey) throw new Error("Anona: `apiKey` is required.");
    this.http = new HttpClient({
      apiKey: options.apiKey,
      baseUrl: (options.baseUrl ?? "https://api.anonalabs.com").replace(/\/+$/, ""),
      timeoutMs: options.timeoutMs ?? 30_000,
      maxRetries: options.maxRetries ?? 2,
      fetchImpl: options.fetch ?? globalThis.fetch.bind(globalThis),
    });
  }

  /**
   * The budget the API reported on the most recent call.
   *
   * Every metered response carries the rate-limit and credit headers; this is
   * where they land, so a caller can pace itself instead of discovering the
   * ceiling by hitting it. Fields are `undefined` until a metered call has
   * been made, and an unmetered one (spaces, settings, webhooks) leaves the
   * last reading in place rather than blanking it.
   */
  get rateLimit(): RateLimitSnapshot {
    return this.http.rateLimit;
  }

  /** Every space this key can reach. */
  async listSpaces(): Promise<Space[]> {
    const page = await this.http.request<{ spaces: Space[]; total: number }>({
      method: "GET",
      path: "/v1/spaces/",
    });
    return page.spaces;
  }

  /** One space by id. */
  async getSpace(spaceId: string): Promise<Space> {
    return this.http.request<Space>({ method: "GET", path: `/v1/spaces/${seg(spaceId)}` });
  }

  /**
   * Store a memory.
   *
   * `tags` attaches visibility-scope tags that `retrieve` can filter on — tag
   * by source agent for agent-to-agent workflows, for example.
   *
   * With `background: true` the write is queued and returns a `job_id` with
   * `status: "processing"` instead of a `memory_id`. Use it in latency-
   * sensitive paths so the call never blocks on fact extraction, then poll
   * `getJob`.
   */
  async record(options: RecordOptions): Promise<RecordResult> {
    const { spaceId, background, signal, ...rest } = options;
    return this.http.request<RecordResult>({
      method: "POST",
      path: "/v1/record",
      // A create: never auto-retried on a 5xx/timeout, which could store twice.
      idempotent: false,
      signal,
      body: compact({
        space_id: spaceId,
        content: rest.content,
        context: rest.context,
        timestamp: rest.timestamp,
        metadata: rest.metadata,
        tags: rest.tags,
        user_id: rest.userId,
        agent_id: rest.agentId,
        session_id: rest.sessionId,
        async: background ? true : undefined,
      }),
    });
  }

  /**
   * Bulk-ingest up to 100 memories in one call. Always queued — poll the
   * returned job id with `getJob`.
   */
  async recordBatch(options: RecordBatchOptions): Promise<BatchRecordResult> {
    if (options.items.length === 0) {
      throw new Error("Anona: recordBatch needs at least one item.");
    }
    if (options.items.length > 100) {
      throw new Error(
        `Anona: recordBatch accepts at most 100 items, got ${options.items.length}.`,
      );
    }
    return this.http.request<BatchRecordResult>({
      method: "POST",
      path: "/v1/record/batch",
      // A create: never auto-retried on a 5xx/timeout, which could queue twice.
      idempotent: false,
      signal: options.signal,
      body: {
        space_id: options.spaceId,
        items: options.items.map((item) => compact({ ...item })),
      },
    });
  }

  /**
   * Status of a queued ingestion job, from `record({ background: true })`,
   * `recordBatch`, or `uploadFiles`. Free — does not consume credits.
   */
  async getJob(options: { spaceId: string; jobId: string; signal?: AbortSignal }): Promise<JobStatus> {
    return this.http.request<JobStatus>({
      method: "GET",
      path: `/v1/spaces/${seg(options.spaceId)}/jobs/${seg(options.jobId)}`,
      signal: options.signal,
    });
  }

  /**
   * Cancel a queued ingestion job.
   *
   * A job is split into parts that run independently, and only a part still
   * queued can be stopped — one a worker has already picked up runs to
   * completion. The result says which, so `cancelled: 3, running: 1` means
   * work is still in flight, not that the cancel failed.
   */
  async cancelJob(options: {
    spaceId: string;
    jobId: string;
    signal?: AbortSignal;
  }): Promise<JobCancelResult> {
    return this.http.request<JobCancelResult>({
      method: "DELETE",
      path: `/v1/spaces/${seg(options.spaceId)}/jobs/${seg(options.jobId)}`,
      signal: options.signal,
    });
  }

  /**
   * The relevant memories as one prompt-ready string.
   *
   * Same search as `retrieve`, returned already formatted so it can go straight
   * into a system prompt — no join to write, and the token budget is handled
   * server-side rather than by a loop that does not have one. Returns `""` when
   * nothing matched.
   */
  async getContext(
    options: RetrieveOptions & {
      maxTokens?: number;
      /**
       * `"stable"` orders the block by when each memory was recorded, so the
       * part of the prompt this SDK wrote stays byte-identical between turns
       * and a provider's prompt cache can hit on it. The discount matches an
       * unchanged *prefix*, so it ends at the first byte that differs from
       * last turn — which is why this mode also bullets instead of numbering
       * and drops the relevance scores. `"relevance"` (default) puts the best
       * match first.
       */
      blockOrder?: "relevance" | "stable";
    },
  ): Promise<string> {
    const response = await this.http.request<{ context?: string }>({
      method: "POST",
      path: "/v1/retrieve",
      signal: options.signal,
      // Same search as `retrieve`, only rendered as a block — so it takes the
      // same filters. Sending only space_id/query/limit dropped tags, scope,
      // mode and the point-in-time cutoff, building the block from the whole
      // unfiltered space while the types promised those knobs applied.
      body: compact({
        space_id: options.spaceId,
        query: options.query,
        limit: options.limit,
        top_k: options.topK,
        mode: options.mode,
        memory_type: options.memoryType,
        user_id: options.userId,
        agent_id: options.agentId,
        session_id: options.sessionId,
        tags: options.tags,
        tags_match: options.tagsMatch,
        prefer_observations: options.preferObservations,
        min_score: options.minScore,
        query_timestamp: options.queryTimestamp,
        as_of: options.asOf,
        occurred_after: options.occurredAfter,
        occurred_before: options.occurredBefore,
        member_id: options.memberId,
        format: "block",
        context_max_tokens: options.maxTokens,
        block_order: options.blockOrder,
      }),
    });
    return response.context ?? "";
  }

  /** Search memories in a space. */
  async retrieve(options: RetrieveOptions): Promise<SearchResult[]> {
    const response = await this.http.request<{ results?: SearchResult[] }>({
      method: "POST",
      path: "/v1/retrieve",
      signal: options.signal,
      body: compact({
        space_id: options.spaceId,
        query: options.query,
        limit: options.limit,
        top_k: options.topK,
        mode: options.mode,
        memory_type: options.memoryType,
        user_id: options.userId,
        agent_id: options.agentId,
        session_id: options.sessionId,
        tags: options.tags,
        tags_match: options.tagsMatch,
        prefer_observations: options.preferObservations,
        min_score: options.minScore,
        query_timestamp: options.queryTimestamp,
        as_of: options.asOf,
        occurred_after: options.occurredAfter,
        occurred_before: options.occurredBefore,
        member_id: options.memberId,
      }),
    });
    return response.results ?? [];
  }

  /**
   * `retrieve`, plus the id of the receipt for that search.
   *
   * Same search, same results. `retrieve` returns a bare array, which has
   * nowhere to carry the receipt id, so this returns both. Pass the id to
   * `getReceipt`, or to `explain` with a memory id to ask about one memory.
   */
  async retrieveReceipt(
    options: RetrieveReceiptOptions,
  ): Promise<RetrieveWithReceipt> {
    const response = await this.http.request<{
      results?: SearchResult[];
      receipt_id?: string;
    }>({
      method: "POST",
      path: "/v1/retrieve",
      signal: options.signal,
      body: compact({
        space_id: options.spaceId,
        query: options.query,
        limit: options.limit,
        top_k: options.topK,
        mode: options.mode,
        memory_type: options.memoryType,
        user_id: options.userId,
        agent_id: options.agentId,
        session_id: options.sessionId,
        tags: options.tags,
        tags_match: options.tagsMatch,
        prefer_observations: options.preferObservations,
        min_score: options.minScore,
        query_timestamp: options.queryTimestamp,
        as_of: options.asOf,
        occurred_after: options.occurredAfter,
        occurred_before: options.occurredBefore,
        member_id: options.memberId,
        receipt: true,
        receipt_detail:
          options.receiptDetail === "full" ? "full" : undefined,
      }),
    });
    return {
      memories: response.results ?? [],
      // Null only when the receipt could not be built or stored. A receipt is
      // a debugging aid and never load-bearing, so the search itself still
      // succeeded and the results above are complete.
      receipt_id: response.receipt_id ?? null,
    };
  }

  /**
   * The manifest for one earlier search: what came back, what was cut, why.
   *
   * `requestId` is what `retrieveReceipt` returned, or the `X-Request-ID` of
   * any earlier call, which is the same value. Every search builds a receipt
   * whether or not one was asked for, so this works on a call nobody flagged
   * in advance. Receipts expire after about an hour; a missing, expired or
   * foreign one is an ordinary 404.
   */
  async getReceipt(requestId: string, signal?: AbortSignal): Promise<ContextReceipt> {
    return this.http.request<ContextReceipt>({
      method: "GET",
      path: `/v1/receipts/${seg(requestId)}`,
      signal,
    });
  }

  /**
   * Account for one specific memory against an earlier search.
   *
   * The receipt says what was cut; this answers why *this* memory is not in
   * your results, including the case no stage mentions it at all. That comes
   * back as `outcome: "not_retrieved"` and is the useful one: nothing matched
   * it, so a bigger `limit` will not help and the wording or scope is what to
   * check.
   *
   * Replays the search pinned to the instant the original ran, so memories
   * written since do not change the answer. Free, but it runs a real search,
   * so it counts against your rate limit.
   */
  async explain(
    requestId: string,
    memoryId: string,
    signal?: AbortSignal,
  ): Promise<MemoryExplanation> {
    return this.http.request<MemoryExplanation>({
      method: "GET",
      path: `/v1/receipts/${seg(requestId)}/explain`,
      query: { memory_id: memoryId },
      signal,
    });
  }

  /**
   * Ask a question across a space and get a synthesised answer.
   *
   * Reflect is a multi-iteration agent loop — a single call routinely runs the
   * better part of two minutes. So this is `idempotent: false` (a slow reflect
   * must never be auto-replayed into two or three overlapping runs at a space
   * already under load) and carries its own ~90s per-attempt timeout to match
   * the API's reflect budget, rather than being cut off at the 30s default.
   * The two go together: the long attempt is only safe because it is never
   * retried.
   */
  async reason(options: {
    spaceId: string;
    query: string;
    signal?: AbortSignal;
  }): Promise<InsightsResult> {
    return this.http.request<InsightsResult>({
      method: "POST",
      path: "/v1/reason",
      idempotent: false,
      timeoutMs: 90_000,
      signal: options.signal,
      body: { space_id: options.spaceId, query: options.query },
    });
  }

  /**
   * Everything this space has learned about one end user.
   *
   * `userId` must be the same value your writes are scoped with — this reads
   * back the memories tagged with it, so a typo on either side looks like an
   * empty profile rather than an error.
   *
   * Pass `format: "block"` for a prompt-ready `context` string as well (with
   * `contextMaxTokens` to cap it), rendered exactly as `getContext` renders a
   * search.
   *
   * Two things worth knowing before building on the result:
   *
   * - **An unknown user is not a 404.** A `userId` is a scope tag created by
   *   the first write naming it, not a resource you register, so there is no
   *   valid set for a typo to fall outside of. A user nobody has recorded
   *   under returns `memory_count: 0` and an empty `memories`. An unknown
   *   *space* is still a 404.
   * - **`memory_count` can go down.** Consolidation folds several raw facts
   *   into one note and the default view counts the note, so a profile read
   *   during an import can go 115 → 67 → 15 while the corpus behind it grows.
   *   Do not build a progress bar on it.
   */
  async getUserProfile(options: {
    spaceId: string;
    userId: string;
    limit?: number;
    offset?: number;
    /** Restrict to one layer, e.g. `"note"` for only the synthesized view. */
    memoryType?: string;
    format?: "results" | "block";
    contextMaxTokens?: number;
    signal?: AbortSignal;
  }): Promise<UserProfile> {
    return this.http.request<UserProfile>({
      method: "GET",
      path: `/v1/spaces/${seg(options.spaceId)}/users/${seg(options.userId)}/profile`,
      // Only what the caller passed: every parameter has a server-side default,
      // and filling in today's value would pin them to a default free to move.
      query: {
        limit: options.limit,
        offset: options.offset,
        memory_type: options.memoryType,
        format: options.format,
        context_max_tokens: options.contextMaxTokens,
      },
      signal: options.signal,
    });
  }

  /**
   * Ask a question answered from one end user's memories only.
   *
   * The scope is not a filter you can relax: the answer only ever draws on
   * memories written under this `userId`.
   *
   * `model` picks the LLM that answers — the same catalog and the same
   * request → space default → platform default ladder `reason` uses. The
   * response reports the model that *actually* answered, which is what the
   * credits are charged at.
   *
   * A synthesis pass sits behind this exactly as it does behind `reason`, so
   * it carries the same treatment: never auto-replayed, and given the API's
   * own ~90s budget rather than the 30s default.
   */
  async askAboutUser(options: {
    spaceId: string;
    userId: string;
    query: string;
    model?: string;
    signal?: AbortSignal;
  }): Promise<AskUserResult> {
    return this.http.request<AskUserResult>({
      method: "POST",
      path: `/v1/spaces/${seg(options.spaceId)}/users/${seg(options.userId)}/ask`,
      idempotent: false,
      timeoutMs: 90_000,
      signal: options.signal,
      body: compact({ query: options.query, model: options.model }),
    });
  }

  /**
   * Create a memory space.
   *
   * A space's id IS its name — there is no separate id to assign, and passing
   * one is rejected. A name containing spaces produces a space id containing
   * spaces, which is legal but means every later path call must encode it (the
   * client does this for you).
   */
  async createSpace(options: {
    name: string;
    description?: string;
    signal?: AbortSignal;
  }): Promise<Space> {
    return this.http.request<Space>({
      method: "POST",
      path: "/v1/spaces/",
      // The engine's bank is name-keyed and idempotent, but creating a space
      // also inserts a membership row with no on-conflict guard — so a replay
      // after a landed-but-unacknowledged create trips its unique constraint and
      // surfaces a 500 for a space that already exists. Don't auto-retry it.
      idempotent: false,
      signal: options.signal,
      body: compact({ name: options.name, description: options.description }),
    });
  }

  /** Delete a space and every memory in it. Irreversible. */
  async deleteSpace(spaceId: string, signal?: AbortSignal): Promise<void> {
    await this.http.request<void>({
      method: "DELETE",
      path: `/v1/spaces/${seg(spaceId)}`,
      expectNoContent: true,
      signal,
    });
  }

  /**
   * Page through the memories stored in a space.
   *
   * A synthesized memory and the raw facts behind it are the same knowledge in
   * two layers, so the listing returns only the synthesis by default. Pass
   * `includeSources: true` to page through the underlying evidence as well.
   *
   * This is browsing, not searching: `retrieve` ranks by relevance to a query,
   * this walks the space in order with a `total`. `query` here is a plain
   * substring filter, not a search.
   */
  async listMemories(options: {
    spaceId: string;
    limit?: number;
    offset?: number;
    includeSources?: boolean;
    /** Plain substring filter over the memory text. Not a ranked search. */
    query?: string;
    /** One memory type: fact / note / experience / summary. */
    memoryType?: string;
    /**
     * `"active"` (default) or `"invalidated"` to list what has been superseded
     * or retired — the memories `updateMemory({ state: "invalidated" })` put
     * aside, which are otherwise invisible.
     */
    state?: "active" | "invalidated";
    /** Hierarchical scope, exactly as on `record` and `retrieve`. */
    userId?: string;
    agentId?: string;
    sessionId?: string;
    /** Only what this member wrote; `"me"` for your own. */
    memberId?: string;
    signal?: AbortSignal;
  }): Promise<MemoryListPage> {
    return this.http.request<MemoryListPage>({
      method: "GET",
      path: `/v1/spaces/${seg(options.spaceId)}/memories`,
      query: {
        limit: options.limit,
        offset: options.offset,
        prefer_observations: options.includeSources ? "false" : undefined,
        q: options.query,
        // The API's query parameter is `type`; `memoryType` is the name the
        // rest of this client uses for the same thing.
        type: options.memoryType,
        state: options.state,
        user_id: options.userId,
        agent_id: options.agentId,
        session_id: options.sessionId,
        member_id: options.memberId,
      },
      signal: options.signal,
    });
  }

  /** How a memory changed over time. Empty when it has never changed. */
  async getMemoryHistory(options: {
    spaceId: string;
    memoryId: string;
    signal?: AbortSignal;
  }): Promise<MemoryHistory> {
    return this.http.request<MemoryHistory>({
      method: "GET",
      path: `/v1/spaces/${seg(options.spaceId)}/memories/${seg(options.memoryId)}/history`,
      signal: options.signal,
    });
  }

  /** Edit a memory, or invalidate/restore it. See {@link UpdateMemoryOptions}. */
  async updateMemory(options: UpdateMemoryOptions): Promise<MemoryItem> {
    const body = compact({
      text: options.text,
      context: options.context,
      occurred_start: options.occurredStart,
      occurred_end: options.occurredEnd,
      memory_type: options.memoryType,
      entities: options.entities,
      state: options.state,
      reason: options.reason,
    });
    if (Object.keys(body).length === 0) {
      throw new Error("Anona: updateMemory needs at least one field to change.");
    }
    return this.http.request<MemoryItem>({
      method: "PATCH",
      path: `/v1/spaces/${seg(options.spaceId)}/memories/${seg(options.memoryId)}`,
      signal: options.signal,
      body,
    });
  }

  /** Delete a single memory. Irreversible — see `updateMemory` for a reversible retire. */
  async deleteMemory(options: {
    spaceId: string;
    memoryId: string;
    signal?: AbortSignal;
  }): Promise<void> {
    await this.http.request<void>({
      method: "DELETE",
      path: `/v1/spaces/${seg(options.spaceId)}/memories/${seg(options.memoryId)}`,
      expectNoContent: true,
      signal: options.signal,
    });
  }

  /** Credits and rate limit for the key in use. */
  async getUsage(signal?: AbortSignal): Promise<UsageSnapshot> {
    return this.http.request<UsageSnapshot>({
      method: "GET",
      path: "/v1/usage/me",
      signal,
    });
  }

  /**
   * Upload files into a space so retrieval can draw on their content.
   *
   * Supports PDF, DOCX, DOC, PPTX, PPT, XLSX, XLS, HTML, TXT/MD, CSV,
   * JPEG/PNG images, MP3/WAV audio and MP4/MOV/WEBM/MKV video; images, audio
   * and video are read into text.
   * Ingestion is asynchronous — poll each returned job id with `getJob`.
   */
  async uploadFiles(options: UploadOptions): Promise<UploadResult> {
    const { files } = options;
    if (files.length === 0) throw new Error("Anona: uploadFiles needs at least one file.");
    if (files.length > MAX_FILES) {
      throw new Error(`Anona: at most ${MAX_FILES} files per upload, got ${files.length}.`);
    }

    let total = 0;
    for (const file of files) {
      const size = byteLength(file.data);
      if (size > MAX_FILE_BYTES) {
        throw new Error(
          `Anona: '${file.filename}' is ${Math.floor(size / 1024 / 1024)} MB, over the ` +
            `${MAX_FILE_BYTES / 1024 / 1024} MB per-file limit.`,
        );
      }
      total += size;
    }
    if (total > MAX_UPLOAD_BYTES) {
      throw new Error(
        `Anona: upload totals ${Math.floor(total / 1024 / 1024)} MB, over the ` +
          `${MAX_UPLOAD_BYTES / 1024 / 1024} MB per-request limit.`,
      );
    }

    const form = new FormData();
    for (const file of files) form.append("files", toBlob(file.data), file.filename);
    if (options.strategy) form.append("strategy", options.strategy);
    if (options.tags?.length) form.append("tags", options.tags.join(","));

    return this.http.request<UploadResult>({
      method: "POST",
      path: `/v1/spaces/${seg(options.spaceId)}/documents`,
      // A create: never auto-retried on a 5xx/timeout, which could ingest twice.
      idempotent: false,
      form,
      signal: options.signal,
    });
  }

  /** Page through the documents in a space. */
  async listDocuments(options: {
    spaceId: string;
    limit?: number;
    offset?: number;
    signal?: AbortSignal;
  }): Promise<DocumentListPage> {
    return this.http.request<DocumentListPage>({
      method: "GET",
      path: `/v1/spaces/${seg(options.spaceId)}/documents`,
      query: { limit: options.limit, offset: options.offset },
      signal: options.signal,
    });
  }

  /** One document by id. */
  async getDocument(options: {
    spaceId: string;
    documentId: string;
    signal?: AbortSignal;
  }): Promise<DocumentItem> {
    return this.http.request<DocumentItem>({
      method: "GET",
      path: `/v1/spaces/${seg(options.spaceId)}/documents/${seg(options.documentId)}`,
      signal: options.signal,
    });
  }

  /** Delete a document and the memories extracted from it. */
  async deleteDocument(options: {
    spaceId: string;
    documentId: string;
    signal?: AbortSignal;
  }): Promise<void> {
    await this.http.request<void>({
      method: "DELETE",
      path: `/v1/spaces/${seg(options.spaceId)}/documents/${seg(options.documentId)}`,
      expectNoContent: true,
      signal: options.signal,
    });
  }

  /**
   * Entity relationship graph for a space.
   *
   * Nodes are entities; an edge means two entities were mentioned in the same
   * memory, weighted by how often. Edges are co-occurrence, not typed
   * relationships.
   */
  async getGraph(options: {
    spaceId: string;
    limit?: number;
    minCount?: number;
    signal?: AbortSignal;
  }): Promise<Graph> {
    return this.http.request<Graph>({
      method: "GET",
      path: `/v1/spaces/${seg(options.spaceId)}/graph`,
      query: { limit: options.limit, min_count: options.minCount },
      signal: options.signal,
    });
  }

  /** Entities extracted in a space, most-mentioned first. */
  async listEntities(options: {
    spaceId: string;
    limit?: number;
    offset?: number;
    signal?: AbortSignal;
  }): Promise<EntityListPage> {
    return this.http.request<EntityListPage>({
      method: "GET",
      path: `/v1/spaces/${seg(options.spaceId)}/entities`,
      query: { limit: options.limit, offset: options.offset },
      signal: options.signal,
    });
  }

  /** One entity and what has been learned about it. */
  async getEntity(options: {
    spaceId: string;
    entityId: string;
    signal?: AbortSignal;
  }): Promise<EntityDetail> {
    return this.http.request<EntityDetail>({
      method: "GET",
      path: `/v1/spaces/${seg(options.spaceId)}/entities/${seg(options.entityId)}`,
      signal: options.signal,
    });
  }

  /**
   * How this space turns recorded text into memories.
   *
   * A null field is unset: it follows the platform default and keeps following
   * it, rather than being pinned to that default's current value.
   */
  async getExtractionSettings(
    spaceId: string,
    signal?: AbortSignal,
  ): Promise<ExtractionSettings> {
    return this.http.request<ExtractionSettings>({
      method: "GET",
      path: `/v1/spaces/${seg(spaceId)}/extraction-settings`,
      signal,
    });
  }

  /**
   * Replace this space's extraction settings.
   *
   * `guidance` is added to the standard extraction rules in every mode — name
   * the terms your team uses and the fields that always matter. `customPrompt`
   * replaces those rules instead, and only applies while `mode` is `"custom"`.
   *
   * Note this replaces the record rather than patching it, so anything you
   * leave out is cleared. Settings apply to writes made after the call and
   * never re-extract stored memories.
   */
  async setExtractionSettings(options: {
    spaceId: string;
    mode?: ExtractionSettings["mode"];
    guidance?: string | null;
    customPrompt?: string | null;
    signal?: AbortSignal;
  }): Promise<ExtractionSettings> {
    return this.http.request<ExtractionSettings>({
      method: "PUT",
      path: `/v1/spaces/${seg(options.spaceId)}/extraction-settings`,
      signal: options.signal,
      // Sent in full, nulls included: an omitted key would keep its stored
      // value, which is the opposite of what a replace means.
      body: {
        mode: options.mode ?? null,
        guidance: options.guidance ?? null,
        custom_prompt: options.customPrompt ?? null,
      },
    });
  }

  /** Drop this space's extraction settings, back to the platform defaults. */
  async resetExtractionSettings(spaceId: string, signal?: AbortSignal): Promise<void> {
    await this.http.request<void>({
      method: "DELETE",
      path: `/v1/spaces/${seg(spaceId)}/extraction-settings`,
      expectNoContent: true,
      signal,
    });
  }

  /** This space's defaults for the drop-in LLM proxy endpoints. */
  async getChatSettings(spaceId: string, signal?: AbortSignal): Promise<ChatSettings> {
    return this.http.request<ChatSettings>({
      method: "GET",
      path: `/v1/spaces/${seg(spaceId)}/chat-settings`,
      signal,
    });
  }

  /**
   * Replace this space's proxy defaults.
   *
   * A request that sets the same field — in its body or an `X-Anona-*` header —
   * still wins over these. Replaces the record, so anything left out is cleared.
   */
  async setChatSettings(options: {
    spaceId: string;
    memoryLimit?: number | null;
    memoryTokenBudget?: number | null;
    autoRecord?: boolean | null;
    memory?: boolean | null;
    signal?: AbortSignal;
  }): Promise<ChatSettings> {
    return this.http.request<ChatSettings>({
      method: "PUT",
      path: `/v1/spaces/${seg(options.spaceId)}/chat-settings`,
      signal: options.signal,
      body: {
        memory_limit: options.memoryLimit ?? null,
        memory_token_budget: options.memoryTokenBudget ?? null,
        auto_record: options.autoRecord ?? null,
        memory: options.memory ?? null,
      },
    });
  }

  /** Drop this space's proxy defaults, back to the platform defaults. */
  async resetChatSettings(spaceId: string, signal?: AbortSignal): Promise<void> {
    await this.http.request<void>({
      method: "DELETE",
      path: `/v1/spaces/${seg(spaceId)}/chat-settings`,
      expectNoContent: true,
      signal,
    });
  }

  /**
   * Register an HTTPS endpoint to be called when something happens in a space.
   *
   * The result carries `secret`, and this is the only time it is returned —
   * store it. Every delivery is signed with it as
   * `X-Anona-Signature: sha256=<hex>`, the HMAC-SHA256 of the raw request body.
   */
  async createWebhook(options: {
    spaceId: string;
    url: string;
    eventTypes?: WebhookEventType[];
    enabled?: boolean;
    signal?: AbortSignal;
  }): Promise<Webhook> {
    return this.http.request<Webhook>({
      method: "POST",
      path: `/v1/spaces/${seg(options.spaceId)}/webhooks`,
      // A create that mints a fresh signing secret each time: a 5xx/timeout
      // after the server registered the webhook would, on replay, register a
      // second one whose secret the caller never sees — every event then
      // delivered twice, verifiable against only the second secret.
      idempotent: false,
      signal: options.signal,
      body: {
        url: options.url,
        event_types: options.eventTypes ?? ["memory.created"],
        enabled: options.enabled ?? true,
      },
    });
  }

  /** Every webhook registered on a space. */
  async listWebhooks(spaceId: string, signal?: AbortSignal): Promise<Webhook[]> {
    const page = await this.http.request<{ items?: Webhook[] }>({
      method: "GET",
      path: `/v1/spaces/${seg(spaceId)}/webhooks`,
      signal,
    });
    return page.items ?? [];
  }

  /**
   * Change a webhook's URL, events or enabled state.
   *
   * Unlike the settings above this is a patch: only the fields you pass change,
   * so an omitted one is left alone rather than cleared.
   */
  async updateWebhook(options: {
    spaceId: string;
    webhookId: string;
    url?: string;
    eventTypes?: WebhookEventType[];
    enabled?: boolean;
    signal?: AbortSignal;
  }): Promise<Webhook> {
    return this.http.request<Webhook>({
      method: "PATCH",
      path: `/v1/spaces/${seg(options.spaceId)}/webhooks/${seg(options.webhookId)}`,
      signal: options.signal,
      body: compact({
        url: options.url,
        event_types: options.eventTypes,
        enabled: options.enabled,
      }),
    });
  }

  /** Remove a webhook. Queued deliveries for it stop. */
  async deleteWebhook(options: {
    spaceId: string;
    webhookId: string;
    signal?: AbortSignal;
  }): Promise<void> {
    await this.http.request<void>({
      method: "DELETE",
      path: `/v1/spaces/${seg(options.spaceId)}/webhooks/${seg(options.webhookId)}`,
      expectNoContent: true,
      signal: options.signal,
    });
  }

  /**
   * Recent delivery attempts, newest first — for debugging a receiver that is
   * not working. Pass `next_cursor` back as `cursor` for the next page.
   */
  async listWebhookDeliveries(options: {
    spaceId: string;
    webhookId: string;
    limit?: number;
    cursor?: string;
    signal?: AbortSignal;
  }): Promise<WebhookDeliveryPage> {
    return this.http.request<WebhookDeliveryPage>({
      method: "GET",
      path: `/v1/spaces/${seg(options.spaceId)}/webhooks/${seg(options.webhookId)}/deliveries`,
      query: { limit: options.limit, cursor: options.cursor },
      signal: options.signal,
    });
  }

  // ── Reason settings ─────────────────────────────────────────────────────────

  /** The model this space uses for `reason`, or null for the platform default. */
  async getReasonSettings(spaceId: string, signal?: AbortSignal): Promise<ReasonSettings> {
    return this.http.request<ReasonSettings>({
      method: "GET",
      path: `/v1/spaces/${seg(spaceId)}/reason-settings`,
      signal,
    });
  }

  /**
   * Pin the model `reason` uses for this space.
   *
   * Takes a model id from `listCatalogModels`, or a tier name (`"fast"`,
   * `"balanced"`). It is stored **resolved**, so re-pointing a tier later never
   * moves a space that already chose one. `model: null` clears the override.
   *
   * A full replace, and owner-only.
   */
  async setReasonSettings(options: {
    spaceId: string;
    model: string | null;
    signal?: AbortSignal;
  }): Promise<ReasonSettings> {
    return this.http.request<ReasonSettings>({
      method: "PUT",
      path: `/v1/spaces/${seg(options.spaceId)}/reason-settings`,
      signal: options.signal,
      body: { model: options.model },
    });
  }

  /** Clear the space's reason-model override. Owner-only. */
  async resetReasonSettings(spaceId: string, signal?: AbortSignal): Promise<void> {
    await this.http.request<void>({
      method: "DELETE",
      path: `/v1/spaces/${seg(spaceId)}/reason-settings`,
      expectNoContent: true,
      signal,
    });
  }

  // ── Memory models and the space profile ─────────────────────────────────────

  /**
   * The memory models defined on a space, with their current content.
   *
   * A memory model is a standing question the space keeps an answer to,
   * refreshed as memories arrive rather than computed per call. Not to be
   * confused with `listCatalogModels`, which lists the LLMs.
   */
  async listMemoryModels(options: {
    spaceId: string;
    limit?: number;
    offset?: number;
    tags?: string[];
    signal?: AbortSignal;
  }): Promise<MemoryModelList> {
    return this.http.request<MemoryModelList>({
      method: "GET",
      path: `/v1/spaces/${seg(options.spaceId)}/models`,
      query: { limit: options.limit, offset: options.offset, tags: options.tags },
      signal: options.signal,
    });
  }

  /**
   * Define a memory model.
   *
   * Content is generated asynchronously, so the model comes back before it has
   * an answer — its `content` is null until the first refresh lands. Billed
   * flat at creation.
   */
  async createMemoryModel(options: {
    spaceId: string;
    name: string;
    query: string;
    /** Stable id — lowercase alphanumeric and hyphens. Generated if omitted. */
    modelId?: string;
    tags?: string[];
    maxTokens?: number;
    trigger?: MemoryModelTrigger;
    signal?: AbortSignal;
  }): Promise<MemoryModel> {
    return this.http.request<MemoryModel>({
      method: "POST",
      path: `/v1/spaces/${seg(options.spaceId)}/models`,
      // A create, and one that costs credits at submit time.
      idempotent: false,
      signal: options.signal,
      body: compact({
        name: options.name,
        query: options.query,
        model_id: options.modelId,
        tags: options.tags,
        max_tokens: options.maxTokens,
        trigger: options.trigger,
      }),
    });
  }

  /** One memory model, with its current content. */
  async getMemoryModel(options: {
    spaceId: string;
    modelId: string;
    signal?: AbortSignal;
  }): Promise<MemoryModel> {
    return this.http.request<MemoryModel>({
      method: "GET",
      path: `/v1/spaces/${seg(options.spaceId)}/models/${seg(options.modelId)}`,
      signal: options.signal,
    });
  }

  /**
   * Edit a memory model's definition.
   *
   * Deliberately does not re-answer it: a rewrite costs credits and an edit is
   * often a typo fix. Call `refreshMemoryModel` to regenerate the content.
   */
  async updateMemoryModel(options: {
    spaceId: string;
    modelId: string;
    name?: string;
    query?: string;
    tags?: string[];
    maxTokens?: number;
    trigger?: MemoryModelTrigger;
    signal?: AbortSignal;
  }): Promise<MemoryModel> {
    return this.http.request<MemoryModel>({
      method: "PATCH",
      path: `/v1/spaces/${seg(options.spaceId)}/models/${seg(options.modelId)}`,
      signal: options.signal,
      body: compact({
        name: options.name,
        query: options.query,
        tags: options.tags,
        max_tokens: options.maxTokens,
        trigger: options.trigger,
      }),
    });
  }

  /**
   * Delete a memory model and its content. This really deletes — to keep the
   * definition and drop only the answer, use `clearMemoryModel`.
   */
  async deleteMemoryModel(options: {
    spaceId: string;
    modelId: string;
    signal?: AbortSignal;
  }): Promise<void> {
    await this.http.request<void>({
      method: "DELETE",
      path: `/v1/spaces/${seg(options.spaceId)}/models/${seg(options.modelId)}`,
      expectNoContent: true,
      signal: options.signal,
    });
  }

  /**
   * Re-answer a memory model from the space's current memories.
   *
   * Asynchronous and billed flat — returns a `job_id` to poll with `getJob`.
   */
  async refreshMemoryModel(options: {
    spaceId: string;
    modelId: string;
    signal?: AbortSignal;
  }): Promise<MemoryModelJob> {
    return this.http.request<MemoryModelJob>({
      method: "POST",
      path: `/v1/spaces/${seg(options.spaceId)}/models/${seg(options.modelId)}/refresh`,
      // Costs credits at submit time; a replay pays twice for one answer.
      idempotent: false,
      signal: options.signal,
    });
  }

  /** Wipe a memory model's content, keeping its definition. */
  async clearMemoryModel(options: {
    spaceId: string;
    modelId: string;
    signal?: AbortSignal;
  }): Promise<MemoryModel> {
    return this.http.request<MemoryModel>({
      method: "POST",
      path: `/v1/spaces/${seg(options.spaceId)}/models/${seg(options.modelId)}/clear`,
      signal: options.signal,
    });
  }

  /** Earlier versions of a memory model's content. */
  async getMemoryModelHistory(options: {
    spaceId: string;
    modelId: string;
    signal?: AbortSignal;
  }): Promise<MemoryModelHistory> {
    return this.http.request<MemoryModelHistory>({
      method: "GET",
      path: `/v1/spaces/${seg(options.spaceId)}/models/${seg(options.modelId)}/history`,
      signal: options.signal,
    });
  }

  /** A space's profile — its mission and disposition. */
  async getSpaceProfile(spaceId: string, signal?: AbortSignal): Promise<SpaceProfile> {
    return this.http.request<SpaceProfile>({
      method: "GET",
      path: `/v1/spaces/${seg(spaceId)}/profile`,
      signal,
    });
  }

  // ── LLM catalog ─────────────────────────────────────────────────────────────

  /**
   * Every LLM this deployment will answer with, and what each costs.
   *
   * Selectable on any plan: credits are derived from real cost, so a credit
   * balance is already the spend cap and the model choice cannot move it. Pass
   * an `id` (or a short name) from here to `askAboutUser`, or pin one per space
   * with `setReasonSettings`.
   *
   * Named `catalog` to keep it apart from `listMemoryModels`, which is the
   * *memory* models on one space.
   */
  async listCatalogModels(signal?: AbortSignal): Promise<CatalogModelList> {
    return this.http.request<CatalogModelList>({
      method: "GET",
      path: "/v1/models",
      signal,
    });
  }
}
