# The compatibility proof

zero-server claims to behave like the server half of `@rocicorp/zero` at the version in `ZERO_VERSION`. This
page says what that claim covers, how CI proves it on every push, and where it stops.

```sh
uv run pytest && uv run python -m tests.compat
```

CI runs both in the `check` job, which has five minutes to finish.

## What "compatible" means here

The differential tests send the same input through the published npm package and through Python, and diff
everything an observer could see: the response body, every row either side wrote to Postgres, and the
message of every error. Parse failures are compared word for word, including valita's description of the
bad field and V8's `JSON.parse` message for a body that is not JSON.

A passing diff only proves the inputs that were tried. `tests/compat.py` checks that the inputs were enough,
from two directions:

1. **Upstream coverage.** The harness records V8 coverage of the npm package while the tests run, and c8
   maps it back to the TypeScript through the package's source maps. Every line and branch of every
   upstream module a port docstring cites must have run. Citing a module is how a port says what it
   reproduces, so the scope follows the code with no separate list to drift. That includes the npm
   package's own dependencies where a port reproduces them: valita, which words parse failures, and
   `@databases/escape-identifier`, which refuses identifiers Postgres would take. A dependency file c8
   cannot map back to its source is read from V8's coverage directly.
2. **Port coverage.** Every line and branch of `zero_server` must have run during a differential test in
   `tests/wire`. Unit tests do not count. A Python branch only a unit test reaches is behaviour nothing
   compared with upstream.

When either check finds a gap, it prints the uncovered source. The fix is a differential test that reaches
it, or an entry saying why no test can.

## What is left out, and why

`tests/compat_scope.py` lists every upstream region the proof skips, each with its reason. A region that
turns out covered fails the check, so that list can only shrink. The reasons come in five kinds:

- Unused. Exported, but nothing the port serves calls it, such as `deepEqual` or `ProtocolError`.
- Deprecated. Marked `@deprecated` upstream: `getMutation`, `makeSchemaCRUD`, `handleMutationRequest`,
  `handleGetQueriesRequest`, `handleTransformRequest`, the positional overloads of the handlers, and the
  legacy `tx.query` builder. None of these are ported.
- JavaScript only. Values Python cannot produce: a thrown string, a `URLSearchParams`, a TypeScript typing
  helper like `defineQueryWithType`.
- Client only. `run`, `preload`, `materialize` and `hash` on a query.
- Unreachable. A guard the surrounding code already rules out. Each entry names what rules it out.

On the Python side, code with no upstream counterpart carries `# python-only: <reason>` on the line, and
`codegen.py` is excluded whole. The Starlette routes, Pydantic validators and `strict` builders are the
main cases.

## Known differences

These are the cases where the proof holds Python to something narrower than "identical", and why.

- **A circular error cause.** Upstream logs a failed mutation's error at warn level, and its logger follows
  a cause that points back at the error until the stack overflows. At the default log level that mutation
  then fails with `Maximum call stack size exceeded` and its ID never advances. Below warn level upstream
  reports it as Python does, which `test_circular_cause_matches_upstream` compares. This is an upstream
  bug, so the port does not copy it.
- **User code.** The test apps on each side must fail the same way for a comparison to mean anything, so
  the tests keep to failures both languages word alike. One example: upstream's `mustGetMutator` returns a
  namespace for a name like `issue`, and so does Python's. The app's `.fn(...)` call on it then fails in
  each language's own words.
- **Two failures in one query batch.** Upstream runs a batch's handlers together under `Promise.all`, and
  so does Python. When two of them fail the whole batch, which failure is reported depends on how each
  runtime orders its promise callbacks, and the two runtimes can disagree.
- **Interned queries.** Upstream's builder hands back the query it built before when a step repeats one,
  keyed so that some values JavaScript's types rule out share a key: a `flip` of `'yes'` with `true`, any
  `orderBy` direction but `'asc'` with `'desc'`. In one process, such a value can come back as whichever
  was built first. Python builds what it is given. The harness gives each program a fresh schema, which
  upstream interns by, so that no comparison depends on the programs before it.
- **A mutator returning `null`.** Upstream records a mutator's return value as result data unless it is
  `undefined`, so a TypeScript mutator can return `null` as data. Python has only `None`, which the port
  takes as returning nothing, as the tests' mutators never return `null`.
- **The JavaScript object model.** Upstream's registries also resolve `~`, their marker key, and names every
  object inherits such as `toString`. A Python registry has neither.
- **The runtime.** The oracle runs on Node 24, and JSON parse messages are V8's. Bun or Deno word them
  differently.
- **The driver.** Upstream hands some values to its JavaScript driver as they are, so what gets bound
  depends on the adapter. The oracle runs the postgres.js adapter, and Python binds as postgres.js does.
- **Nesting depth.** Upstream checks that a mutation's arguments are JSON by recursing through them, and
  on Node 24's default stack it runs out at 4,296 levels of nested arrays. Past that it refuses the push
  with `RangeError: Maximum call stack size exceeded`. Python's `json` goes to about 10,000 levels, so
  between the two Python accepts what upstream refuses. Past 10,000 Python refuses with the same
  `RangeError`. The exact upstream limit depends on the stack size Node runs with, so it can't be
  matched; the tests pin 300, 1,000 and 4,000 levels, which both accept, and 12,000, which both refuse.
  Error details nested past about 900 levels exceed Python's recursion limit when they are written out.

## Adding to the port

Port a module, cite it in the docstring as `packages/<package>/src/<path>.ts`, and the proof starts holding
it to full coverage. Siblings in the same directory can be cited by file name alone.
