export interface TokenUsage {
  input_tokens: number;
  output_tokens: number;
}

export interface RecordResult {
  /** Present on a synchronous write. Null when queued — poll the job instead. */
  memory_id: string | null;
  /** Present when the write was queued. */
  job_id: string | null;
  /** "stored" for a synchronous write, "processing" when queued. */
  status: string;
  usage?: TokenUsage | null;
}

export interface BatchRecordResult {
  job_id: string | null;
  /** A large batch may be split into several engine operations. */
  job_ids: string[] | null;
  status: string;
  accepted: number;
  usage?: TokenUsage | null;
}

export type JobState =
  | "pending"
  | "processing"
  | "completed"
  | "failed"
  | "cancelled"
  | "not_found";

export interface JobStatus {
  job_id: string;
  status: JobState;
  created_at: string | null;
  completed_at: string | null;
  error: string | null;
  /**
   * How many memories the job produced. Null on a job that ran before the API
   * recorded the count.
   */
  memory_count: number | null;
  /**
   * Ids of the memories the job produced, so an async or bulk write can be
   * followed up without searching for what it created.
   *
   * Null when unavailable, and it may be SHORTER than `memory_count` for a very
   * large batch — the count stays exact. Check the array before indexing it.
   */
  memory_ids: string[] | null;
}

export interface SearchResult {
  memory_id: string | null;
  content: string | null;
  /**
   * Composite relevance: cross-encoder × recency × temporal × proof-count.
   * **Can exceed 1.0** — it is not a normalised probability. Null for facts
   * returned outside a ranked recall, such as source facts.
   */
  relevance_score: number | null;
  memory_type: string | null;
  /** Entities this memory is about — the graph layer. */
  entities: string[];
  /** When the underlying event happened, as distinct from when it was recorded. */
  occurred_start: string | null;
  occurred_end: string | null;
  metadata: Record<string, unknown> | null;
  created_at: string | null;
}

export interface InsightsResult {
  job_id: string | null;
  status: string | null;
  insights: unknown;
  usage?: TokenUsage | null;
}

export interface Space {
  space_id: string;
  name: string;
  description: string | null;
  created_at: string | null;
}

export interface MemoryItem {
  id: string | null;
  text: string | null;
  context: string | null;
  date: string | null;
  type: string | null;
  entities: string | null;
  metadata: Record<string, unknown> | null;
  /** Memories this one was synthesized from. Empty on a raw fact. */
  source_ids?: string[];
  /** The scope this memory was written under, mapped back from its tags. */
  user_id?: string | null;
  agent_id?: string | null;
  session_id?: string | null;
  /**
   * Stamped by the API, and only in a space shared across organizations — in a
   * space you alone own every memory is yours, so it stays null.
   */
  member_id?: string | null;
}

export interface MemoryHistoryEntry {
  previous_content: string | null;
  changed_at: string | null;
  previous_occurred_start: string | null;
  previous_occurred_end: string | null;
}

export interface MemoryHistory {
  memory_id: string;
  /** Empty when the memory has never changed. */
  history: MemoryHistoryEntry[];
}

export interface MemoryListPage {
  items: MemoryItem[];
  total: number;
  limit: number;
  offset: number;
}

/** What a space has learned about one end user. */
export interface UserProfile {
  space_id: string;
  user_id: string;
  /**
   * How many memories carry this user's scope, under whatever `memoryType`
   * filter was sent. `0` for a user nobody has ever recorded under — which is a
   * `200`, not a 404.
   *
   * **This number can go down as well as up.** The default view collapses
   * layers: several raw facts become one synthesized note, and the note is what
   * gets counted. Read it as "how many distinct things we currently know about
   * this user", never as an ingestion counter.
   */
  memory_count: number;
  /** When this user's earliest memory was learned — record time, not the event it describes. */
  first_seen: string | null;
  /** When this user's most recent memory was learned. */
  last_active: string | null;
  memories: MemoryItem[];
  /** Present only when `format` is `"block"`. */
  context?: string | null;
  /** Present only under `format: "block"`. An approximation, not a tokenizer count. */
  token_estimate?: number | null;
}

/** A synthesized answer drawn from one end user's memories only. */
export interface AskUserResult {
  space_id: string;
  user_id: string;
  /** The answer, or null when this user has nothing to answer from. */
  insights: string | null;
  usage?: TokenUsage | null;
  /**
   * The model that actually answered, which is not necessarily the one
   * requested — omitting `model` resolves a default. Reconcile the credits on
   * this call against this field, not against what you asked for.
   */
  model?: string | null;
}

/** How a document came to exist in a space. */
export type DocumentSource = "file" | "custom" | "memory";

export interface DocumentItem {
  document_id: string;
  source: DocumentSource;
  created_at: string | null;
  updated_at: string | null;
  text_length: number | null;
  memory_count: number | null;
  tags: string[];
}

export interface DocumentListPage {
  documents: DocumentItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface UploadResult {
  /** Poll each id with `getJob` to know when the files are searchable. */
  job_ids: string[];
}

export interface GraphNode {
  id: string;
  label: string;
  mention_count: number;
}

/** An edge means two entities were mentioned together — co-occurrence, not a typed relation. */
export interface GraphEdge {
  source: string;
  target: string;
  source_label: string | null;
  target_label: string | null;
  weight: number;
  last_co_occurred: string | null;
}

export interface Graph {
  nodes: GraphNode[];
  edges: GraphEdge[];
  total_entities: number;
  total_edges: number;
}

export interface EntityItem {
  id: string;
  name: string;
  mention_count: number;
  first_seen: string | null;
  last_seen: string | null;
}

export interface EntityListPage {
  items: EntityItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface EntityObservation {
  text: string;
  mentioned_at: string | null;
}

export interface EntityDetail extends EntityItem {
  observations: EntityObservation[];
}

export interface UsageSnapshot {
  credits_remaining: number;
  credits_limit: number;
  credits_used: number;
  rate_limit_per_min: number;
}

/**
 * How a space turns recorded text into memories.
 *
 * Null means *unset* — the field follows the platform default and keeps
 * following it, which is not the same as being pinned to that default's
 * current value.
 */
export interface ExtractionSettings {
  space_id: string;
  mode: "concise" | "verbose" | "verbatim" | "custom" | null;
  guidance: string | null;
  custom_prompt: string | null;
}

/** A space's defaults for the drop-in LLM proxy endpoints. */
export interface ChatSettings {
  space_id: string;
  memory_limit: number | null;
  memory_token_budget: number | null;
  auto_record: boolean | null;
  memory: boolean | null;
}

export type WebhookEventType =
  | "memory.created"
  | "memory.consolidated"
  | "security.policy_triggered";

export interface Webhook {
  id: string;
  space_id: string | null;
  url: string;
  event_types: string[];
  enabled: boolean;
  /**
   * Returned **only** when the webhook is created — store it then. Every
   * delivery carries `X-Anona-Signature: sha256=<hex>`, the HMAC-SHA256 of the
   * raw request body keyed with this secret. Compare in constant time.
   */
  secret?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

/** One delivery attempt chain, for debugging a receiver that is not working. */
export interface WebhookDelivery {
  id: string;
  event_type: string;
  url: string;
  status: string;
  attempts: number;
  response_status: number | null;
  error: string | null;
  next_retry_at?: string | null;
  last_attempt_at?: string | null;
  created_at?: string | null;
}

export interface WebhookDeliveryPage {
  items: WebhookDelivery[];
  next_cursor: string | null;
}

/**
 * One memory the pipeline dropped, and the reason it did.
 *
 * `reason` is a stable code, not prose. Four of them describe cuts made after
 * the search returned (`dedup`, `min_score`, `limit`, `budget`); three more
 * describe cuts the search made internally (`candidate_cap`, `rerank_rank`,
 * `engine_budget`) and only appear when the call asked for
 * `receiptDetail: "full"`. The two families never overlap, so a memory is
 * listed at most once.
 */
export interface ReceiptExclusion {
  memory_id: string;
  reason: string;
  /** A short account of the cut. Never memory content. */
  detail: string;
}

/** One memory that survived every cut. Ids and scores only, never content. */
export interface ReceiptInclusion {
  memory_id: string;
  relevance_score: number | null;
  token_estimate: number;
}

/**
 * The manifest for one search: what came back, what was cut, and why.
 *
 * Carries no memory content by design, which is what makes a receipt id safe
 * to paste into a support ticket.
 */
export interface ContextReceipt {
  request_id: string;
  space_id: string;
  included: ReceiptInclusion[];
  excluded: ReceiptExclusion[];
  /**
   * `{}` when the manifest above is complete. A reason listed here hit the
   * server's per-reason cap, and the number is how many more were cut than the
   * list shows, so a truncated manifest never reads as a complete one.
   */
  excluded_truncated: Record<string, number>;
  /**
   * `{}` unless a prompt-ready block was rendered; otherwise
   * `{block_tokens, budget}`.
   */
  token_accounting: Record<string, unknown>;
  /**
   * Whether the call asked the search pipeline to account for its own cuts
   * (`receiptDetail: "full"`). When false, `excluded` covers only what happened
   * after the search returned, so its silence about a memory says nothing about
   * whether the search considered it.
   */
  engine_stages: boolean;
}

/** Where one specific memory left the pipeline, from `explain`. */
export interface MemoryExplanation {
  memory_id: string;
  /**
   * `not_retrieved` is the one to act on: no stage matched the memory at all,
   * so a bigger `limit` or a lower relevance floor will not bring it back. The
   * wording or the scope is what to check.
   */
  outcome: "included" | "excluded" | "not_retrieved";
  /** `retrieval` | `fusion` | `rerank`. Null unless `outcome` is `excluded`. */
  stage: string | null;
  detail: string | null;
  /**
   * The rank this memory reached in each kind of matching, or null for one
   * that never found it. Found by `keyword` but not `semantic` usually means
   * the query shares words with the memory but not meaning.
   */
  arms: Record<string, number | null>;
  scores: Record<string, number | null>;
}

/** `retrieve` results plus the id of the receipt for that search. */
export interface RetrieveWithReceipt {
  memories: SearchResult[];
  receipt_id: string | null;
}
