#!/usr/bin/env node
import { main } from "./index.js";

main(process.argv.slice(2), { stdin: process.stdin, stdout: process.stdout, stderr: process.stderr }).then(
  (code) => {
    process.exitCode = code;
  },
  (err: unknown) => {
    // Message only: a stack can carry request details.
    process.stderr.write(`anona: ${err instanceof Error ? err.message : String(err)}\n`);
    process.exitCode = 1;
  },
);
