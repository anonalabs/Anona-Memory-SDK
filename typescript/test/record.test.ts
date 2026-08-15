import { describe, expect, it, vi } from "vitest";
import { Anona } from "../src/client.js";

function stub(body: unknown, status = 200) {
  return vi.fn(
    async () =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      }),
  );
}

function bodyOf(fetchImpl: ReturnType<typeof stub>, call = 0): unknown {
  const calls = (fetchImpl as any).mock.calls as Array<unknown[]>;
  return JSON.parse((calls[call]![1] as RequestInit).body as string);
}

describe("record", () => {
  it("posts the minimal body and returns the result", async () => {
    const fetchImpl = stub({ memory_id: "mem_1", job_id: null, status: "stored" }, 201);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const result = await anona.record({ spaceId: "support", content: "Alice prefers email" });

    expect((fetchImpl as any).mock.calls[0]![0]).toBe("https://api.anonalabs.com/v1/record");
    expect(bodyOf(fetchImpl)).toEqual({
      space_id: "support",
      content: "Alice prefers email",
    });
    expect(result.memory_id).toBe("mem_1");
  });

  it("omits optional fields that were not supplied", async () => {
    const fetchImpl = stub({ memory_id: "m", job_id: null, status: "stored" }, 201);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.record({ spaceId: "s", content: "c", metadata: {} });

    // An empty metadata object is still meaningful and is sent; absent
    // fields must not appear at all, because the API forbids unknown keys
    // and treats null differently from absent.
    expect(bodyOf(fetchImpl)).toEqual({ space_id: "s", content: "c", metadata: {} });
  });

  it("maps background:true onto the API's `async` key", async () => {
    const fetchImpl = stub({ memory_id: null, job_id: "job_1", status: "processing" }, 201);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const result = await anona.record({ spaceId: "s", content: "c", background: true });

    expect(bodyOf(fetchImpl)).toEqual({ space_id: "s", content: "c", async: true });
    expect(result.job_id).toBe("job_1");
  });

  it("sends context, timestamp and tags when given", async () => {
    const fetchImpl = stub({ memory_id: "m", job_id: null, status: "stored" }, 201);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.record({
      spaceId: "s",
      content: "c",
      context: "from support chat",
      timestamp: "2026-08-01T10:00:00Z",
      tags: ["agent:triage"],
    });

    expect(bodyOf(fetchImpl)).toEqual({
      space_id: "s",
      content: "c",
      context: "from support chat",
      timestamp: "2026-08-01T10:00:00Z",
      tags: ["agent:triage"],
    });
  });
});

describe("recordBatch", () => {
  it("rejects an empty batch before making a request", async () => {
    const fetchImpl = stub({});
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await expect(anona.recordBatch({ spaceId: "s", items: [] })).rejects.toThrow(/at least one/i);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("rejects more than 100 items before making a request", async () => {
    const fetchImpl = stub({});
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });
    const items = Array.from({ length: 101 }, (_, i) => ({ content: `m${i}` }));

    await expect(anona.recordBatch({ spaceId: "s", items })).rejects.toThrow(/100/);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("posts the items and returns job ids", async () => {
    const fetchImpl = stub(
      { job_id: "job_1", job_ids: ["job_1"], status: "processing", accepted: 2 },
      202,
    );
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const result = await anona.recordBatch({
      spaceId: "s",
      items: [{ content: "a" }, { content: "b", tags: ["x"] }],
    });

    expect(bodyOf(fetchImpl)).toEqual({
      space_id: "s",
      items: [{ content: "a" }, { content: "b", tags: ["x"] }],
    });
    expect(result.accepted).toBe(2);
  });
});

describe("getJob", () => {
  it("encodes both path segments", async () => {
    const fetchImpl = stub({
      job_id: "job/1",
      status: "completed",
      created_at: null,
      completed_at: null,
      error: null,
      memory_count: 2,
      memory_ids: ["m1", "m2"],
    });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const job = await anona.getJob({ spaceId: "a/b", jobId: "job/1" });
    expect(job.memory_ids).toEqual(["m1", "m2"]);

    expect((fetchImpl as any).mock.calls[0]![0]).toBe(
      "https://api.anonalabs.com/v1/spaces/a%2Fb/jobs/job%2F1",
    );
  });
});

describe("getJob memory ids", () => {
  it("treats memory_count and memory_ids as nullable", async () => {
    // The API omits both on a job that ran before it recorded them, and
    // memory_ids can be shorter than memory_count on a very large batch. Typing
    // them as required is what made `job.memory_ids.length` throw after a clean
    // typecheck — the bug that got this surface removed the first time.
    const fetchImpl = stub({
      job_id: "job_1",
      status: "completed",
      created_at: null,
      completed_at: null,
      error: null,
      memory_count: null,
      memory_ids: null,
    });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    const job = await anona.getJob({ spaceId: "s", jobId: "job_1" });

    expect(job.memory_ids).toBeNull();
    expect(job.memory_count).toBeNull();
  });
});

describe("record retry safety", () => {
  it("does not auto-retry a failed create, so a memory is never stored twice", async () => {
    const fetchImpl = vi.fn(async () => new Response("boom", { status: 503 }));
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await expect(
      anona.record({ spaceId: "s", content: "c" }),
    ).rejects.toMatchObject({ statusCode: 503 });
    // Default maxRetries is 2; without idempotent:false this would be 3 calls.
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
});
