// The scripted client: an unmodified Zero client that writes through the app's mutators, then reads
// through its queries, and prints everything it ended up with. Run once per app server; the outputs
// must be identical.
//
//   node client.ts http://localhost:4848
import {Zero} from '@rocicorp/zero';
import {mutators} from './app/mutators.ts';
import {queries} from './app/queries.ts';
import {schema} from './app/schema.ts';

const server = process.argv[2];

function connect(userID: string) {
  return new Zero({
    server,
    userID,
    auth: userID,
    schema,
    mutators,
    context: {userID},
    kvStore: 'mem',
    logLevel: 'error',
  });
}

// oxlint-disable-next-line no-explicit-any
async function settle(z: any, request: unknown) {
  const pending = z.mutate(request);
  const client = await pending.client;
  const result = await pending.server;
  // Both halves go to the run's log: a refusal that already happened on the client points at the app.
  console.error(JSON.stringify({client, server: result}));
  return result.type === 'error' ? {error: result.error} : {ok: true};
}

const MS = Date.UTC(2026, 0, 1);
const ada = connect('ada');
const steps: [string, unknown][] = [
  ['register', mutators.user.register({name: 'Ada'})],
  ['project p1', mutators.project.create({id: 'p1', name: 'Alpha'})],
  ['project p2', mutators.project.create({id: 'p2', name: 'archived beta'})],
  ['project blank', mutators.project.create({id: 'p3', name: '  '})],
  ...[1, 2, 3, 4, 5].map(
    n =>
      [
        `task t${n}`,
        mutators.task.create({
          id: `t${n}`,
          projectID: n === 5 ? 'p2' : 'p1',
          title: ['write spec', 'Fix bug', 'ship it', 'triage', 'archive'][n - 1],
          priority: n % 3,
          tags: n % 2 ? ['a', `n${n}`] : undefined,
          due: n === 1 ? MS + 86_400_000 : undefined,
          createdAt: MS + n * 1000,
        }),
      ] as [string, unknown],
  ),
  ['label bug', mutators.label.upsert({id: 'l1', name: 'bug'})],
  ['label ops', mutators.label.upsert({id: 'l2', name: 'ops'})],
  ['attach t2 bug', mutators.label.attach({taskID: 't2', labelID: 'l1'})],
  ['attach t3 ops', mutators.label.attach({taskID: 't3', labelID: 'l2'})],
  ['attach t3 bug', mutators.label.attach({taskID: 't3', labelID: 'l1'})],
  ['toggle t2', mutators.task.toggle({id: 't2', done: true})],
  ['retitle t3 refused', mutators.task.retitle({id: 't3', title: 'forbidden fruit'})],
  ['retitle t4', mutators.task.retitle({id: 't4', title: 'Triage inbox'})],
  ['remove t5', mutators.task.remove({id: 't5'})],
  ['rename p1', mutators.project.rename({id: 'p1', name: 'Alpha prime'})],
];

const mutations: Record<string, unknown> = {};
for (const [name, request] of steps) {
  mutations[name] = await settle(ada, request);
}

// oxlint-disable-next-line no-explicit-any
async function read(z: any, query: unknown) {
  return z.run(query, {type: 'complete'});
}

const grace = connect('grace');
mutations['grace task in ada project'] = await settle(
  grace,
  mutators.task.create({id: 'tg', projectID: 'p1', title: 'sneaky', priority: 1, createdAt: MS}),
);

const reads = {
  'ada projects.mine': await read(ada, queries.projects.mine()),
  'ada tasks.open p1': await read(ada, queries.tasks.open({projectID: 'p1'})),
  'ada tasks.open p2': await read(ada, queries.tasks.open({projectID: 'p2'})),
  'ada tasks.search': await read(ada, queries.tasks.search({text: 'I', labels: ['bug']})),
  'ada tasks.byID t1': await read(ada, queries.tasks.byID({id: 't1'})),
  'ada tasks.byID t5': (await read(ada, queries.tasks.byID({id: 't5'}))) ?? null,
  'grace projects.mine': await read(grace, queries.projects.mine()),
  'grace tasks.byID t3': await read(grace, queries.tasks.byID({id: 't3'})),
};

await ada.close();
await grace.close();
process.stdout.write(JSON.stringify({mutations, reads}, null, 2) + '\n');
process.exit(0);
