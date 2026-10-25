#!/usr/bin/env node
import { BROKEN_PIPE_EXIT } from "./exits.js";
import { main } from "./index.js";

// A reader that goes away (`anona guide`, piped into `head`) ends the command
// quietly: exit 141, as a process killed by SIGPIPE reports, and nothing on
// stderr -- the same as the pip package. Node otherwise turns the EPIPE into
// an unhandled 'error' event and a stack trace.
for (const stream of [process.stdout, process.stderr]) {
  stream.on("error", (err: NodeJS.ErrnoException) => {
    if (err.code === "EPIPE") process.exit(BROKEN_PIPE_EXIT);
    throw err;
  });
}

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
