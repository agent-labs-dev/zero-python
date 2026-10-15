// The TypeScript reference app server: the published @rocicorp/zero/server over postgres.js.
import {createServer} from 'node:http';
import {mustGetMutator, mustGetQuery} from '@rocicorp/zero';
import {handleMutateRequest, handleQueryRequest} from '@rocicorp/zero/server';
import {zeroPostgresJS} from '@rocicorp/zero/server/adapters/postgresjs';
import {mutators} from './app/mutators.ts';
import {queries} from './app/queries.ts';
import {schema} from './app/schema.ts';

const db = zeroPostgresJS(schema, process.env.UPSTREAM_DB!);
const port = Number(process.env.PORT ?? 3001);

/** The test app trusts the bearer token as the user ID. A real app would verify it. */
function userIDOf(authorization: string | undefined): string | null {
  return authorization?.startsWith('Bearer ') ? authorization.slice('Bearer '.length) : null;
}

createServer(async (req, res) => {
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(chunk as Buffer);
  const url = new URL(req.url!, 'http://localhost');
  const userID = userIDOf(req.headers.authorization);
  const ctx = {userID: userID ?? 'anon'};
  const query = Object.fromEntries(url.searchParams);
  const body = JSON.parse(Buffer.concat(chunks).toString() || 'null');

  let response: unknown;
  if (url.pathname === '/query') {
    response = await handleQueryRequest({
      handler: (name, args) => mustGetQuery(queries, name).fn({args, ctx} as never),
      schema,
      query,
      body,
      userID,
    });
  } else if (url.pathname === '/mutate') {
    response = await handleMutateRequest({
      dbProvider: db,
      handler: transact =>
        transact((tx, name, args) => mustGetMutator(mutators, name).fn({tx, ctx, args} as never)),
      query,
      body,
      userID,
    });
  } else {
    res.writeHead(404).end();
    return;
  }
  res.writeHead(200, {'Content-Type': 'application/json'}).end(JSON.stringify(response));
}).listen(port, () => console.error(`ts app server on ${port}`));
