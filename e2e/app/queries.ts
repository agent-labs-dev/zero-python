import {createBuilder, defineQueries, defineQuery} from '@rocicorp/zero';
import {schema} from './schema.ts';

export const zql = createBuilder(schema);

type ProjectArgs = {projectID: string};
type SearchArgs = {text: string; labels: string[]};
type IDArgs = {id: string};

export const queries = defineQueries({
  projects: {
    // Each of the caller's projects with its number of open tasks.
    stats: defineQuery(({ctx}) =>
      zql.project
        .where('ownerID', ctx.userID)
        .related('tasks', q => q.where('done', false).count())
        .orderBy('id', 'asc'),
    ),
    // Only the caller's own projects: the server decides who the caller is.
    mine: defineQuery(({ctx}) =>
      zql.project
        .where('ownerID', ctx.userID)
        .related('owner')
        .related('tasks', q => q.orderBy('priority', 'desc').orderBy('id', 'asc').related('labels'))
        .orderBy('name', 'asc'),
    ),
  },
  tasks: {
    open: defineQuery(({args}: {args: ProjectArgs}) =>
      zql.task
        .where('projectID', args.projectID)
        .where('done', false)
        .whereExists('project', p => p.where('name', 'NOT LIKE', 'archived%'))
        .orderBy('title', 'asc')
        .limit(10),
    ),
    search: defineQuery(({args}: {args: SearchArgs}) =>
      zql.task
        .where(({or, cmp, exists}) =>
          or(
            cmp('title', 'ILIKE', `%${args.text}%`),
            exists('labels', l => l.where('name', 'IN', args.labels)),
          ),
        )
        .where('priority', '>=', 1)
        .orderBy('priority', 'asc')
        .orderBy('id', 'asc'),
    ),
    byID: defineQuery(({args}: {args: IDArgs}) => zql.task.where('id', args.id).related('project').one()),
    // The number of tasks at each priority, and the tasks whose first tag is `a`, with their labels counted.
    byPriority: defineQuery(() => zql.task.groupBy('priority').count().orderBy('priority', 'desc')),
    tagged: defineQuery(() =>
      zql.task
        .where(({cmp, json}) => cmp(json('tags', 0), '=', 'a'))
        .related('labels', q => q.count())
        .orderBy('id', 'asc'),
    ),
  },
});
