import {
  NameMapper,
  astSchema,
  mapAST,
  mapCondition,
  cleanupResultsArgSchema,
  mutateParamsSchema,
  normalizeAST,
  pushBodySchema,
  transformRequestMessageSchema,
  v,
} from './zero.ts';
import {ApplicationError, isApplicationError} from '@rocicorp/zero/server';
import {mutate, mutateRequest, mutateWithSchema, pushProcessor, pushProcessorRequest, wireSchema} from './mutate.ts';
import {query, queryRequest, rawQuery, runQuery, schemaJSON} from './query.ts';
import {httpMutate, httpQuery} from './http.ts';
import {registryLookup} from './registry.ts';

// The schemas upstream parses requests with, and the mode it parses each in.
const schemas = {
  ast: [astSchema, undefined],
  pushBody: [pushBodySchema, 'passthrough'],
  mutateParams: [mutateParamsSchema, 'passthrough'],
  transformRequest: [transformRequestMessageSchema, undefined],
  cleanupResultsArg: [cleanupResultsArgSchema, undefined],
} as const;

/** `value` with each infinite number as `{$nonFinite: 'Infinity'}`, which JSON can carry back to Python. */
function tagNonFinite(value: unknown): unknown {
  if (typeof value === 'number') return Number.isFinite(value) ? value : {$nonFinite: String(value)};
  if (Array.isArray(value)) return value.map(tagNonFinite);
  if (typeof value === 'object' && value !== null) {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, tagNonFinite(v)]));
  }
  return value;
}

export const ops = {
  mutate,
  mutateRequest,
  pushProcessor,
  pushProcessorRequest,
  mutateWithSchema,
  httpMutate,
  query,
  queryRequest,
  httpQuery,
  rawQuery,
  registryLookup,
  runQuery,
  schema: schemaJSON,
  wireSchema: () => JSON.parse(JSON.stringify(wireSchema)),

  /** Whether upstream accepts `value` as a `schema`. */
  accepts({schema, value}: {schema: keyof typeof schemas; value: unknown}) {
    const [type, mode] = schemas[schema];
    return v.test(value, type as v.Type<unknown>, mode).ok;
  },

  /** The message `v.parse` throws for `value` as a `schema`, or null when it parses. */
  refusal({schema, value}: {schema: keyof typeof schemas; value: unknown}) {
    const [type, mode] = schemas[schema];
    const result = v.test(value, type as v.Type<unknown>, mode);
    return result.ok ? null : result.error;
  },

  /** `JSON.parse(text)`, or the message it throws. */
  jsonParse({text}: {text: string}) {
    try {
      return {value: tagNonFinite(JSON.parse(text))};
    } catch (e) {
      return {error: (e as Error).message};
    }
  },

  /** `await request.json()` for a POST whose body is `body`, base64-encoded. */
  async requestJSON({body}: {body: string}) {
    const request = new Request('http://localhost/', {method: 'POST', body: Buffer.from(body, 'base64')});
    try {
      return {value: tagNonFinite(await request.json())};
    } catch (e) {
      return {error: (e as Error).message, name: (e as Error).name};
    }
  },

  /** `JSON.stringify(value)`. */
  jsonStringify({value}: {value: unknown}) {
    return JSON.stringify(value);
  },

  /** What an ApplicationError says about itself, and whether `isApplicationError` takes it. */
  applicationError({message, details}: {message: string; details?: unknown}) {
    const error = new ApplicationError(message, {details: details as never});
    return {
      kind: error.kind,
      name: error.name,
      message: error.message,
      details: error.details ?? null,
      isApplicationError: isApplicationError(error),
      plainIsApplicationError: isApplicationError(new Error(message)),
    };
  },

  /** `String(Number(text))`, for numbers JSON cannot carry. */
  numberString({text}: {text: string}) {
    return String(Number(text));
  },

  /** `String(value)`, for a JSON scalar or array. */
  jsString({value}: {value: unknown}) {
    return String(value);
  },

  /** `mapCondition` through a mapper built as `mapAST`'s below is. */
  mapCondition({
    condition,
    table,
    tables,
  }: {
    condition: unknown;
    table: string;
    tables: Record<string, {tableName: string; columns: Record<string, string>}>;
  }) {
    const mapper = new NameMapper(
      new Map(Object.entries(tables).map(([t, d]) => [t, {...d, allColumnsSame: false}])),
    );
    const ast = v.parse({table, where: condition}, astSchema);
    return mapCondition(ast.where!, table, mapper);
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
