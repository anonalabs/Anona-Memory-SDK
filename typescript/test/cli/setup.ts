/**
 * Global guard: no test may launch a browser.
 *
 * Two layers, because either alone has a hole.
 *  1. `browser.open` is replaced with a recording fake, so a test that reaches
 *     the default opener is observable and harmless.
 *  2. `child_process.spawn` is wrapped at the boundary: a spawn of any browser
 *     launcher is recorded and refused, which catches a regression to a direct
 *     spawn that bypasses layer 1. afterEach fails the test if anything was
 *     recorded. (openBrowser swallows spawn errors on purpose, so throwing
 *     alone would be silent.)
 */
import { afterEach, beforeEach, vi } from "vitest";

const LAUNCHERS = new Set(["xdg-open", "open", "rundll32", "cmd", "start", "sensible-browser", "x-www-browser"]);

export const launched: Array<{ cmd: string; args: string[] }> = [];
export const opened: string[] = [];

vi.mock("node:child_process", async (importOriginal) => {
  const real = await importOriginal<typeof import("node:child_process")>();
  return {
    ...real,
    spawn: ((cmd: string, ...rest: unknown[]) => {
      if (LAUNCHERS.has(cmd)) {
        const args = Array.isArray(rest[0]) ? (rest[0] as string[]) : [];
        launched.push({ cmd, args });
        throw new Error("a test tried to launch a browser");
      }
      return (real.spawn as (...a: unknown[]) => unknown)(cmd, ...rest);
    }) as typeof real.spawn,
  };
});

beforeEach(async () => {
  // Imported lazily: a static import here would load login.ts before a test
  // file's own vi.mock calls take effect, and cache it unmocked.
  const { browser } = await import("../../src/cli/login.js");
  opened.length = 0;
  vi.spyOn(browser, "open").mockImplementation((url) => void opened.push(url));
});

afterEach(() => {
  vi.restoreAllMocks();
  const l = launched.splice(0);
  if (l.length) throw new Error(`a test launched a browser: ${JSON.stringify(l)}`);
});
