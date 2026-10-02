#!/usr/bin/env node
import { runCli } from "../dist/cli.js";

const code = await runCli(process.argv.slice(2));
// Pipe writes can still be queued when runCli returns. Drain both streams
// before explicitly exiting, including when the host leaves handles open.
await Promise.all(
  [process.stdout, process.stderr].map(
    (stream) =>
      new Promise((resolve, reject) => {
        stream.write("", (error) => (error ? reject(error) : resolve()));
      }),
  ),
);
process.exit(code ?? 0);
