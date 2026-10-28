"""What `tests/compat.py` leaves out of the proof, each with the reason.

`NOT_PORTED` maps an upstream module the port cites to its regions no differential test reaches: a function
by its name, a line range `"start-end"` of the TypeScript at the pinned version, or `"*"` for all of it. A
region that turns out covered fails the check, so the list only shrinks. The reasons fall into five kinds:

- **Unused.** Exported, but no handler, builder or registry path the port serves calls it.
- **Deprecated.** Marked `@deprecated` upstream and not ported.
- **JavaScript only.** A value or call Python cannot produce: a thrown non-`Error`, a `URLSearchParams`, a
  positional overload, a TypeScript typing helper.
- **Client only.** Part of the client's query API, which the server never runs.
- **Unreachable.** A guard or arm the code around it already rules out, named with what rules it out.

`PYTHON_ONLY` lists `zero_server` modules with no upstream counterpart. Lines within a ported module that
have none are marked in place with `# python-only: <reason>`.
"""

_TYPING_HELPER = "JavaScript only: a TypeScript typing helper that returns its argument; Python needs none"
_DEPRECATED_POSITIONAL = "deprecated: the positional call forms, which the port's keyword signatures replace"
_PARSE_DETAILS = (
    "unreachable: a parse failure is a TypeError or SyntaxError, so its details always name it; the arm "
    "with details is compared"
)

# valita is a dependency; `shared/valita.py` ports the part of it the wire schemas run.
_VALITA_MESSAGE = "unused: valita's own error message; Zero's getMessage builds one from the first issue"
_VALITA_CUSTOM = "unreachable: a custom error, which only jsonSchema raises, for a value that is not JSON"
_VALITA_UNUSED = "unused: a validator or modifier none of the ported schemas uses"
_VALITA_BITS = "unreachable: the bit set grows past one number only for an object of more than 32 keys"
_VALITA_COPY = "unused: copying a value a transform changed; the ported schemas transform nothing"
_VALITA_UNION = "unreachable: a union shape the ported schemas do not build (optional or unknown options, a "
_VALITA_UNION += "literal sharing a type with another option, or an ambiguous discriminator)"

_ZERO_CACHE_PARSE = "unused: zero-cache's parsing of client messages from newer clients; no handler parses with it"
_AST_DEPTH = "unused: the depth bound zero-cache puts on ASTs from clients; the server parses none"

NOT_PORTED: dict[str, dict[str, str]] = {
    "shared/src/asserts.ts": {
        "7-56": "unused: assertString through assertNotNull; only `assert` and `unreachable` run in ported code",
        "assertNotUndefined": "unused",
    },
    "shared/src/error.ts": {
        "10-10": "JavaScript only: a thrown string",
        "44-44": "JavaScript only: a thrown object that is not an Error but has a message",
        "getErrorCauses": "unused: the client logs an error's causes with it",
        "readCause": "unused: only getErrorCauses calls it",
        "109-119": "JavaScript only: details read off a thrown value that is not an Error",
    },
    "shared/src/json-schema.ts": {
        "17-17": "unreachable: `skipAssertJSONValue` is a build flag, false in the published package",
        "32-43": "unused: jsonObjectSchema, which no ported schema uses",
    },
    "shared/src/json.ts": {
        "deepEqual": "client only: only the builder's query interning compares with it",
        "assertJSONValue": "unused",
        "assertJSONObject": "unused",
        "assertObjectIsJSONObject": "unused",
        "assertJSONArray": "unused",
        "isJSONObject": "unused: only jsonObjectSchema calls it",
        "237-237": "unreachable: an array element that is not JSON, which no decoded body or Python value has",
    },
    "shared/src/object-traversal.ts": {
        "iterateLeaves": "unused: only iterateMutators, itself unused, calls it",
    },
    "shared/src/valita.ts": {
        "13-14": "JavaScript only: a bigint, which JSON cannot carry",
        "81-84": "unreachable: a missing value always has a path, the property it is missing at",
        "112-113": "unreachable: an invalid union below the top level; every nested union of the ported "
        "schemas is told apart by type or by a literal key, so only a top-level union fails as a union",
        "101-108": "unreachable: an undeclared key reported; transform requests are parsed in strip mode and "
        "push bodies in passthrough, so no request is refused for one",
        "115-116": "unreachable: a custom error, which only jsonSchema raises, for a value that is not JSON",
        "145-148": "unreachable: a union value JSON.stringify cannot write, which no decoded body is",
        "is": "unused",
        "assert": "unused",
        "instanceOfAbstractType": "unused",
        "deepPartial": "unused",
        "toTerminals": _ZERO_CACHE_PARSE,
        "assertUnrecognized": _ZERO_CACHE_PARSE,
        "taggedTupleUnion": _ZERO_CACHE_PARSE,
        "taggedObjectUnion": _ZERO_CACHE_PARSE,
    },
    "z2s/src/compiler.ts": {
        "100-100": "unreachable: the builder gives every related subquery an alias",
        "167-167": "unreachable: the builder completes every ordering with the primary key",
        "194-194": "unreachable: called only for a query with a start bound",
        "579-581": "unreachable: TypeScript's exhaustiveness check after every value position type",
        "622-627": "unreachable: the other side is a null literal, whose own comparison fails first with "
        "'Literal of unexpected type'",
        "641-642": "unreachable: TypeScript's exhaustiveness check after every value position type",
    },
    "z2s/src/sql.ts": {
        "347-355": "unused: identifiers that are not strings, which no ported query builds",
    },
    "zero-cache/src/services/mutagen/error.ts": {
        "10-11": "unreachable: the assertion message, built only when received >= actual, which the caller rules out",
    },
    "zero-protocol/src/ast.ts": {
        "813-813": "unreachable: after flattening, a compound condition's compound siblings share one type",
        "exceedsASTDepth": _AST_DEPTH,
        "astExceedsDepth": _AST_DEPTH,
        "conditionExceedsDepth": _AST_DEPTH,
        "274-276": _AST_DEPTH,
        "compareUTF8MaybeNull": "unreachable: the null arms; every op and type it compares is a string",
    },
    "zero-protocol/src/error.ts": {
        "162-177": "unused: ProtocolError and isProtocolError, which the client raises and the server never does",
    },
    "zero-protocol/src/query-server.ts": {
        "*": "unused: the response schema zero-cache parses with; the handlers import only its types, and "
        "every response is compared with upstream's own",
    },
    "zero-protocol/src/mutation.ts": {
        "mapCRUD": "unused: zero-cache renames CRUD mutations; the server receives none to rename",
    },
    "zero-schema/src/name-mapper.ts": {
        "serverToClient": "unused: the server maps only client names to server names",
        "32-34": "unused: the server-to-client arm of createMapperFrom",
        "38-38": "unused: the server-to-client arm of createMapperFrom",
        "41-41": "unused: the server-to-client arm of createMapperFrom",
        "validator": "unused",
    },
    "zero-server/src/custom.ts": {
        "85-112": "deprecated: the delegate behind `tx.query`, the legacy query builder (enableLegacyQueries)",
        "makeSchemaCRUD": "deprecated",
        "makeServerCRUDExecutor": "unused: only makeServerTransaction and makeSchemaCRUD call it; the "
        "adapters build their executor through CRUDMutatorFactory, which is compared",
    },
    "zero-server/src/pg-query-executor.ts": {
        "30-31": "JavaScript only: a DBTransaction whose query returns an iterable that is not an array; "
        "Python's DBTransaction returns a list",
    },
    "zero-server/src/process-mutations.ts": {
        "278-278": _PARSE_DETAILS,
        "304-304": _PARSE_DETAILS,
        "327-327": _PARSE_DETAILS,
        "487-488": "JavaScript only: a URLSearchParams instance; the port takes a mapping",
        "518-521": _DEPRECATED_POSITIONAL,
        "537-541": _DEPRECATED_POSITIONAL,
        "669-669": "unreachable: a DatabaseTransactionError always has a cause",
        "713-714": "unreachable: the callback a pre-transaction failure passes, never called because an "
        "application error is recorded instead",
        "getMutation": "deprecated: use getMutator",
        "getObjectAtPath": "deprecated: only getMutation calls it",
    },
    "zero-server/src/queries/process-queries.ts": {
        "handleGetQueriesRequest": "deprecated",
        "handleTransformRequest": "deprecated",
        "161-167": _DEPRECATED_POSITIONAL,
        "226-226": _DEPRECATED_POSITIONAL,
        "261-261": _PARSE_DETAILS,
        "306-306": "deprecated: a response with no userID, which only the positional call forms produce",
        "318-318": "unreachable: an internal failure is a plain Error from mapping the AST or asserting "
        "query internals, which carries no details",
    },
    "zero-server/src/schema.ts": {
        "209-209": "unreachable: called only when there is an incompatibility to report",
    },
    "zero-types/src/name-mapper.ts": {
        "tableNameIfKnown": "unused",
        "row": "unused: only mapCRUD calls it",
        "columns": "unused: only mapCRUD calls it",
    },
    "zql/src/mutate/crud.ts": {
        "makeCRUDMutate": "deprecated: only makeSchemaCRUD calls it",
    },
    "zql/src/mutate/custom.ts": {
        "customMutatorKey": "unused: the client names legacy mutators with it",
    },
    "zql/src/mutate/mutator-registry.ts": {
        "defineMutatorsWithType": _TYPING_HELPER,
        "iterateMutators": "unused",
    },
    "zql/src/mutate/mutator.ts": {
        "defineMutatorWithType": _TYPING_HELPER,
    },
    "zql/src/query/complete-ordering.ts": {
        "assertOrderingIncludesPK": "unused",
        "51-51": "unreachable: every caller passes a condition",
    },
    "zql/src/query/create-builder.ts": {
        "createRunnableBuilder": "deprecated: builds `tx.query`, the legacy query builder",
    },
    "zql/src/query/query-impl.ts": {
        "newQueryImpl": "unused: a query from an AST built elsewhere; the server builds every query itself",
        "150-152": "unused: reusing an interned root query, which only saves work; the harness builds each "
        "program from a fresh schema so that no program's result depends on the ones before it",
        "326-347": "client only: run, preload and materialize, which throw on a query that is not runnable; "
        "the server runs a query with `tx.run`",
        "hash": "client only: the client caches views by AST hash; the server sends ASTs unhashed",
        "392-425": "client only: #canonicalize and #sameAs, which only hash calls",
        "616-619": "unreachable: a relationship has one hop or two, which the schema builder enforces",
        "895-898": "unreachable: a relationship has one hop or two, which the schema builder enforces",
        "1090-1091": "unreachable: TypeScript's exhaustiveness check after every condition type",
        "throwQueryNotRunnable": "client only: called only by run, preload and materialize",
    },
    "zql/src/query/query-registry.ts": {
        "addContextToQuery": "unused: not exported by the published package",
        "defineQueryWithType": _TYPING_HELPER,
        "defineQueriesWithType": _TYPING_HELPER,
    },
    "zql/src/query/validate-input.ts": {
        "40-40": "unreachable: every caller checks for a validator first",
    },
    "@databases/escape-identifier/lib/index.js": {
        "14-15": "unused: the extended (Unicode) mode, which z2s never asks for",
        "escapeMySqlIdentifier": "unused",
        "escapeSQLiteIdentifier": "unused: zqlite's escaper",
        "71-72": "unused: the message variant for SQLite",
        "assertUnicode": "unused: the extended mode",
    },
    "@databases/validate-unicode/lib/index.js": {
        "removeInvalidUnicode": "unused",
        "28-28": "unreachable: every identifier is a string",
    },
    "@badrap/valita/dist/index.js": {
        "25-25": _VALITA_CUSTOM,
        "59-61": _VALITA_MESSAGE,
        "143-150": _VALITA_MESSAGE,
        "182-182": "JavaScript only: a bigint literal in valita's own message",
        "countIssues": _VALITA_MESSAGE,
        "formatIssueTree": _VALITA_MESSAGE,
        "259-263": _VALITA_MESSAGE,
        "289-293": _VALITA_MESSAGE,
        "344-345": _VALITA_UNUSED,
        "350-351": "JavaScript only: a bigint, which JSON cannot carry",
        "355-357": "unreachable: a null or undefined type is only ever a union option, which the union "
        "picks by the value's type before it is tried",
        "368-369": _VALITA_UNUSED,
        "379-384": "unused: the Standard Schema interface",
        "409-415": "unused: a parse mode passed to a type's own `parse`; Zero's wrapper passes it to `try`",
        "map": _VALITA_UNUSED,
        "479-480": _VALITA_UNUSED,
        "as": _VALITA_UNUSED,
        "515-515": _VALITA_UNUSED,
        "522-524": _VALITA_UNUSED,
        "539-558": _VALITA_UNUSED,
        "577-584": _VALITA_UNUSED,
        "588-589": "unreachable: a matcher built twice; each type's is built once and kept",
        "605-611": _VALITA_BITS,
        "615-617": _VALITA_BITS,
        "623-626": _VALITA_BITS,
        "rest": _VALITA_UNUSED,
        "pick": _VALITA_UNUSED,
        "omit": _VALITA_UNUSED,
        "partial": _VALITA_UNUSED,
        "676-681": "JavaScript only: a `__proto__` key, which a Python dict holds as any other",
        "722-726": _VALITA_COPY,
        "735-736": "unused: the fast path for `record(unknown())`, which no ported schema has",
        "857-865": "unused: array length messages for a shape no ported schema has; Zero words its own",
        "890-890": _VALITA_UNUSED,
        "917-931": "unused: concatenations other than a tuple followed by an array",
        "962-966": _VALITA_UNION,
        "985-985": _VALITA_UNION,
        "989-991": _VALITA_UNION,
        "1016-1021": _VALITA_UNION,
        "1032-1053": _VALITA_UNION,
        "1105-1105": _VALITA_UNION,
        "1189-1198": _VALITA_COPY,
        "1318-1319": "unreachable: a matcher built twice; each type's is built once and kept",
        "1343-1343": _VALITA_UNUSED,
        "1350-1350": _VALITA_UNUSED,
    },
}

PYTHON_ONLY: dict[str, str] = {
    "codegen.py": "`zero-server codegen`, which writes typed Python helpers from the schema; upstream's types "
    "come from TypeScript inference",
}
