# zero-python

A community Python server SDK for [Zero](https://zero.rocicorp.dev/). It is not affiliated with or endorsed by Rocicorp.

It ports what `@rocicorp/zero/server` gives a TypeScript backend: the query endpoint (`ZERO_QUERY_URL`), the mutate endpoint (`ZERO_MUTATE_URL`), the ZQL query builder, `defineQueries` and `defineMutators`, and `tx.mutate` and `tx.run` on Postgres. The client and `zero-cache` stay in TypeScript, and a Python backend serves the same TypeScript client a TypeScript backend would.

Compatibility is pinned to one Zero release at a time. This version is tested against `@rocicorp/zero` **1.10.0** (see `ZERO_VERSION`).

## Usage

The schema stays in your TypeScript `schema.ts`, because the client needs it. Emit it as JSON and load that:

```ts
// emit-schema.ts
import {schema} from './schema.ts';
process.stdout.write(JSON.stringify(schema));
```

```python
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

from zero_python import (
    ApplicationError,
    create_builder,
    define_mutator,
    define_mutators,
    define_queries,
    define_query,
    handle_mutate_request,
    handle_query_request,
    load_schema,
    must_get_mutator,
    must_get_query,
)
from zero_python.server.sqlalchemy import SQLAlchemyDatabase

schema = load_schema(Path("schema.json"))
zql = create_builder(schema)
db = SQLAlchemyDatabase(create_async_engine("postgresql+asyncpg://..."), schema)

queries = define_queries(
    {
        "projects": {"mine": define_query(lambda *, args, ctx: zql.project.where("ownerID", ctx["userID"]))},
    }
)


async def create_project(*, tx, ctx, args):
    if not args["name"].strip():
        raise ApplicationError("A project needs a name")
    await tx.mutate.project.insert({"id": args["id"], "name": args["name"], "ownerID": ctx["userID"]})


mutators = define_mutators({"project": {"create": define_mutator(create_project)}})


async def query_endpoint(body: bytes, user_id: str | None) -> dict:
    ctx = {"userID": user_id}
    response = await handle_query_request(
        handler=lambda name, args: must_get_query(queries, name).fn(args=args, ctx=ctx),
        schema=schema,
        body=body,
        user_id=user_id,
    )
    return response.to_json()


async def mutate_endpoint(query_string: dict[str, str], body: bytes, user_id: str | None) -> dict:
    ctx = {"userID": user_id}

    async def handler(transact, mutation):
        return await transact(lambda tx, name, args: must_get_mutator(mutators, name).fn(tx=tx, ctx=ctx, args=args))

    response = await handle_mutate_request(
        db_provider=db,
        handler=handler,
        query=query_string,
        body=body,
        user_id=user_id,
    )
    return response.to_json()
```

Wire those two functions to whatever web framework you use. `e2e/py_server.py` is a complete Starlette app.

Install with the SQLAlchemy provider: `pip install "zero-python[sqlalchemy]"`.

## How compatibility is checked

The published npm package is the definition of correct. Every test diffs Python against it:

- **Wire.** `harness/` runs the real `handleMutateRequest`, `handleQueryRequest`, `PushProcessor` and the query builder under Node. Hypothesis generates pushes (with duplicates, gaps, refusals and cleanup mutations) and builder programs, sends each through both sides against identical Postgres databases, and diffs the responses and every row written.
- **SQL.** The same generated queries run through upstream's `ZQLDatabase.run` and through `tx.run` against one seeded database, and the result rows are compared, never the SQL text.
- **End to end.** `e2e/run.py` starts Postgres, two `rocicorp/zero` containers at the pinned version, the TypeScript reference app server and the Python one. An unmodified Zero client runs the same script through each, and the run fails if the client ends up with different data or the databases differ.

Run them with `uv run pytest` (needs Node 24 and Docker, or `ZERO_PYTHON_PG` pointing at a Postgres) and `uv run --group e2e python e2e/run.py`.

## License

Apache-2.0. Zero itself is Apache-2.0, and code ported from [rocicorp/mono](https://github.com/rocicorp/mono) keeps its notices.
