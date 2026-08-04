import { defineConfig } from "tsup";

export default defineConfig({
  entry: ["src/index.ts", "src/adapters/vercel.ts", "src/adapters/openai-agents.ts"],
  format: ["esm", "cjs"],
  dts: true,
  clean: true,
  sourcemap: true,
  outExtension: ({ format }) => ({ js: format === "esm" ? ".mjs" : ".cjs" }),
});
