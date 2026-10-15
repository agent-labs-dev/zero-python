// The TypeScript side of the benchmark tier (bench/ at the repo root drives it). A worker that times the
// published @rocicorp/zero package on the workloads in bench/workloads.json, one short burst at a time,
// so the driver can alternate it with the Python worker (bench/cases.py) case by case.
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
import {handleMutateRequest} from '@rocicorp/zero/server';
import {handler, mutate, pushProcessor, wireSchema} from '../mutate.ts';
import {apply, query, zql} from '../query.ts';
import {schema} from '../schema.ts';
import {pushBodySchema, transformRequestMessageSchema, v} from '../zero.ts';
import {compile} from '../node_modules/@rocicorp/zero/out/z2s/src/compiler.js';
import {formatPgInternalConvert} from '../node_modules/@rocicorp/zero/out/z2s/src/sql.js';
import {asQueryInternals} from '../node_modules/@rocicorp/zero/out/zql/src/query/query-internals.js';
import {getServerSchema} from '../node_modules/@rocicorp/zero/out/zero-server/src/schema.js';
import {ZQLDatabase} from '../node_modules/@rocicorp/zero/out/zero-server/src/zql-database.js';

// Upstream logs the failures it handles; stdout carries the protocol.
console.log = console.info = console.debug = console.warn = console.error = () => {};

type Config = {
  filter: string;
  workloads: string;
  queryURL: string;
  mutateURL: string;
  serverSchemaRows: string;
};
// oxlint-disable-next-line no-explicit-any
type Any = any;
type Program = {table: string; ops: unknown[][]};

const config: Config = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const W = JSON.parse(readFileSync(config.workloads, 'utf8'));
const filter = new RegExp(config.filter);

/** A sample takes at least this long, so timer resolution and a stray interrupt matter less. */
const SAMPLE_MS = 1;

type Case = {fn: () => unknown; loop: (n: number) => Promise<void> | void; inner: number};
const cases = new Map<string, Case>();
let registered: [string, string][] = [];

function register(name: string, impl: string, fn: () => unknown) {
  if (!filter.test(name)) return;
  cases.set(`${name} ${impl}`, {fn, loop: () => {}, inner: 1});
  registered.push([name, impl]);
}

async function warm(c: Case, ms: number) {
  const first = c.fn();
  if (first instanceof Promise) {
    await first;
    c.loop = async n => {
      for (let i = 0; i < n; i++) await c.fn();
    };
  } else {
    c.loop = n => {
      for (let i = 0; i < n; i++) c.fn();
    };
  }
  const start = performance.now();
  let calls = 0;
  for (; performance.now() < start + ms || calls < 5; calls++) await c.loop(1);
  c.inner = Math.max(1, Math.min(10_000, Math.ceil(SAMPLE_MS / ((performance.now() - start) / calls))));
}

async function time(c: Case, ms: number): Promise<number> {
  const samples: number[] = [];
  const end = performance.now() + ms;
  while (performance.now() < end || samples.length < 1) {
    const t0 = performance.now();
    await c.loop(c.inner);
    samples.push(((performance.now() - t0) * 1000) / c.inner);
  }
  samples.sort((a, b) => a - b);
  return samples[samples.length >> 1];
}

const withLimit = (p: Program, n: number): Program => ({...p, ops: p.ops.map(o => o.map(x => (x === 'N' ? n : x)))});
const build = (p: Program) => apply(zql[p.table], p.ops);

function transformBody(program: Program, n: number) {
  return ['transform', Array.from({length: n}, (_, i) => ({id: `q${i}`, name: 'prog', args: [program]}))];
}

function pushBody(clientID: string, kind: string, n: number) {
  return {
    clientGroupID: 'cg',
    pushVersion: 1,
    timestamp: 1700000000000,
    requestID: clientID,
    mutations: Array.from({length: n}, (_, i) => ({
      type: 'custom',
      id: i + 1,
      clientID,
      name: kind === 'put' ? 'item.put' : 'crud.insert',
      timestamp: 1700000000000 + i,
      args: [
        kind === 'put'
          ? {id: `${clientID}-${i}`, value: 'v'}
          : {table: 'thing', row: {id: `${clientID}-${i}`, ...W.thingRow}},
      ],
    })),
  };
}

let clientSeq = 0;
const freshClient = () => `ts${process.pid}-${clientSeq++}`;
const MUTATE_QUERY = {schema: 'zero_0', appID: 'zero'};

function checkPush(r: Any, n: number) {
  if (r.mutations?.length !== n || r.mutations.some((m: Any) => m.result.error)) {
    throw new Error(`push failed: ${JSON.stringify(r).slice(0, 500)}`);
  }
}

let querySQL: postgres.Sql | undefined;
const queryDB = () => (querySQL ??= postgres(config.queryURL, {onnotice: () => {}}));

/** Each group's cases, with only the setup that group needs. A case's group is its name's first part. */
const groups: Record<string, () => Promise<void>> = {
  async query() {
    for (const kind of ['simple', 'complex']) {
      for (const n of W.batchSizes) {
        const body = transformBody(W.programs[kind], n);
        register(`query.${kind}.b${n}`, 'node', () => query({body, userID: 'u1'}));
      }
    }
  },

  // What an HTTP endpoint adds: parsing the request text and serializing the response.
  async query_json() {
    for (const kind of ['simple', 'complex']) {
      for (const n of W.batchSizes) {
        const text = JSON.stringify(transformBody(W.programs[kind], n));
        register(`query_json.${kind}.b${n}`, 'node', async () =>
          JSON.stringify(await query({body: JSON.parse(text), userID: 'u1'})),
        );
      }
    }
  },

  async parse() {
    for (const m of W.parseSizes) {
      const push = pushBody('c1', 'crud', m);
      register(`parse.push.m${m}`, 'node', () => v.parse(push, pushBodySchema, 'passthrough'));
      const transform = transformBody(W.programs.complex, m);
      register(`parse.transform.q${m}`, 'node', () => v.parse(transform, transformRequestMessageSchema));
    }
  },

  async parse_json() {
    for (const m of W.parseSizes) {
      const pushText = JSON.stringify(pushBody('c1', 'crud', m));
      register(`parse_json.push.m${m}`, 'node', () => v.parse(JSON.parse(pushText), pushBodySchema, 'passthrough'));
      const transformText = JSON.stringify(transformBody(W.programs.complex, m));
      register(`parse_json.transform.q${m}`, 'node', () =>
        v.parse(JSON.parse(transformText), transformRequestMessageSchema),
      );
    }
  },

  // The mutate endpoint over a fake database that answers from memory: the SDK's time only.
  async mutate_cpu() {
    const serverSchemaRows = JSON.parse(readFileSync(config.serverSchemaRows, 'utf8'));
    const lastMutationIDs = new Map<string, number>();
    const fakeTx = {
      wrappedTransaction: null,
      query(sql: string, params: unknown[]) {
        if (sql.includes('pg_catalog')) return Promise.resolve(serverSchemaRows);
        if (sql.includes('lastMutationID')) {
          const id = (lastMutationIDs.get(String(params[1])) ?? 0) + 1;
          lastMutationIDs.set(String(params[1]), id);
          return Promise.resolve([{lastMutationID: id}]);
        }
        return Promise.resolve([]);
      },
    };
    const fakeDB = new ZQLDatabase({transaction: (fn: Any) => fn(fakeTx)} as Any, wireSchema);
    for (const kind of ['put', 'crud']) {
      for (const n of W.pushSizes) {
        const run = async () =>
          JSON.stringify(
            await handleMutateRequest({
              dbProvider: fakeDB as Any,
              handler,
              query: MUTATE_QUERY,
              body: pushBody(freshClient(), kind, n) as never,
              userID: 'u1',
              logLevel: 'error',
            }),
          );
        checkPush(JSON.parse(await run()), n);
        register(`mutate_cpu.${kind}.n${n}`, 'node', run);
      }
    }
  },

  async z2s() {
    const serverSchema = await queryDB().begin(tx =>
      getServerSchema({query: (s: string, p: unknown[]) => tx.unsafe(s, p as never)} as Any, schema),
    );
    const compileCases: Record<string, Program> = {
      simple: W.programs.simple,
      complex: W.programs.complex,
      related: withLimit(W.run.related, W.compileRelatedLimit),
    };
    for (const [k, p] of Object.entries(compileCases)) {
      const qi = asQueryInternals(build(p));
      register(`z2s.${k}`, 'node', () => formatPgInternalConvert(compile(serverSchema, schema, qi.ast, qi.format)));
    }
  },

  // tx.run end to end: compile, query, decode.
  async run() {
    const db = zeroPostgresJS(schema, queryDB());
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
        const run = () =>
          mutate({url: config.mutateURL, query: MUTATE_QUERY, body: pushBody(freshClient(), kind, n), userID: 'u1'});
        checkPush(await run(), n);
        register(`mutate.${kind}.n${n}`, 'postgres.js', run);
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
