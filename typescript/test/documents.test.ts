import { describe, expect, it, vi } from "vitest";
import { Anona, MAX_FILE_BYTES } from "../src/index.js";

function stub(body: unknown, status = 200) {
  return vi.fn(
    async () =>
      new Response(status === 204 ? null : JSON.stringify(body), {
        status,
        headers: status === 204 ? {} : { "content-type": "application/json" },
      }),
  );
}

const small = () => ({ data: new Blob(["hello"]), filename: "a.txt" });

describe("uploadFiles", () => {
  it("sends multipart form data with one part per file", async () => {
    const fetchImpl = stub({ job_ids: ["job_1"] }, 202);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.uploadFiles({ spaceId: "s", files: [small(), small()] });

    const init = (fetchImpl as any).mock.calls[0]![1] as RequestInit;
    expect((fetchImpl as any).mock.calls[0]![0]).toBe("https://api.anonalabs.com/v1/spaces/s/documents");
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.body as FormData).getAll("files")).toHaveLength(2);
    // fetch must set the multipart boundary itself
    expect((init.headers as Record<string, string>)["content-type"]).toBeUndefined();
  });

  it("joins tags with commas and passes strategy through", async () => {
    const fetchImpl = stub({ job_ids: [] }, 202);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.uploadFiles({
      spaceId: "s",
      files: [small()],
      strategy: "rag",
      tags: ["handbook", "hr"],
    });

    const form = ((fetchImpl as any).mock.calls[0]![1] as RequestInit).body as FormData;
    expect(form.get("strategy")).toBe("rag");
    expect(form.get("tags")).toBe("handbook,hr");
  });

  it("puts the whole upload in one hierarchical scope", async () => {
    // A scoped retrieve is strict, so it never returns an unscoped write. An
    // upload that cannot carry scope is therefore invisible to every scoped
    // query — silently, because the files are stored and the job completes.
    const fetchImpl = stub({ job_ids: [] }, 202);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.uploadFiles({
      spaceId: "s",
      files: [small()],
      userId: "alice",
      agentId: "bot",
      sessionId: "sess1",
    });

    const form = ((fetchImpl as any).mock.calls[0]![1] as RequestInit).body as FormData;
    // The wire names are the API's snake_case form fields.
    expect(form.get("user_id")).toBe("alice");
    expect(form.get("agent_id")).toBe("bot");
    expect(form.get("session_id")).toBe("sess1");
  });

  it("omits scope that was not passed", async () => {
    // An unscoped upload has to stay byte-identical: an empty `user_id` part
    // is a different write from no part at all.
    const fetchImpl = stub({ job_ids: [] }, 202);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.uploadFiles({ spaceId: "s", files: [small()] });

    const form = ((fetchImpl as any).mock.calls[0]![1] as RequestInit).body as FormData;
    expect(form.has("user_id")).toBe(false);
    expect(form.has("agent_id")).toBe(false);
    expect(form.has("session_id")).toBe(false);
  });

  it("rejects a file over the per-file cap before uploading", async () => {
    const fetchImpl = stub({});
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });
    const tooBig = { data: new Uint8Array(MAX_FILE_BYTES + 1), filename: "big.bin" };

    await expect(anona.uploadFiles({ spaceId: "s", files: [tooBig] })).rejects.toThrow(
      /25 MB per-file/,
    );
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("rejects more than 20 files before uploading", async () => {
    const fetchImpl = stub({});
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });
    const files = Array.from({ length: 21 }, small);

    await expect(anona.uploadFiles({ spaceId: "s", files })).rejects.toThrow(/at most 20/);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("rejects an empty file list before uploading", async () => {
    const fetchImpl = stub({});
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await expect(anona.uploadFiles({ spaceId: "s", files: [] })).rejects.toThrow(/at least one/i);
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

describe("getDocument / deleteDocument", () => {
  it("encodes the document id", async () => {
    const fetchImpl = stub({ document_id: "file_1", source: "file", tags: [] });
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await anona.getDocument({ spaceId: "s", documentId: "a/b" });

    expect((fetchImpl as any).mock.calls[0]![0]).toBe(
      "https://api.anonalabs.com/v1/spaces/s/documents/a%2Fb",
    );
  });

  it("deletes without parsing a body", async () => {
    const fetchImpl = stub(null, 204);
    const anona = new Anona({ apiKey: "k", fetch: fetchImpl as never });

    await expect(
      anona.deleteDocument({ spaceId: "s", documentId: "d1" }),
    ).resolves.toBeUndefined();
  });
});
