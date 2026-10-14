# Port plan

## Goal

A Python backend can replace a TypeScript backend behind an unmodified Zero client, for one pinned
`@rocicorp/zero` version at a time, and later releases fast-follow each upstream release. The first pin is
**1.10.0**.

## Scope

In scope, mirroring these rocicorp/mono packages (about 10-15k TS lines):

- `zero-server`: `handleQueryRequest` / `handleTransformRequest`, `handleMutateRequest` / `PushProcessor`,
  the transaction provider and its two hooks (`updateClientMutationID`, `writeMutationResult`).
- `zero-protocol`: the wire types, including the query AST (`src/ast.ts`).
- `z2s`: ZQL compiled to Postgres SQL, which is what server-side ZQL (`tx.run(query)`) runs on.
- `zero-schema` / `zero-types`: the schema, client-to-server name mapping.
- The query builder from `zql` (`builder`, `query`). Its IVM engine and planner are client-side and out.
- `defineQueries` / `defineMutators` equivalents.

Out of scope: zero-client, replicache, zql's IVM, zero-cache, zqlite, UI bindings. A Python backend serves
the same TypeScript client a TS backend would.

The schema stays authored in TypeScript (`schema.ts`), because the client needs it. Python loads it as JSON
emitted from the TS schema. A Python schema DSL would be a second source of truth.

## The oracle

The published npm package at the pinned version is the definition of correct. The public rocicorp/mono repo
ships no `.test.ts` for zql, z2s, zero-server, zero-protocol or zero-cache, so there are no upstream tests to
port. Three tiers of differential tests, each comparing Python against the real package:

1. **Wire.** A small bun runner calls the real handlers. Hypothesis generates query args and mutation
   sequences, including gaps, duplicates, refusals and the `_zero_cleanupResults` mutation. Diff the
   responses and the rows written to the upstream schema (`zero_0.clients`, `zero_0.mutations`).
2. **SQL (z2s).** The same ZQL through TS and Python against the same Postgres. Compare result rows, never
   SQL text.
3. **End to end.** Docker compose: Postgres, the `rocicorp/zero:<pin>` image, a TS reference app server and
   the Python one, one scripted client. Diff the rows the client ends up with.

Python's own tests hold 100% branch coverage as a hygiene floor, on top of the differential tiers.

## Phases

Each phase is a PR. Its completion criterion is what makes it done.

1. **Scaffold.** `ZERO_VERSION` (1.10.0), CI running ruff, ty, pytest with coverage. Done when CI is green on
   an empty test suite.
2. **Seed.** Port the generic Zero code out of Nebula (see "Seed material"), free of Nebula imports. Add the
   AST operators Nebula never emitted (`<`, `>`, `LIKE`, `NOT LIKE`, `ILIKE`, `NOT ILIKE`). Done when every
   module in `zero-protocol/src/ast.ts` and the push/query schemas has a Python counterpart and no file imports
   anything outside this package.
3. **Mutate endpoint, one transaction.** `handle_mutate_request` runs the mutator inside the same transaction
   as the lastMutationID bump, as upstream does. First transaction provider: SQLAlchemy async. Done when the
   wire tier passes for every branch of `process-mutations.ts`: ok, app error, already processed, out of
   order, unsupported push version, cleanup bulk and single.
4. **Query endpoint.** `handle_query_request`, a `define_queries` registry, schema loading with name mapping.
   Done when the wire tier passes for generated queries over a test schema covering every AST node type.
5. **End-to-end tier** against 1.10.0. Done when the e2e diff is empty for the test app. This is the spike's
   finish line: the point to make the repo public and contact Rocicorp.
6. **z2s and `tx.run`.** Server-side ZQL against Postgres. Done when the SQL tier passes over generated queries
   covering every operator, `related`, `exists`/`not exists`, ordering, `limit` and `start` bounds.
7. **Builder ergonomics** and the remaining DB adapters (asyncpg, psycopg 3).
8. **Release automation.** Watch npm for new `@rocicorp/zero` versions, diff the in-scope packages between
   tags, port, run all three tiers, release with the new pin.

## Seed material

Nebula (`~/code/nebula`, `main`) already speaks this protocol in production. Read these as starting points,
not as the design:

- `src/models/zero/zql.py`: AST models and the transform envelope. Generic.
- `src/models/zero/push.py`: push wire models. Imports Nebula's settings for the upstream schema check.
- `src/modules/zero/push_processor.py`: the exactly-once state machine. Its persist commits in a separate
  session from the bookkeeping; that two-commit design is a Nebula shortcut, and phase 3 replaces it.
- `src/models/postgres/zero_bookkeeping.py`: SQL against the upstream schema's `clients` / `mutations`.
- `src/routers/zero.py`: the transform loop (`_transform_one`) and the query response shape.
- `packages/zero-harness/golden-ast.ts` and `tests/unit/test_zero_query_ast.py`: the fixture-based ancestor
  of the wire tier.
- `packages/zero-contract/emit.ts`: emits schema JSON from `schema.ts`; the starting point for schema loading.
- `.agents/skills/zero-sync/SKILL.md`: compatibility rules learned the hard way (subset rule, protocol
  version ordering, optionality).

Upstream source: rocicorp/mono, `packages/zero-server/src/process-mutations.ts`,
`packages/zero-server/src/queries/process-queries.ts`, `packages/zero-protocol/src/ast.ts`, `packages/z2s`.
Read it at the tag matching `ZERO_VERSION`.

## Decisions already made

- Name `zero-python` everywhere (repo, PyPI, import `zero_python`). `zero-py` is blocked on PyPI as too
  similar to the abandoned `zeropy`.
- Repo private until phase 5 passes; then public, then contact Rocicorp about a community listing and early
  notice of protocol changes.
- PyPI publishing by trusted publishing from `.github/workflows/release.yml`, environment `pypi`. The pending
  publisher on PyPI is not set up yet; the 0.0.1 placeholder is unpublished.
- Nebula will adopt the library and move its mutators to server-side ZQL. Zero has no after-commit hook, so
  Nebula's side effects need their own seam; that is Nebula's design work, not this library's.
