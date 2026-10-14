// The TypeScript half of the differential tests. Reads one JSON request per line on stdin and writes one
// JSON response per line on stdout: {ok: true, value} or {ok: false, error}.
import {createInterface} from 'node:readline';

// stdout carries the protocol, so upstream's logging goes to stderr.
console.log = console.info = console.debug = console.warn = console.error;

import {ops} from './ops.ts';

const lines = createInterface({input: process.stdin, crlfDelay: Infinity});
for await (const line of lines) {
  let response: unknown;
  try {
    const {op, ...args} = JSON.parse(line);
    const fn = ops[op as keyof typeof ops];
    if (!fn) throw new Error(`unknown op ${op}`);
    response = {ok: true, value: await fn(args as never)};
  } catch (e) {
    response = {ok: false, error: e instanceof Error ? (e.stack ?? e.message) : String(e)};
  }
  process.stdout.write(JSON.stringify(response) + '\n');
}
