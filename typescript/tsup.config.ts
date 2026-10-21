import { readFileSync } from "node:fs";
import { defineConfig } from "tsup";

// `anona --version` has to answer with the version this bundle WAS BUILT AS.
// Reading package.json at runtime would mean resolving a path out of a
// bundled file, which differs between the esm and cjs outputs and breaks the
// moment anyone re-bundles us; injecting it here keeps package.json the one
// source of truth and leaves nothing to resolve.
const { version } = JSON.parse(readFileSync(new URL("package.json", import.meta.url), "utf8")) as {
  version: string;
};

export default defineConfig({
  // `cli` is the `anona` bin. Its #!/usr/bin/env node line lives in the source
  // and esbuild keeps a leading hashbang on an entry point.
  entry: {
    index: "src/index.ts",
    "adapters/vercel": "src/adapters/vercel.ts",
    "adapters/openai-agents": "src/adapters/openai-agents.ts",
    cli: "src/cli/bin.ts",
  },
  format: ["esm", "cjs"],
  dts: true,
  clean: true,
  sourcemap: true,
  outExtension: ({ format }) => ({ js: format === "esm" ? ".mjs" : ".cjs" }),
  define: { __ANONA_VERSION__: JSON.stringify(version) },
});
