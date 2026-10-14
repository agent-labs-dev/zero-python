import {ApplicationError, defineMutator, defineMutators} from '@rocicorp/zero';

type ProjectArgs = {id: string; name: string};
type TaskArgs = {
  id: string;
  projectID: string;
  title: string;
  priority: number;
  tags?: string[] | undefined;
  due?: number | undefined;
  createdAt: number;
};

export const mutators = defineMutators({
  user: {
    register: defineMutator(async ({tx, ctx, args}: {tx: never; ctx: {userID: string}; args: {name: string}}) => {
      // oxlint-disable-next-line no-explicit-any
      await (tx as any).mutate.user.upsert({id: ctx.userID, name: args.name});
    }),
  },
  project: {
    create: defineMutator(async ({tx, ctx, args}: {tx: never; ctx: {userID: string}; args: ProjectArgs}) => {
      if (args.name.trim() === '') {
        throw new ApplicationError('A project needs a name', {details: {field: 'name'}});
      }
      // oxlint-disable-next-line no-explicit-any
      await (tx as any).mutate.project.insert({id: args.id, name: args.name, ownerID: ctx.userID});
    }),
    rename: defineMutator(async ({tx, args}: {tx: never; args: ProjectArgs}) => {
      // oxlint-disable-next-line no-explicit-any
      await (tx as any).mutate.project.update({id: args.id, name: args.name});
    }),
  },
  task: {
    create: defineMutator(async ({tx, args}: {tx: never; args: TaskArgs}) => {
      // oxlint-disable-next-line no-explicit-any
      await (tx as any).mutate.task.insert({...args, done: false});
    }),
    toggle: defineMutator(async ({tx, args}: {tx: never; args: {id: string; done: boolean}}) => {
      // oxlint-disable-next-line no-explicit-any
      await (tx as any).mutate.task.update({id: args.id, done: args.done});
    }),
    retitle: defineMutator(async ({tx, args}: {tx: {location: string}; args: {id: string; title: string}}) => {
      // The server refuses what the client allowed, so the client must roll its optimistic write back.
      if (tx.location === 'server' && args.title.includes('forbidden')) {
        throw new ApplicationError('That title is not allowed', {details: {title: args.title}});
      }
      // oxlint-disable-next-line no-explicit-any
      await (tx as any).mutate.task.update({id: args.id, title: args.title});
    }),
    remove: defineMutator(async ({tx, args}: {tx: never; args: {id: string}}) => {
      // oxlint-disable-next-line no-explicit-any
      await (tx as any).mutate.task.delete({id: args.id});
    }),
  },
  label: {
    upsert: defineMutator(async ({tx, args}: {tx: never; args: {id: string; name: string}}) => {
      // oxlint-disable-next-line no-explicit-any
      await (tx as any).mutate.label.upsert(args);
    }),
    attach: defineMutator(async ({tx, args}: {tx: never; args: {taskID: string; labelID: string}}) => {
      // oxlint-disable-next-line no-explicit-any
      await (tx as any).mutate.taskLabel.upsert(args);
    }),
  },
});
