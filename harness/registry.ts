// Query and mutator registries as an app defines them, looked up by name. tests/wire/test_registry.py
// builds the same registries in Python and diffs what each lookup finds.
import {
  defineMutator,
  defineMutators,
  defineQueries,
  defineQuery,
  getMutator,
  getQuery,
  mustGetMutator,
  mustGetQuery,
} from '@rocicorp/zero';
import {asQueryInternals} from './node_modules/@rocicorp/zero/out/zql/src/query/query-internals.js';
import {isMutator} from './node_modules/@rocicorp/zero/out/zql/src/mutate/mutator.js';
import {isQuery} from './node_modules/@rocicorp/zero/out/zql/src/query/query-registry.js';
import {zql} from './query.ts';

// oxlint-disable-next-line no-explicit-any
type Any = any;

const upper = {
  '~standard': {
    version: 1,
    vendor: 'zero-server',
    validate: (v: unknown) => (typeof v === 'string' ? {value: v.toUpperCase()} : {issues: [{message: 'expected a string'}]}),
  },
};
const later = {'~standard': {version: 1, vendor: 'zero-server', validate: (v: unknown) => Promise.resolve({value: v})}};

const baseQueries = defineQueries({
  issue: {
    all: defineQuery(() => zql.issue),
    byTitle: defineQuery(({args}: Any) => zql.issue.where('title', args)),
    checked: defineQuery(upper as never, ({args}: Any) => zql.issue.where('title', args)),
    later: defineQuery(later as never, () => zql.issue),
  },
  plain: defineQuery(() => zql.user),
} as never);
const queries: Record<string, Any> = {
  base: baseQueries,
  merged: defineQueries(baseQueries, {
    issue: {open: defineQuery(() => zql.issue.where('open', true))},
    plain: defineQuery(() => zql.comment),
  } as never),
  fromDefinitions: defineQueries({a: defineQuery(() => zql.issue)} as never, {a: {b: defineQuery(() => zql.user)}} as never),
};

const noop = async () => {};
const baseMutators = defineMutators({
  item: {put: defineMutator(noop), checked: defineMutator(upper as never, noop), later: defineMutator(later as never, noop)},
  top: defineMutator(noop),
} as never);
const mutators: Record<string, Any> = {
  base: baseMutators,
  merged: defineMutators(baseMutators, {item: {extra: defineMutator(noop)}, top: defineMutator(noop)} as never),
  fromDefinitions: defineMutators({a: defineMutator(noop)} as never, {b: defineMutator(noop)} as never),
};

function attempt<T>(fn: () => T): T | {error: string} {
  try {
    return fn();
  } catch (e) {
    return {error: (e as Error).message};
  }
}

function keys(value: object) {
  return {kind: 'namespace', keys: Object.keys(value).filter(k => k !== '~')};
}

function describeQuery(value: Any, args: unknown): unknown {
  if (value === undefined) return {kind: 'undefined'};
  if (isQuery(value)) {
    return {
      kind: 'query',
      name: value.queryName,
      ast: attempt(() => asQueryInternals(value.fn({args, ctx: {}})).ast),
      request: attempt(() => {
        const request = value(args);
        return {name: request.query.queryName, args: request.args ?? null};
      }),
    };
  }
  return typeof value === 'object' && value !== null ? keys(value) : {kind: typeof value};
}

async function describeMutator(value: Any, args: unknown): Promise<unknown> {
  if (value === undefined) return {kind: 'undefined'};
  if (isMutator(value)) {
    let run: unknown = 'ok';
    try {
      await value.fn({args, ctx: {}, tx: null});
    } catch (e) {
      run = {error: (e as Error).message, details: (e as {details?: unknown}).details ?? null};
    }
    const request = value(args);
    return {kind: 'mutator', name: value.mutatorName, run, request: {name: request.mutator.mutatorName, args: request.args ?? null}};
  }
  return typeof value === 'object' && value !== null ? keys(value) : {kind: typeof value};
}

export async function registryLookup({kind, registry, name, args}: {kind: string; registry: string; name: string; args: unknown}) {
  if (kind === 'query') {
    const reg = queries[registry];
    return {
      get: describeQuery(getQuery(reg, name), args),
      mustGet: attempt(() => describeQuery(mustGetQuery(reg, name), args)),
    };
  }
  const reg = mutators[registry];
  let mustGet: unknown;
  try {
    mustGet = await describeMutator(mustGetMutator(reg, name), args);
  } catch (e) {
    mustGet = {error: (e as Error).message};
  }
  return {get: await describeMutator(getMutator(reg, name), args), mustGet};
}
