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
