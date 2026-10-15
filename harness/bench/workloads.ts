// The request bodies and fakes both benchmark suites build from bench/workloads.json, as
// bench/workloads.py builds them for the Python side.
import {readFileSync} from 'node:fs';
import {apply, zql} from '../query.ts';

// oxlint-disable-next-line no-explicit-any
export type Any = any;
export type Program = {table: string; ops: unknown[][]};

const BENCH = new URL('../../bench/', import.meta.url);
export const W = JSON.parse(readFileSync(new URL('workloads.json', BENCH), 'utf8'));
export const fixture = (name: string) => JSON.parse(readFileSync(new URL(`fixtures/${name}`, BENCH), 'utf8'));

export const MUTATE_QUERY = {schema: 'zero_0', appID: 'zero'};

export const withLimit = (p: Program, n: number): Program => ({
  ...p,
  ops: p.ops.map(o => o.map(x => (x === 'N' ? n : x))),
});
export const build = (p: Program) => apply(zql[p.table], p.ops);

export function transformBody(program: Program, n: number) {
  return ['transform', Array.from({length: n}, (_, i) => ({id: `q${i}`, name: 'prog', args: [program]}))];
}

export function pushBody(clientID: string, kind: string, n: number) {
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
/** A client ID no push has used, so its mutation IDs start at 1. */
export const freshClient = () => `ts${process.pid}-${clientSeq++}`;

export function checkPush(r: Any, n: number) {
  if (r.mutations?.length !== n || r.mutations.some((m: Any) => m.result.error)) {
    throw new Error(`push failed: ${JSON.stringify(r).slice(0, 500)}`);
  }
}

/**
 * Answers the SDK's own statements from memory: the server-schema query from `rows`, the lastMutationID
 * bump from a counter, and everything else with no rows.
 */
export function fakeTransaction(rows: unknown[]) {
  const lastMutationIDs = new Map<string, number>();
  return {
    wrappedTransaction: null,
    query(sql: string, params: unknown[]) {
      if (sql.includes('pg_catalog')) return Promise.resolve(rows);
      if (sql.includes('lastMutationID')) {
        const id = (lastMutationIDs.get(String(params[1])) ?? 0) + 1;
        lastMutationIDs.set(String(params[1]), id);
        return Promise.resolve([{lastMutationID: id}]);
      }
      return Promise.resolve([]);
    },
  };
}
