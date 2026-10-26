/**
 * The grammar: the registry is the one source for parsing, help, the command
 * tree, `anona guide` and `anona completion`, and the parser keeps argparse's
 * conveniences a script may rely on.
 */
import { describe, expect, it } from "vitest";
import { COMMANDS, leaves, tree, commandName } from "../../src/cli/args.js";
import { EXAMPLES, JSON_SHAPES, document } from "../../src/cli/guide.js";
import { EXIT_CODES } from "../../src/cli/exits.js";
import { run, useDataEnv } from "./data-helpers.js";

describe("command grammar", () => {
  useDataEnv();

  it("the tree covers every command and action, globals on the root only", () => {
    const root = tree();
    expect(root.children.map((c) => c.path[0])).toEqual(COMMANDS.map((c) => c.name));
    expect(root.options.flatMap((o) => o.flags)).toEqual([
      "--base-url", "--no-browser", "--profile", "--timeout", "--retries", "--quiet", "-q", "--version",
    ]);
    for (const c of root.children) {
      for (const o of c.options) expect(o.flags).not.toContain("--base-url");
    }
    const list = root.children.find((c) => c.path[0] === "memories")?.children[0];
    expect(list?.options.flatMap((o) => o.flags)).toContain("--no-prefer-observations");
  });

  it("every leaf command has an example that parses, and a --json shape", async () => {
    for (const leaf of leaves(tree())) {
      const name = commandName(leaf);
      expect(EXAMPLES[name], name).toBeTruthy();
      expect(JSON_SHAPES[name], name).toBeTruthy();
    }
    // Each example gets past the parser (anything but a usage error from it).
    for (const example of Object.values(EXAMPLES)) {
      const argv = (example.match(/"[^"]*"|\S+/g) as string[]).slice(1).map((a) => a.replace(/^"|"$/g, ""));
      if (["mcp", "login", "start"].includes(argv[0] as string)) continue;
      const r = await run([...argv, "--base-url", "https://memory.anonalabs.com"]);
      expect(r.err, example).not.toMatch(/^usage: /);
    }
  });

  it("guide --json carries the exit-code table and is plain data", () => {
    const doc = document() as { exit_codes: Array<{ code: number }> };
    expect(doc.exit_codes.map((e) => e.code)).toEqual(EXIT_CODES.map(([c]) => c));
    expect(JSON.parse(JSON.stringify(doc))).toEqual(doc);
  });

  it("prefixes, `=` values, `--` and options after positionals all parse", async () => {
    // Each gets past the parser to the credential check (exit 4).
    for (const argv of [
      ["record", "x", "--times", "2026-06-01", "--meta={}"],
      ["record", "--", "-starts with a dash"],
      ["--base-url", "https://memory.anonalabs.com", "record", "x"],
      ["record", "x", "--no-browser", "-q", "--retries", "0", "--timeout", "5"],
    ]) {
      const r = await run(argv);
      expect(r.rc, `${argv.join(" ")}: ${r.err}`).toBe(4);
    }
    // `--desc` names no record option: unrecognized, as in argparse.
    expect((await run(["record", "--desc=x", "y"])).rc).toBe(2);
  });

  it("an ambiguous prefix names its candidates, globals first as argparse orders them", async () => {
    const r = await run(["record", "x", "--t", "a"]);
    expect(r.rc).toBe(2);
    expect(r.err).toContain("anona record: error: ambiguous option: --t could match --timeout, --tags, --timestamp");
  });

  it("--version and --help before a command answer for the binary", async () => {
    let r = await run(["--version", "record", "x"]);
    expect([r.rc, r.err]).toEqual([0, ""]);
    expect(r.out).toMatch(/^anona \S+\n$/);
    r = await run(["--help", "record"]);
    expect(r.rc).toBe(0);
    expect(r.out).toContain("usage: anona [-h]");
    r = await run(["record", "x", "--version"]);
    expect(r.rc).toBe(2);
    expect(r.err).toContain("anona: error: unrecognized arguments: --version");
  });

  it("login and logout take --cli or --mcp, never both", async () => {
    for (const verb of ["login", "logout"]) {
      const r = await run([verb, "--cli", "--mcp"]);
      expect(r.rc).toBe(2);
      expect(r.err).toContain(`anona ${verb}: error: argument --mcp: not allowed with argument --cli`);
    }
  });

  it("help: plain help, --llm for the guide, --json only with --llm", async () => {
    let r = await run(["help"]);
    expect([r.rc, r.out.startsWith("usage: anona")]).toEqual([0, true]);
    r = await run(["help", "--llm"]);
    expect(r.out.startsWith("ANONA CLI REFERENCE (for agents)")).toBe(true);
    r = await run(["help", "--json"]);
    expect([r.rc, r.out, r.err]).toEqual([2, "", "anona: --json needs --llm. Nothing was printed.\n"]);
    r = await run(["completion", "tcsh"]);
    expect(r.rc).toBe(2);
    expect(r.err).toContain("argument shell: invalid choice: 'tcsh' (choose from 'bash', 'zsh', 'fish')");
  });

  it("an unknown command says the copy may be older than the docs", async () => {
    // `anona guide` on a copy that predated it printed only the list, and
    // nothing pointed at the version as the cause.
    const r = await run(["nosuchcommand"]);
    expect(r.rc).toBe(2);
    expect(r.err).toContain("invalid choice: 'nosuchcommand'");
    expect(r.err).toMatch(/this copy \(anona [^)]+\) is older than they are: npm install -g @anona-labs\/memory/);
  });

  it("login --help says approving claims a temporary profile", async () => {
    const r = await run(["login", "--help"]);
    expect(r.rc).toBe(0);
    const flat = r.out.split(/\s+/).join(" ");
    expect(flat).toContain("approving also claims it");
    expect(flat).toContain("email not yet used for Anona");
  });
});
