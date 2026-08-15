import { HttpClient, seg } from "./http.js";
import type {
  BatchRecordResult,
  DocumentItem,
  DocumentListPage,
  EntityDetail,
  EntityListPage,
  Graph,
  InsightsResult,
  JobStatus,
  MemoryHistory,
  MemoryItem,
  MemoryListPage,
  RecordResult,
  SearchResult,
  Space,
  UploadResult,
  UsageSnapshot,
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
   * Anchor recency scoring to this ISO 8601 instant instead of now, to
   * reproduce what would have been recalled at a past point in time.
   */
  queryTimestamp?: string;
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
        tags: options.tags,
        tags_match: options.tagsMatch,
        prefer_observations: options.preferObservations,
        min_score: options.minScore,
        query_timestamp: options.queryTimestamp,
      }),
    });
    return response.results ?? [];
  }

  /** Ask a question across a space and get a synthesised answer. */
  async reason(options: {
    spaceId: string;
    query: string;
    signal?: AbortSignal;
  }): Promise<InsightsResult> {
    return this.http.request<InsightsResult>({
      method: "POST",
      path: "/v1/reason",
      signal: options.signal,
      body: { space_id: options.spaceId, query: options.query },
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

  /** Page through the memories stored in a space. */
  async listMemories(options: {
    spaceId: string;
    limit?: number;
    offset?: number;
    signal?: AbortSignal;
  }): Promise<MemoryListPage> {
    return this.http.request<MemoryListPage>({
      method: "GET",
      path: `/v1/spaces/${seg(options.spaceId)}/memories`,
      query: { limit: options.limit, offset: options.offset },
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
   * Supports PDF, DOCX, PPTX, XLSX, images (OCR), HTML, TXT/MD, CSV and audio
   * (transcription). Ingestion is asynchronous — poll each returned job id with
   * `getJob`.
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
}
