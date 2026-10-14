// The mutate half of the wire tier: the published handleMutateRequest over the postgres.js adapter,
// with mutators that tests/wire/mutators.py mirrors one for one.
import {createSchema, string, table} from '@rocicorp/zero';
import {
  ApplicationError,
  handleMutateRequest,
  type Database,
  type MutateRequestHandler,
  type TransactionProviderHooks,
  type TransactionProviderInput,
} from '@rocicorp/zero/server';
import {zeroPostgresJS} from '@rocicorp/zero/server/adapters/postgresjs';
import postgres from 'postgres';

const item = table('item').columns({id: string(), value: string()}).primaryKey('id');
export const wireSchema = createSchema({tables: [item]});

type Tx = {dbTransaction: {query(sql: string, params: unknown[]): Promise<unknown[]>}};
type Args = {id?: string; value?: string; message?: string; details?: unknown};

const PUT = `INSERT INTO item (id, value) VALUES ($1, $2)
  ON CONFLICT (id) DO UPDATE SET value = item.value || '+' || excluded.value`;

async function put(tx: Tx, args: Args) {
  await tx.dbTransaction.query(PUT, [String(args.id ?? 'x'), String(args.value ?? '')]);
}

const mutators: Record<string, (tx: Tx, args: Args) => Promise<void>> = {
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
  'item.dbError': async tx => {
    await tx.dbTransaction.query('SELECT * FROM missing_table', []);
  },
};

const handler: MutateRequestHandler<Database<Tx>> = async (transact, mutation) => {
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
    await mutator(tx, (mutatorArgs ?? {}) as Args);
  });
  if (mutation.name.startsWith('post.')) {
    throw new Error('after commit');
  }
  return result;
};

const providers = new Map<string, Database<Tx>>();

/** The database for `url`, failing to open any transaction for client `cfail`. */
function provider(url: string): Database<Tx> {
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
  return handleMutateRequest({dbProvider: provider(url), handler, query, body: body as never, userID, logLevel: 'error'});
}

export type {TransactionProviderHooks};
