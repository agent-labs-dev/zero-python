// The Fetch-Request half of the wire tier: upstream's handlers given a `Request` as an app's HTTP route
// would give it, the response sent as `Response.json` would send it. tests/wire/test_http.py runs
// Python's Starlette routes on the same bytes.
import {defineMutator, defineMutators, mustGetMutator, mustGetQuery} from '@rocicorp/zero';
import {handleMutateRequest, handleQueryRequest} from '@rocicorp/zero/server';
import {mutators, provider, put, type Args, type Tx} from './mutate.ts';
import {queries} from './query.ts';
import {schema} from './schema.ts';

type Ctx = {userID: string | null};

// The twin of HTTP_REGISTRY in tests/wire/mutators.py.
const httpMutators = defineMutators({
  item: {
    put: defineMutator(async ({tx, args, ctx}: {tx: Tx; args: Args; ctx: Ctx}) =>
      put(tx, {...args, value: `${String(args?.value ?? '')}@${String(ctx.userID)}`}),
    ),
    appError: defineMutator(async ({tx, args}: {tx: Tx; args: Args}) => mutators['item.appError'](tx, args ?? {})),
    error: defineMutator(async ({tx, args}: {tx: Tx; args: Args}) => mutators['item.error'](tx, args ?? {})),
  },
} as never);

function post(path: string, search: string, body: string): Request {
  return new Request(`http://localhost${path}${search}`, {method: 'POST', body: Buffer.from(body, 'base64')});
}

/** What the client receives: the response through `JSON.stringify`. */
function sent(response: unknown): unknown {
  return JSON.parse(JSON.stringify(response));
}

export async function httpMutate({
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
  const ctx: Ctx = {userID};
  return sent(
    await handleMutateRequest({
      dbProvider: provider(url),
      handler: transact =>
        transact((tx, name, args) => mustGetMutator(httpMutators, name).fn({tx, args, ctx} as never)),
      request: post('/mutate', search, body),
      userID,
    }),
  );
}

export async function httpQuery({search, body, userID}: {search: string; body: string; userID: string | null}) {
  const ctx: Ctx = {userID};
  return sent(
    await handleQueryRequest({
      handler: (name, args) => mustGetQuery(queries, name).fn({args, ctx} as never),
      schema,
      request: post('/query', search, body),
      userID,
    }),
  );
}
