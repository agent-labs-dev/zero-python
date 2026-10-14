# zero-python

A community Python server SDK for [Zero](https://zero.rocicorp.dev/). It is not affiliated with or endorsed by Rocicorp.

Nothing here works yet. This release only reserves the name on PyPI.

The plan is a Python port of what `@rocicorp/zero/server` gives a TypeScript backend: the query endpoint (`ZERO_QUERY_URL`), the mutate endpoint (`ZERO_MUTATE_URL`), the ZQL query builder, and server-side ZQL against Postgres. Each release will name the exact `@rocicorp/zero` version its tests ran against.

The client and `zero-cache` stay in TypeScript. A Python backend serves the same TypeScript client a TypeScript backend would.

## License

Apache-2.0. Zero itself is Apache-2.0, and code ported from [rocicorp/mono](https://github.com/rocicorp/mono) keeps its notices.
