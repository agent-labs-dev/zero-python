// The CPU-only cases, TypeScript side: the published @rocicorp/zero package on every case that does no
// I/O, named as bench/cpu.py names the Python side. harness/bench/cpu.ts runs them under CodSpeed;
// harness/bench/cases.ts times them against Python on the wall clock.
import {handleMutateRequest} from '@rocicorp/zero/server';
import {handler, wireSchema} from '../mutate.ts';
import {query} from '../query.ts';
import {schema} from '../schema.ts';
import {pushBodySchema, transformRequestMessageSchema, v} from '../zero.ts';
import {compile} from '../node_modules/@rocicorp/zero/out/z2s/src/compiler.js';
import {mapAST} from '../node_modules/@rocicorp/zero/out/zero-protocol/src/ast.js';
import {clientToServer} from '../node_modules/@rocicorp/zero/out/zero-schema/src/name-mapper.js';
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

/** Each case by name: a function with no arguments, returning its result or a promise of it. */
export async function cpuCases(): Promise<Map<string, () => unknown>> {
  const out = new Map<string, () => unknown>();

  for (const kind of ['simple', 'complex']) {
    for (const n of W.batchSizes) {
      const body = transformBody(W.programs[kind], n);
      const text = JSON.stringify(body);
      out.set(`query.${kind}.b${n}`, () => query({body, userID: 'u1'}));
      out.set(`query_json.${kind}.b${n}`, async () => JSON.stringify(await query({body: JSON.parse(text), userID: 'u1'})));
    }
  }

  for (const m of W.parseSizes) {
    const push = pushBody('c1', 'crud', m);
    const pushText = JSON.stringify(push);
    const transform = transformBody(W.programs.complex, m);
    const transformText = JSON.stringify(transform);
    out.set(`parse.push.m${m}`, () => v.parse(push, pushBodySchema, 'passthrough'));
    out.set(`parse.transform.q${m}`, () => v.parse(transform, transformRequestMessageSchema));
    out.set(`parse_json.push.m${m}`, () => v.parse(JSON.parse(pushText), pushBodySchema, 'passthrough'));
    out.set(`parse_json.transform.q${m}`, () => v.parse(JSON.parse(transformText), transformRequestMessageSchema));
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
      out.set(`mutate_cpu.${kind}.n${n}`, push);
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
    out.set(`z2s.${name}`, () => formatPgInternalConvert(compile(serverSchema, schema, qi.ast, qi.format)));
  }
  // The query endpoint's two stages on their own: building a program's query, and mapping its AST to
  // server names.
  const mapper = clientToServer(schema.tables);
  for (const kind of ['simple', 'complex']) {
    const program = W.programs[kind];
    const ast = asQueryInternals(build(program)).ast;
    out.set(`build.${kind}`, () => build(program));
    out.set(`map.${kind}`, () => mapAST(ast, mapper));
  }
  return out;
}
