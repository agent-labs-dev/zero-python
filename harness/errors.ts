// What a mutator or query can throw, by name. tests/wire/errors.py raises each one's Python twin.
import {ApplicationError} from '@rocicorp/zero/server';

class NamedError extends Error {
  override name = 'NamedError';
}

export const shapes: Record<string, () => unknown> = {
  empty: () => new Error(''),
  cause: () => new Error('', {cause: new Error('inner')}),
  emptyCause: () => new Error('', {cause: new Error('')}),
  deepCause: () => new Error('', {cause: new Error('', {cause: new TypeError('deepest')})}),
  circular: () => {
    const e = new Error('') as Error & {cause?: unknown};
    e.cause = e;
    return e;
  },
  props: () => Object.assign(new Error(''), {code: 5, tags: ['a']}),
  unknown: () => Object.assign(new Error(''), {fn: () => 1}),
  details: () => Object.assign(new Error('m'), {details: {a: [1, null]}}),
  badDetails: () => Object.assign(new Error('m'), {details: () => 1}),
  nullDetails: () => Object.assign(new TypeError('m'), {details: null}),
  nanDetails: () => Object.assign(new Error('m'), {details: {x: NaN, y: Infinity}}),
  named: () => new NamedError('named'),
  numberMessage: () => new Error(5 as never),
  // Deeper than Pydantic's serializer goes.
  deepDetails: () => Object.assign(new Error('m'), {details: JSON.parse('['.repeat(300) + ']'.repeat(300))}),
  appErrorCause: () => new ApplicationError('', {cause: new Error('inner'), details: 0 as never}),
};
