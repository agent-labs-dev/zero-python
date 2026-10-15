// The wire tier's test schema. It has a table and columns renamed on the server, a compound primary
// key, one-hop relationships of both cardinalities, a compound correlation, two-hop relationships
// through a junction table, and a table with no relationships at all.
import {boolean, createSchema, json, number, relationships, string, table} from '@rocicorp/zero';

const user = table('user')
  .from('users')
  .columns({id: string(), name: string().from('full_name'), age: number().optional(), active: boolean()})
  .primaryKey('id');
const issue = table('issue')
  .columns({
    id: string(),
    ownerID: string().from('owner_id'),
    title: string(),
    meta: json().optional(),
    priority: number(),
    open: boolean(),
  })
  .primaryKey('id');
const comment = table('comment')
  .columns({id: string(), issueID: string().from('issue_id'), authorID: string().from('author_id'), body: string()})
  .primaryKey('id');
const label = table('label').columns({id: string(), name: string()}).primaryKey('id');
const issueLabel = table('issueLabel')
  .from('issue_label')
  .columns({issueID: string().from('issue_id'), labelID: string().from('label_id')})
  .primaryKey('issueID', 'labelID');
const audit = table('audit').columns({id: string(), at: number()}).primaryKey('id');

const userRelationships = relationships(user, ({many}) => ({
  issues: many({sourceField: ['id'], destField: ['ownerID'], destSchema: issue}),
}));
const issueRelationships = relationships(issue, ({one, many}) => ({
  owner: one({sourceField: ['ownerID'], destField: ['id'], destSchema: user}),
  comments: many({sourceField: ['id'], destField: ['issueID'], destSchema: comment}),
  ownComments: many({sourceField: ['id', 'ownerID'], destField: ['issueID', 'authorID'], destSchema: comment}),
  labels: many(
    {sourceField: ['id'], destField: ['issueID'], destSchema: issueLabel},
    {sourceField: ['labelID'], destField: ['id'], destSchema: label},
  ),
}));
const commentRelationships = relationships(comment, ({one}) => ({
  issue: one({sourceField: ['issueID'], destField: ['id'], destSchema: issue}),
  author: one({sourceField: ['authorID'], destField: ['id'], destSchema: user}),
}));
const labelRelationships = relationships(label, ({many}) => ({
  issues: many(
    {sourceField: ['id'], destField: ['labelID'], destSchema: issueLabel},
    {sourceField: ['issueID'], destField: ['id'], destSchema: issue},
  ),
}));

export const schema = createSchema({
  tables: [user, issue, comment, label, issueLabel, audit],
  relationships: [userRelationships, issueRelationships, commentRelationships, labelRelationships],
});
