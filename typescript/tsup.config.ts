import { defineConfig } from "tsup";

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
});
