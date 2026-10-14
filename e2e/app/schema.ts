// The end-to-end test app's schema. The client syncs it, the TypeScript reference server serves it, and
// the Python server loads it as JSON.
import {boolean, createSchema, json, number, relationships, string, table} from '@rocicorp/zero';

const user = table('user').from('users').columns({id: string(), name: string()}).primaryKey('id');
const project = table('project')
  .columns({id: string(), name: string(), ownerID: string().from('owner_id')})
  .primaryKey('id');
const task = table('task')
  .columns({
    id: string(),
    projectID: string().from('project_id'),
    title: string(),
    done: boolean(),
    priority: number(),
    tags: json<string[]>().optional(),
    due: number().optional(),
    createdAt: number().from('created_at'),
  })
  .primaryKey('id');
const label = table('label').columns({id: string(), name: string()}).primaryKey('id');
const taskLabel = table('taskLabel')
  .from('task_label')
  .columns({taskID: string().from('task_id'), labelID: string().from('label_id')})
  .primaryKey('taskID', 'labelID');

const projectRelationships = relationships(project, ({one, many}) => ({
  owner: one({sourceField: ['ownerID'], destField: ['id'], destSchema: user}),
  tasks: many({sourceField: ['id'], destField: ['projectID'], destSchema: task}),
}));
const taskRelationships = relationships(task, ({one, many}) => ({
  project: one({sourceField: ['projectID'], destField: ['id'], destSchema: project}),
  labels: many(
    {sourceField: ['id'], destField: ['taskID'], destSchema: taskLabel},
    {sourceField: ['labelID'], destField: ['id'], destSchema: label},
  ),
}));

export const schema = createSchema({
  tables: [user, project, task, label, taskLabel],
  relationships: [projectRelationships, taskRelationships],
});

export type Context = {userID: string};

declare module '@rocicorp/zero' {
  interface DefaultTypes {
    schema: typeof schema;
    context: Context;
  }
}
