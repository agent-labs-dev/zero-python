// The TypeScript side of the benchmark tier (bench/ at the repo root runs it). Times the published
// @rocicorp/zero package on the workloads in bench/workloads.json and prints one JSON line of results.
// Usage: node harness/bench/cases.ts <config.json> <group>, where the group is a case name's first part.
import {readFileSync} from 'node:fs';
import {performance} from 'node:perf_hooks';
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

// Upstream logs the failures it handles; stdout carries the results.
console.log = console.info = console.debug = console.warn = console.error = () => {};

type Config = {
  filter: string;
  budgetMs: number;
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
const group = process.argv[3];
const results: {case: string; impl: string; median_us: number; samples: number}[] = [];

/** The median time of one call of `fn`, by the same procedure as bench/cases.py. */
async function bench(name: string, impl: string, fn: () => unknown) {
  if (name.split('.')[0] !== group || !filter.test(name)) return;
  const first = fn();
  const isAsync = first instanceof Promise;
  if (isAsync) await first;
  const loop = isAsync
    ? async (n: number) => {
        for (let i = 0; i < n; i++) await fn();
      }
    : (n: number) => {
        for (let i = 0; i < n; i++) fn();
      };
  // Enough calls per sample that one sample takes at least 0.2 ms.
  let t0 = performance.now();
  await loop(1);
  const inner = Math.max(1, Math.min(1000, Math.ceil(0.2 / Math.max(performance.now() - t0, 1e-4))));
  const warmEnd = performance.now() + config.budgetMs * 0.3;
  for (let w = 0; performance.now() < warmEnd || w < 5; w++) await loop(1);
  const samples: number[] = [];
  const end = performance.now() + config.budgetMs;
  while ((performance.now() < end || samples.length < 7) && samples.length < 5000) {
    t0 = performance.now();
    await loop(inner);
    samples.push(((performance.now() - t0) * 1000) / inner);
  }
  samples.sort((a, b) => a - b);
  results.push({case: name, impl, median_us: samples[samples.length >> 1], samples: samples.length});
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

// ---- query endpoint ----
for (const kind of ['simple', 'complex']) {
  for (const n of W.batchSizes) {
    const body = transformBody(W.programs[kind], n);
    const text = JSON.stringify(body);
    await bench(`query.${kind}.b${n}`, 'node', () => query({body, userID: 'u1'}));
    await bench(`query_json.${kind}.b${n}`, 'node', async () =>
      JSON.stringify(await query({body: JSON.parse(text), userID: 'u1'})),
    );
  }
}

// ---- request parsing ----
for (const m of W.parseSizes) {
  const push = pushBody('c1', 'crud', m);
  const pushText = JSON.stringify(push);
  await bench(`parse.push.m${m}`, 'node', () => v.parse(push, pushBodySchema, 'passthrough'));
  await bench(`parse_json.push.m${m}`, 'node', () => v.parse(JSON.parse(pushText), pushBodySchema, 'passthrough'));
  const transform = transformBody(W.programs.complex, m);
  const transformText = JSON.stringify(transform);
  await bench(`parse.transform.q${m}`, 'node', () => v.parse(transform, transformRequestMessageSchema));
  await bench(`parse_json.transform.q${m}`, 'node', () =>
    v.parse(JSON.parse(transformText), transformRequestMessageSchema),
  );
}

// ---- mutate endpoint, CPU only: a fake database that answers from memory ----
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
    await bench(`mutate_cpu.${kind}.n${n}`, 'node', run);
  }
}

// ---- z2s compile, and tx.run end to end ----
const querySQL = postgres(config.queryURL, {onnotice: () => {}});
const serverSchema = await querySQL.begin(tx =>
  getServerSchema({query: (s: string, p: unknown[]) => tx.unsafe(s, p as never)} as Any, schema),
);
const compileCases: Record<string, Program> = {
  simple: W.programs.simple,
  complex: W.programs.complex,
  related: withLimit(W.run.related, W.compileRelatedLimit),
};
for (const [k, p] of Object.entries(compileCases)) {
  const qi = asQueryInternals(build(p));
  await bench(`z2s.${k}`, 'node', () => formatPgInternalConvert(compile(serverSchema, schema, qi.ast, qi.format)));
}
const queryDB = zeroPostgresJS(schema, querySQL);
for (const [k, p] of Object.entries(W.run as Record<string, Program>)) {
  for (const n of W.rowCounts) {
    const q = build(withLimit(p, n));
    const rows = (await queryDB.run(q)) as unknown[];
    if (rows.length !== n) throw new Error(`run.${k}: expected ${n} rows, got ${rows.length}`);
    await bench(`run.${k}.r${n}`, 'postgres.js', () => queryDB.run(q));
  }
}
await querySQL.end();

// ---- mutate endpoint against Postgres ----
const mutateSQL = postgres(config.mutateURL, {onnotice: () => {}});
await mutateSQL.unsafe('TRUNCATE item, things, pair, zero_0."clients", zero_0."mutations"');
await mutateSQL.end();
for (const kind of ['put', 'crud']) {
  for (const n of W.pushSizes) {
    const run = () => mutate({url: config.mutateURL, query: MUTATE_QUERY, body: pushBody(freshClient(), kind, n), userID: 'u1'});
    checkPush(await run(), n);
    await bench(`mutate.${kind}.n${n}`, 'postgres.js', run);
  }
}
await bench('pushproc.put.n10', 'postgres.js', () => {
  const body = pushBody(freshClient(), 'put', 10);
  for (const m of body.mutations) m.name = 'legacy.put';
  return pushProcessor({url: config.mutateURL, query: MUTATE_QUERY, body});
});

process.stdout.write(JSON.stringify(results) + '\n');
// The harness's pooled connections would otherwise keep the process alive.
process.exit(0);
