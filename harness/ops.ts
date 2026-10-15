import {
  NameMapper,
  astSchema,
  mapAST,
  cleanupResultsArgSchema,
  mutateParamsSchema,
  normalizeAST,
  pushBodySchema,
  transformRequestMessageSchema,
  v,
} from './zero.ts';
import {mutate} from './mutate.ts';

// The schemas upstream parses requests with, and the mode it parses each in.
const schemas = {
  ast: [astSchema, undefined],
  pushBody: [pushBodySchema, 'passthrough'],
  mutateParams: [mutateParamsSchema, 'passthrough'],
  transformRequest: [transformRequestMessageSchema, undefined],
  cleanupResultsArg: [cleanupResultsArgSchema, undefined],
} as const;

export const ops = {
  mutate,

  /** Whether upstream accepts `value` as a `schema`. */
  accepts({schema, value}: {schema: keyof typeof schemas; value: unknown}) {
    const [type, mode] = schemas[schema];
    return v.test(value, type as v.Type<unknown>, mode).ok;
  },

  /** `String(value)`, for a JSON scalar or array. */
  jsString({value}: {value: unknown}) {
    return String(value);
  },

  /** `mapAST` through a mapper built from `{table: {tableName, columns}}`. */
  mapAST({ast, tables}: {ast: unknown; tables: Record<string, {tableName: string; columns: Record<string, string>}>}) {
    const mapper = new NameMapper(
      new Map(Object.entries(tables).map(([t, d]) => [t, {...d, allColumnsSame: false}])),
    );
    return mapAST(v.parse(ast, astSchema), mapper);
  },

  /** `items.sort` under a comparator given as a table, which need not be consistent. */
  sortWith({items, table}: {items: number[]; table: Record<string, number>}) {
    return [...items].sort((a, b) => table[`${a},${b}`] ?? 0);
  },

  normalizeAST({ast}: {ast: unknown}) {
    return normalizeAST(v.parse(ast, astSchema));
  },
};
