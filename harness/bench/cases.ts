// The TypeScript worker of the paired Postgres benchmarks (bench/__main__.py drives it). Times the
// published @rocicorp/zero package one short burst at a time, so the driver can alternate it with the
// Python worker (bench/cases.py) case by case.
//
// Usage: node harness/bench/cases.ts <config.json>, then one JSON request per line on stdin:
//   {"op": "setup", "group": g}             -> {"cases": [[case, impl], ...]}
//   {"op": "warm", "case", "impl", "ms"}    -> {}: run for ms, then size a sample to SAMPLE_MS
//   {"op": "time", "case", "impl", "ms"}    -> {"us": median microseconds per call over ms}
import {readFileSync} from 'node:fs';
import {performance} from 'node:perf_hooks';
import {createInterface} from 'node:readline';
import {zeroPostgresJS} from '@rocicorp/zero/server/adapters/postgresjs';
import postgres from 'postgres';
import {mutate, pushProcessor} from '../mutate.ts';
import {schema} from '../schema.ts';
import {type Program, MUTATE_QUERY, W, build, checkPush, freshClient, pushBody, withLimit} from './workloads.ts';

// Upstream logs the failures it handles; stdout carries the protocol.
console.log = console.info = console.debug = console.warn = console.error = () => {};

const config: {filter: string; queryURL: string; mutateURL: string} = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const filter = new RegExp(config.filter);

/** A sample takes at least this long, so timer resolution and a stray interrupt matter less. */
const SAMPLE_MS = 1;

type Case = {fn: () => Promise<unknown>; inner: number};
const cases = new Map<string, Case>();
let registered: [string, string][] = [];

function register(name: string, impl: string, fn: () => Promise<unknown>) {
  if (!filter.test(name)) return;
  cases.set(`${name} ${impl}`, {fn, inner: 1});
  registered.push([name, impl]);
}

async function loop(c: Case, n: number) {
  for (let i = 0; i < n; i++) await c.fn();
}

async function warm(c: Case, ms: number) {
  const start = performance.now();
  let calls = 0;
  for (; performance.now() < start + ms || calls < 5; calls++) await c.fn();
  c.inner = Math.max(1, Math.min(10_000, Math.ceil(SAMPLE_MS / ((performance.now() - start) / calls))));
}

async function time(c: Case, ms: number): Promise<number> {
  const samples: number[] = [];
  const end = performance.now() + ms;
  while (performance.now() < end || samples.length < 1) {
    const t0 = performance.now();
    await loop(c, c.inner);
    samples.push(((performance.now() - t0) * 1000) / c.inner);
  }
  samples.sort((a, b) => a - b);
  return samples[samples.length >> 1];
}

/** Each group's cases. A case's group is its name's first part. */
const groups: Record<string, () => Promise<void>> = {
  // tx.run end to end: compile, query, decode.
  async run() {
    const db = zeroPostgresJS(schema, postgres(config.queryURL, {onnotice: () => {}}));
    for (const [k, p] of Object.entries(W.run as Record<string, Program>)) {
      for (const n of W.rowCounts) {
        const q = build(withLimit(p, n));
        const rows = (await db.run(q)) as unknown[];
        if (rows.length !== n) throw new Error(`run.${k}: expected ${n} rows, got ${rows.length}`);
        register(`run.${k}.r${n}`, 'postgres.js', () => db.run(q));
      }
    }
  },

  async mutate() {
    for (const kind of ['put', 'crud']) {
      for (const n of W.pushSizes) {
        const push = () =>
          mutate({url: config.mutateURL, query: MUTATE_QUERY, body: pushBody(freshClient(), kind, n), userID: 'u1'});
        checkPush(await push(), n);
        register(`mutate.${kind}.n${n}`, 'postgres.js', push);
      }
    }
  },

  async pushproc() {
    register('pushproc.put.n10', 'postgres.js', () => {
      const body = pushBody(freshClient(), 'put', 10);
      for (const m of body.mutations) m.name = 'legacy.put';
      return pushProcessor({url: config.mutateURL, query: MUTATE_QUERY, body});
    });
  },
};

for await (const line of createInterface({input: process.stdin, crlfDelay: Infinity})) {
  const req = JSON.parse(line);
  let res: unknown;
  if (req.op === 'setup') {
    registered = [];
    await groups[req.group]();
    res = {cases: registered};
  } else {
    const c = cases.get(`${req.case} ${req.impl}`);
    if (!c) throw new Error(`unknown case ${req.case} ${req.impl}`);
    res = req.op === 'warm' ? (await warm(c, req.ms), {}) : {us: await time(c, req.ms)};
  }
  process.stdout.write(JSON.stringify(res) + '\n');
}
// Pooled connections would otherwise keep the process alive once stdin closes.
process.exit(0);
