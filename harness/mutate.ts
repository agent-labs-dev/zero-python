// The mutate half of the wire tier: the published handleMutateRequest over the postgres.js adapter,
// with mutators that tests/wire/mutators.py mirrors one for one.
import {
  boolean,
  createBuilder,
  createSchema,
  defineMutator,
  defineMutators,
  enumeration,
  json,
  number,
  string,
  table,
} from '@rocicorp/zero';
import {
  ApplicationError,
  handleMutateRequest,
  PushProcessor,
  ZQLDatabase,
  type Database,
  type DBConnection,
  type IsolationLevel,
  type RetryPredicate,
  type MutateRequestHandler,
  type TransactionProviderHooks,
  type TransactionProviderInput,
} from '@rocicorp/zero/server';
import {PostgresJSConnection, zeroPostgresJS} from '@rocicorp/zero/server/adapters/postgresjs';
import postgres from 'postgres';
import {shapes} from './errors.ts';

const item = table('item').columns({id: string(), value: string()}).primaryKey('id');
// One column per kind of conversion tx.mutate does on the way to Postgres.
const thing = table('thing')
  .from('things')
  .columns({
    id: string(),
    n: number().optional(),
    f: number().optional(),
    b: boolean().optional(),
    j: json().optional(),
    t: number().from('happened_at').optional(),
    d: number().optional(),
    s: string().optional(),
    tags: json().optional(),
    u: string().optional(),
    mood: enumeration<'happy' | 'sad'>().optional(),
    tm: number().optional(),
    ttz: number().optional(),
    amount: number().optional(),
    ip: string().optional(),
    stamps: json().optional(),
    clocks: json().optional(),
  })
  .primaryKey('id');
const pair = table('pair').columns({a: string(), b: number(), note: string().optional()}).primaryKey('a', 'b');
export const wireSchema = createSchema({tables: [item, thing, pair]});

const zql = createBuilder(wireSchema);

export type Tx = {
  clientID: string;
  mutationID: number;
  dbTransaction: {query(sql: string, params: unknown[]): Promise<unknown[]>; wrappedTransaction: unknown};
  run(query: unknown): Promise<unknown>;
  retryOn(shouldRetry: RetryPredicate): void;
  // oxlint-disable-next-line no-explicit-any
  mutate: any;
};
export type Args = {
  id?: string;
  value?: string;
  message?: string;
  details?: unknown;
  table?: string;
  row?: unknown;
  ret?: unknown;
  predicates?: string[];
  fail?: number;
  db?: boolean;
  run?: boolean;
};

const PUT = `INSERT INTO item (id, value) VALUES ($1, $2)
  ON CONFLICT (id) DO UPDATE SET value = item.value || '+' || excluded.value`;

export async function put(tx: Tx, args: Args) {
  await tx.dbTransaction.query(PUT, [String(args.id ?? 'x'), String(args.value ?? '')]);
}

// How many times item.retry has run for each mutation, by `clientID:mutationID`. Reset per test example.
const runs = new Map<string, number>();
// A transaction item.keep held on to after it ended.
let kept: Tx | undefined;

export function resetRuns() {
  runs.clear();
  kept = undefined;
  return null;
}

/** The tx.retryOn() predicates item.retry registers, by name; tests/wire/mutators.py has their twins. */
const predicates: Record<string, RetryPredicate> = {
  yes: () => true,
  no: () => false,
  later: async () => {
    await new Promise(resolve => setTimeout(resolve, 1));
    return true;
  },
  throw: () => {
    throw new Error('predicate broke');
  },
  reject: async () => {
    throw new Error('predicate rejected');
  },
  truthy: () => 1 as never,
  firstOnly: (_error, attempt) => attempt === 1,
  matches: error => error instanceof Error && error.message.includes('failed'),
};

export const mutators: Record<string, (tx: Tx, args: Args) => Promise<unknown>> = {
  'item.put': put,
  'item.appError': async (tx, args) => {
    await put(tx, args);
    throw new ApplicationError(String(args.message ?? 'refused'), {details: args.details as never});
  },
  'item.error': async (tx, args) => {
    await put(tx, args);
    throw new Error(String(args.message ?? 'broke'));
  },
  'item.typeError': async (tx, args) => {
    await put(tx, args);
    throw new TypeError(String(args.message ?? 'bad type'));
  },
  // A `details` property only when the args have one, which may be null.
  'item.detailsError': async (tx, args) => {
    await put(tx, args);
    throw Object.assign(new TypeError('with details'), 'details' in args ? {details: args.details} : {});
  },
  'crud.insert': (tx, args) => tx.mutate[args.table!].insert(args.row),
  'crud.upsert': (tx, args) => tx.mutate[args.table!].upsert(args.row),
  'crud.update': (tx, args) => tx.mutate[args.table!].update(args.row),
  'crud.delete': (tx, args) => tx.mutate[args.table!].delete(args.row),
  // Reads through tx.run inside the mutation's transaction, so it sees the push's earlier writes.
  'item.count': async (tx, args) => {
    const rows = (await tx.run(zql.item.where('value', '!=', String(args.value ?? '')).orderBy('value', 'desc'))) as {
      id: string;
    }[];
    await put(tx, {id: 'count', value: rows.map(r => r.id).join(',')});
  },
  'item.dbError': async tx => {
    await tx.dbTransaction.query('SELECT * FROM missing_table', []);
  },
  // Whatever `ret` holds is the mutation's result data; without it, there is none.
  'item.return': async (tx, args) => {
    await put(tx, args);
    return args.ret;
  },
  // Fails its first `fail` runs, after registering `predicates`, and returns the number of the run.
  'item.retry': async (tx, args) => {
    for (const name of args.predicates ?? []) {
      tx.retryOn(predicates[name]);
    }
    await put(tx, args);
    const key = `${tx.clientID}:${tx.mutationID}`;
    const run = (runs.get(key) ?? 0) + 1;
    runs.set(key, run);
    if (run <= Number(args.fail ?? 0)) {
      if (args.db) {
        await tx.dbTransaction.query('SELECT * FROM missing_table', []);
      }
      throw new Error(`run ${run} failed`);
    }
    return run;
  },
  'item.keep': async (tx, args) => {
    kept = tx;
    await put(tx, args);
    // Still open, so this is the driver's transaction.
    return tx.dbTransaction.wrappedTransaction === undefined;
  },
  // Uses the transaction item.keep held on to, which has ended.
  'item.useKept': async (_tx, args) => {
    if (!kept) throw new Error('nothing kept');
    if (args.db) {
      await kept.dbTransaction.query('SELECT 1', []);
    }
    if (args.run) {
      await kept.run(zql.item);
    }
    return kept.dbTransaction.wrappedTransaction === undefined;
  },
  ...Object.fromEntries(
    Object.entries(shapes).map(([shape, make]) => [
      `throw.${shape}`,
      async (tx: Tx, args: Args) => {
        await put(tx, args);
        throw make();
      },
    ]),
  ),
};

export const handler: MutateRequestHandler<Database<Tx>> = async (transact, mutation) => {
  const args = (mutation.args[0] ?? {}) as Args;
  if (mutation.name === 'pre.appError') {
    throw new ApplicationError(String(args.message ?? 'pre'), {details: args.details as never});
  }
  if (mutation.name === 'pre.error') {
    throw new Error(String(args.message ?? 'pre'));
  }
  const result = await transact(async (tx, name, mutatorArgs) => {
    const mutator = mutators[name.replace(/^post\./, 'item.')];
    if (!mutator) throw new Error(`unknown mutator ${name}`);
    return (await mutator(tx, (mutatorArgs ?? {}) as Args)) as never;
  });
  if (mutation.name.startsWith('post.')) {
    throw new Error('after commit');
  }
  return result;
};

const providers = new Map<string, Database<Tx>>();

/** The database for `url`, failing to open any transaction for client `cfail`. */
export function provider(url: string): Database<Tx> {
  let db = providers.get(url);
  if (!db) {
    const zql = zeroPostgresJS(wireSchema, postgres(url, {onnotice: () => {}}));
    db = {
      transaction: (cb, input?: TransactionProviderInput) =>
        input?.clientID === 'cfail'
          ? Promise.reject(new Error('cannot open'))
          : zql.transaction(cb as never, input),
    } as Database<Tx>;
    providers.set(url, db);
  }
  return db;
}

export function mutate({
  url,
  query,
  body,
  userID,
}: {
  url: string;
  query: Record<string, string>;
  body: unknown;
  userID: string | null;
}) {
  // The default log level, where PushProcessor below logs at debug: between them every log line runs.
  return handleMutateRequest({dbProvider: provider(url), handler, query, body: body as never, userID});
}

/** handleMutateRequest over a raw request, its body given as base64, answered as the text a server would send. */
export async function mutateRequest({
  url,
  search,
  body,
  userID,
}: {
  url: string;
  search: string;
  body: string;
  userID: string | null;
}) {
  const request = new Request(`http://localhost/mutate?${search}`, {method: 'POST', body: Buffer.from(body, 'base64')});
  return JSON.stringify(
    await handleMutateRequest({dbProvider: provider(url), handler, request, userID, logLevel: 'error'}),
  );
}

export type {TransactionProviderHooks};

// PushProcessor dispatches by name to a registry from defineMutators, or to plain legacy functions.
const registry = defineMutators({
  item: {
    put: defineMutator(async ({tx, args, ctx}: {tx: Tx; args: Args; ctx: {tag: string}}) =>
      put(tx, {...args, value: `${String(args?.value ?? '')}@${ctx.tag}`}),
    ),
    appError: defineMutator(async ({tx, args}: {tx: Tx; args: Args}) => mutators['item.appError'](tx, args ?? {})),
    ret: defineMutator(async ({tx, args}: {tx: Tx; args: Args}) => mutators['item.return'](tx, args ?? {})),
  },
} as never);
const legacy = {
  legacy: {put: (tx: Tx, args: Args) => put(tx, args ?? {}), ret: (tx: Tx, args: Args) => mutators['item.return'](tx, args ?? {})},
};

export function pushProcessor({url, query, body}: {url: string; query: Record<string, string>; body: unknown}) {
  return new PushProcessor(provider(url) as never, {tag: 'ctx'}, 'debug').process(
    {...(registry as object), ...legacy} as never,
    query,
    body as never,
  );
}

/** `PushProcessor.process` given a Fetch `Request`, its body base64-encoded. */
export function pushProcessorRequest({url, search, body}: {url: string; search: string; body: string}) {
  const request = new Request(`http://localhost/mutate${search}`, {method: 'POST', body: Buffer.from(body, 'base64')});
  return new PushProcessor(provider(url) as never, {tag: 'ctx'}).process(
    {...(registry as object), ...legacy} as never,
    request,
  );
}

/** handleMutateRequest against `schema`, which may not match the database: its first transaction checks. */
export function mutateWithSchema({
  url,
  schema,
  query,
  body,
}: {
  url: string;
  schema: unknown;
  query: Record<string, string>;
  body: unknown;
}) {
  const sql = postgres(url, {onnotice: () => {}});
  return handleMutateRequest({
    dbProvider: zeroPostgresJS(schema as never, sql) as never,
    handler,
    query,
    body: body as never,
    userID: null,
    logLevel: 'error',
  }).finally(() => sql.end());
}

/** tx.retryOn() on a transaction nothing will re-run: one ZQLDatabase.transaction opens with no input. */
export async function retryOnUnsupported({url}: {url: string}) {
  const sql = postgres(url, {onnotice: () => {}});
  try {
    return await zeroPostgresJS(wireSchema, sql).transaction(async tx => {
      try {
        (tx as unknown as Tx).retryOn(() => true);
        return null;
      } catch (e) {
        return (e as Error).message;
      }
    });
  } finally {
    await sql.end();
  }
}

/** The isolation level a transaction of zeroPostgresJS runs at, given `level` or none. */
export async function isolation({url, level}: {url: string; level: IsolationLevel | null}) {
  const sql = postgres(url, {onnotice: () => {}});
  try {
    return await zeroPostgresJS(wireSchema, sql, level === null ? undefined : {isolationLevel: level}).transaction(
      async tx => {
        const rows = (await (tx as unknown as Tx).dbTransaction.query('SHOW transaction_isolation', [])) as {
          transaction_isolation: string;
        }[];
        return rows[0].transaction_isolation;
      },
    );
  } finally {
    await sql.end();
  }
}

/**
 * `ZQLDatabase.run` over a connection that logs what it is asked to do: with `bare`, one that runs
 * statements outside a transaction. With `failFirst`, the first statement fails. With `concurrent`, the
 * runs start together, otherwise one after another. Returns each run's rows or error, and the log.
 */
export async function recordedRun({
  url,
  bare,
  failFirst,
  concurrent,
  runs: count,
}: {
  url: string;
  bare: boolean;
  failFirst: boolean;
  concurrent: boolean;
  runs: number;
}) {
  const sql = postgres(url, {onnotice: () => {}});
  const inner = new PostgresJSConnection(sql);
  const log: string[] = [];
  let failing = failFirst;
  const record = (event: string, run: () => Promise<unknown>) => {
    log.push(event);
    if (failing) {
      failing = false;
      return Promise.reject(new Error('statement failed'));
    }
    return run();
  };
  const connection: DBConnection<unknown> = {
    transaction: cb => {
      log.push('transaction');
      return inner.transaction(dbTx => {
        const wrapped = Object.create(dbTx);
        wrapped.query = (q: string, p: unknown[]) => record('query in transaction', () => dbTx.query(q, p));
        return cb(wrapped);
      });
    },
    ...(bare ? {query: (q: string, p: unknown[]) => record('query', () => inner.query(q, p))} : {}),
  } as DBConnection<unknown>;
  const db = new ZQLDatabase(connection, wireSchema);
  const one = async () => {
    try {
      return {rows: await db.run(zql.item.where('value', '!=', 'x').orderBy('id', 'asc') as never)};
    } catch (e) {
      return {error: (e as Error).message};
    }
  };
  try {
    const results = [];
    if (concurrent) {
      results.push(...(await Promise.all(Array.from({length: count}, one))));
    } else {
      for (let i = 0; i < count; i++) results.push(await one());
    }
    return {results, log};
  } finally {
    await sql.end();
  }
}
