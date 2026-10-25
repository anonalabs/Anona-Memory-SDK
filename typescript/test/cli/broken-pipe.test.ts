/**
 * A reader that closes the pipe early -- `anona guide` piped into `head` --
 * ends the command quietly: exit 141 (what SIGPIPE reports), nothing on
 * stderr, as the pip package does. Run against the bundled bin in a child
 * process, because only a real closed pipe produces the EPIPE.
 */
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { buildSync } from "esbuild";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
let dir = "";
let bin = "";

beforeAll(() => {
  dir = mkdtempSync(join(tmpdir(), "anona-pipe-"));
  bin = join(dir, "cli.mjs");
  buildSync({
    entryPoints: [resolve(here, "../../src/cli/bin.ts")],
    bundle: true,
    platform: "node",
    format: "esm",
    outfile: bin,
    logLevel: "silent",
    define: { __ANONA_VERSION__: JSON.stringify("0.0.0-test") },
  });
});
afterAll(() => rmSync(dir, { recursive: true, force: true }));

/** Run the bin with its stdout read once and then closed, like `head -c1`. */
function runClosingStdout(args: string[]): Promise<{ code: number | null; err: string }> {
  return new Promise((done) => {
    const child = spawn(process.execPath, [bin, ...args], { stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, HOME: dir } });
    let err = "";
    child.stderr.on("data", (c) => (err += String(c)));
    // Close our end before the child has written: its first write that the
    // kernel cannot buffer, or its flush, meets a closed pipe.
    child.stdout.destroy();
    child.on("close", (code) => done({ code, err }));
  });
}

describe("a closed stdout", () => {
  for (const args of [["guide"], ["guide", "--json"], ["--help"], ["record", "--help"], ["help", "spaces", "delete"], ["completion", "fish"]]) {
    it(`anona ${args.join(" ")}: exit 141, nothing on stderr`, async () => {
      const r = await runClosingStdout(args);
      expect(r.err).toBe("");
      expect(r.code).toBe(141);
    });
  }
});
