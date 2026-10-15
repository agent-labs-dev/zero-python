// The query half of the wire tier: the published handleQueryRequest over queries that interpret a
// builder program, which tests/wire/programs.py interprets the same way in Python.
import {createBuilder, defineQueries, defineQuery, mustGetQuery} from '@rocicorp/zero';
import {ApplicationError, handleQueryRequest} from '@rocicorp/zero/server';
import {zeroPostgresJS} from '@rocicorp/zero/server/adapters/postgresjs';
import postgres from 'postgres';
import {QueryParseError} from './node_modules/@rocicorp/zero/out/zql/src/query/error.js';
import {toStaticParam} from './node_modules/@rocicorp/zero/out/zero-protocol/src/ast.js';
import {schema} from './schema.ts';
import {shapes} from './errors.ts';

// oxlint-disable-next-line no-explicit-any
type Q = any;
type Sub = {ops: unknown[][]} | null;
type Opts = {flip?: boolean; scalar?: boolean} | null;

export const zql = createBuilder(schema) as Q;

function sub(program: Sub): ((q: Q) => Q) | undefined {
  return program ? q => apply(q, program.ops) : undefined;
}

/** `{$param: [anchor, field]}` becomes a static parameter reference. */
function v(value: unknown): unknown {
  if (typeof value === 'object' && value !== null && '$param' in value) {
    const [anchor, field] = (value as {$param: [string, string]}).$param;
    return {[toStaticParam]: () => ({type: 'static', anchor, field})};
  }
  return value;
}

function expr(eb: Q, e: unknown[]): unknown {
  switch (e[0]) {
    case 'cmp':
      return e.length === 3 ? eb.cmp(e[1], v(e[2])) : eb.cmp(e[1], e[2], v(e[3]));
    case 'cmpLit':
      return eb.cmpLit(v(e[1]), e[2], v(e[3]));
    case 'and':
      return eb.and(...(e[1] as unknown[][]).map(x => expr(eb, x)));
    case 'or':
      return eb.or(...(e[1] as unknown[][]).map(x => expr(eb, x)));
    case 'not':
      // Through `eb.eb`, which is the builder itself.
      return eb.eb.not(expr(eb, e[1] as unknown[]));
    case 'exists':
      return eb.exists(e[1], sub(e[2] as Sub), (e[3] as Opts) ?? undefined);
  }
  throw new Error(`bad expression ${e[0]}`);
}

export function apply(q: Q, ops: unknown[][]): Q {
  for (const op of ops) {
    switch (op[0]) {
      case 'where':
        q = op.length === 3 ? q.where(op[1], v(op[2])) : q.where(op[1], op[2], v(op[3]));
        break;
      case 'whereFn':
        q = q.where((eb: Q) => expr(eb, op[1] as unknown[]));
        break;
      case 'whereRaw':
        // A condition built by hand rather than through the expression builder.
        q = q.where(() => op[1]);
        break;
      case 'whereExists': {
        const [, rel, program, opts] = op as [string, string, Sub, Opts];
        q = program ? q.whereExists(rel, sub(program), opts ?? undefined) : opts ? q.whereExists(rel, opts) : q.whereExists(rel);
        break;
      }
      case 'related':
        q = q.related(op[1], sub(op[2] as Sub));
        break;
      case 'orderBy':
        q = q.orderBy(op[1], op[2]);
        break;
      case 'limit':
        q = q.limit(op[1]);
        break;
      case 'start':
        q = op[2] === null ? q.start(op[1]) : q.start(op[1], {inclusive: op[2]});
        break;
      case 'one':
        q = q.one();
        break;
      default:
        throw new Error(`bad op ${op[0]}`);
    }
  }
  return q;
}

type Program = {table: string; ops: unknown[][]};
const run = ({args}: {args: Program}) => apply(zql[args.table], args.ops);

const program = {
  '~standard': {
    version: 1,
    vendor: 'zero-server',
    validate: (v: unknown) =>
      typeof v === 'object' && v !== null && typeof (v as Program).table === 'string'
        ? {value: v}
        : {issues: [{message: 'expected a program'}]},
  },
};

export const queries = defineQueries({
  prog: defineQuery(run as never),
  nested: {prog: defineQuery(run as never)},
  validated: defineQuery(program as never, run as never),
  parseError: defineQuery(() => {
    throw new QueryParseError({cause: new Error('bad args')});
  }),
  parseErrorEmptyCause: defineQuery(() => {
    throw new QueryParseError({cause: new Error('')});
  }),
  parseErrorNoCause: defineQuery(() => {
    throw new QueryParseError({});
  }),
  appError: defineQuery(() => {
    throw new Error('nope');
  }),
  appDetails: defineQuery((({args}: {args: unknown}) => {
    throw new ApplicationError('app', {details: args as never});
  }) as never),
  returns: defineQuery((({args}: {args: string}) => RETURNS[args]) as never),
  // Handlers that return something other than a query, each failing in its own way upstream.
  constant: defineQuery(() => zql.issue),
  returnsNothing: defineQuery(() => undefined as never),
  returnsString: defineQuery(() => 'abc' as never),
  returnsNumber: defineQuery(() => 5 as never),
  returnsObject: defineQuery(() => ({}) as never),
  wrapped: defineQuery(({args}: {args: Program}) => ({query: run({args})}) as never),
  throw: Object.fromEntries(
    Object.entries(shapes).map(([shape, make]) => [
      shape,
      defineQuery(() => {
        throw make();
      }),
    ]),
  ),
});

/** What a query function may return in place of a query, by name. */
const RETURNS: Record<string, unknown> = {number: 5, fraction: 1.5, string: 'x', boolean: true, object: {}, array: []};

/**
 * Queries by name through the registry, except `raw`, which returns `RETURNS[args]` itself, and `gate.wait`,
 * which finishes only once `gate.open` has run in the same request.
 */
function makeHandler() {
  let open!: () => void;
  const gate = new Promise<void>(resolve => (open = resolve));
  return async (name: string, args: unknown) => {
    if (name === 'raw') return RETURNS[args as string] as never;
    if (name === 'gate.wait') {
      await gate;
      return zql.issue;
    }
    if (name === 'gate.open') {
      open();
      return zql.user;
    }
    return mustGetQuery(queries, name).fn({args, ctx: undefined} as never);
  };
}

export function query({body, userID}: {body: unknown; userID: string | null}) {
  const handler = makeHandler();
  return handleQueryRequest({handler, schema, body: body as never, query: {}, userID, logLevel: 'error'});
}

/** handleQueryRequest over a raw request body, given as base64, answered as the text a server would send. */
export async function queryRequest({body, userID}: {body: string; userID: string | null}) {
  const request = new Request('http://localhost/query', {method: 'POST', body: Buffer.from(body, 'base64')});
  const handler = makeHandler();
  return JSON.stringify(await handleQueryRequest({handler, schema, request, userID, logLevel: 'error'}));
}

// Handlers that skip defineQuery, returning what they like. tests/wire/test_query.py has their twins.
const raw: Record<string, (args: Program) => unknown> = {
  query: args => run({args}),
  wrapped: args => ({query: run({args})}),
  nothing: () => undefined,
  string: () => 'abc',
  number: () => 5,
  object: () => ({}),
};

export function rawQuery({body}: {body: unknown}) {
  return handleQueryRequest({
    handler: (name, args) => raw[name](args as Program) as never,
    schema,
    body: body as never,
    query: {},
    userID: 'u',
    logLevel: 'debug',
  });
}

export function schemaJSON() {
  return JSON.parse(JSON.stringify(schema));
}

/** Run a builder program against Postgres with the published ZQLDatabase, as server-side `tx.run` does. */
const databases = new Map<string, ReturnType<typeof zeroPostgresJS>>();
export async function runQuery({url, program}: {url: string; program: Program}) {
  let db = databases.get(url);
  if (!db) {
    db = zeroPostgresJS(schema, postgres(url, {onnotice: () => {}}));
    databases.set(url, db);
  }
  try {
    return {rows: (await db.run(apply(zql[program.table], program.ops))) ?? null};
  } catch (e) {
    return {error: e instanceof Error ? e.message : String(e)};
  }
}
