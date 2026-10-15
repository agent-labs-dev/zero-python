// The CPU benchmarks, TypeScript side: the published @rocicorp/zero package on every case that does no
// I/O, named as bench/test_cpu.py names the Python side, so CodSpeed shows the two next to each other.
//
//   node harness/bench/cpu.ts
//
// Under the CodSpeed action the plugin measures each case by instruction count; elsewhere tinybench
// times it briefly, which only checks that each case runs.
import {withCodSpeed} from '@codspeed/tinybench-plugin';
import {handleMutateRequest} from '@rocicorp/zero/server';
import {Bench} from 'tinybench';
import {handler, wireSchema} from '../mutate.ts';
import {query} from '../query.ts';
import {schema} from '../schema.ts';
import {pushBodySchema, transformRequestMessageSchema, v} from '../zero.ts';
import {compile} from '../node_modules/@rocicorp/zero/out/z2s/src/compiler.js';
import {formatPgInternalConvert} from '../node_modules/@rocicorp/zero/out/z2s/src/sql.js';
import {asQueryInternals} from '../node_modules/@rocicorp/zero/out/zql/src/query/query-internals.js';
import {getServerSchema} from '../node_modules/@rocicorp/zero/out/zero-server/src/schema.js';
import {ZQLDatabase} from '../node_modules/@rocicorp/zero/out/zero-server/src/zql-database.js';
import {
  type Any,
  type Program,
  MUTATE_QUERY,
  W,
  build,
  checkPush,
  fakeTransaction,
  fixture,
  freshClient,
  pushBody,
  transformBody,
  withLimit,
} from './workloads.ts';

// Upstream logs the failures it handles.
console.log = console.info = console.debug = console.warn = console.error = () => {};
const print = (s: string) => process.stdout.write(s + '\n');

const bench = withCodSpeed(new Bench({time: 100}));

for (const kind of ['simple', 'complex']) {
  for (const n of W.batchSizes) {
    const body = transformBody(W.programs[kind], n);
    const text = JSON.stringify(body);
    bench.add(`query.${kind}.b${n}`, () => query({body, userID: 'u1'}));
    bench.add(`query_json.${kind}.b${n}`, async () =>
      JSON.stringify(await query({body: JSON.parse(text), userID: 'u1'})),
    );
  }
}

for (const m of W.parseSizes) {
  const push = pushBody('c1', 'crud', m);
  const pushText = JSON.stringify(push);
  const transform = transformBody(W.programs.complex, m);
  const transformText = JSON.stringify(transform);
  bench.add(`parse.push.m${m}`, () => v.parse(push, pushBodySchema, 'passthrough'));
  bench.add(`parse.transform.q${m}`, () => v.parse(transform, transformRequestMessageSchema));
  bench.add(`parse_json.push.m${m}`, () => v.parse(JSON.parse(pushText), pushBodySchema, 'passthrough'));
  bench.add(`parse_json.transform.q${m}`, () => v.parse(JSON.parse(transformText), transformRequestMessageSchema));
}

// The mutate endpoint over a database that answers from memory: the SDK's own work, no I/O.
const tx = fakeTransaction(fixture('mutate_server_schema_rows.json'));
const fakeDB = new ZQLDatabase({transaction: (fn: Any) => fn(tx)} as Any, wireSchema);
for (const kind of ['put', 'crud']) {
  for (const n of W.pushSizes) {
    const push = async () =>
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
    checkPush(JSON.parse(await push()), n);
    bench.add(`mutate_cpu.${kind}.n${n}`, push);
  }
}

const serverSchema = await getServerSchema(fakeTransaction(fixture('query_server_schema_rows.json')) as Any, schema);
const compileCases: Record<string, Program> = {
  simple: W.programs.simple,
  complex: W.programs.complex,
  related: withLimit(W.run.related, W.compileRelatedLimit),
};
for (const [name, p] of Object.entries(compileCases)) {
  const qi = asQueryInternals(build(p));
  bench.add(`z2s.${name}`, () => formatPgInternalConvert(compile(serverSchema, schema, qi.ast, qi.format)));
}

await bench.run();
for (const task of bench.tasks) {
  const r = task.result as Any;
  print(`${task.name.padEnd(28)} ${r?.latency?.mean ? `${(r.latency.mean * 1000).toFixed(1)} µs` : JSON.stringify(r?.error ?? r?.state ?? '')}`);
}
