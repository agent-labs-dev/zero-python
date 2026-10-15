# zero-python

A community Python server SDK for [Zero](https://zero.rocicorp.dev/). It is not affiliated with or endorsed by Rocicorp.

It ports what `@rocicorp/zero/server` gives a TypeScript backend: the query endpoint (`ZERO_QUERY_URL`), the mutate endpoint (`ZERO_MUTATE_URL`), the ZQL query builder, `defineQueries` and `defineMutators`, and `tx.mutate` and `tx.run` on Postgres. The client and `zero-cache` stay in TypeScript, and a Python backend serves the same TypeScript client a TypeScript backend would.

Compatibility is pinned to one Zero release at a time. This version is tested against `@rocicorp/zero` **1.10.0** (see `ZERO_VERSION`).

## Usage

```sh
pip install "zero-python[sqlalchemy,starlette]"   # or [asyncpg], or [psycopg]
```

The schema stays in your TypeScript `schema.ts`, because the client needs it. Emit it as JSON, and generate Python types from it:

```ts
// emit-schema.ts
import {schema} from './schema.ts';
process.stdout.write(JSON.stringify(schema));
```

```sh
node emit-schema.ts > schema.json
zero-python codegen schema.json -o zero_schema.py
```

`zero_schema.py` has a `TypedDict` per table for a row, an insert, an update and a key, and a builder and `tx.mutate` typed by them. A server then looks like this:

```python
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.applications import Starlette
from zero_schema import Project, create_builder, mutate

from zero_python import ApplicationError, define_mutator, define_mutators, define_queries, define_query, load_schema
from zero_python.integrations.starlette import zero_routes
from zero_python.server.adapters.sqlalchemy import SQLAlchemyDatabase

schema = load_schema(Path("schema.json"))
zql = create_builder(schema)
db = SQLAlchemyDatabase(create_async_engine("postgresql+asyncpg://..."), schema)


@define_query
def my_projects(*, args, ctx):
    return zql.project.where("ownerID", ctx["userID"]).related("tasks").order_by("name", "asc")


class NewTask(BaseModel):
    id: str
    projectID: str
    title: str


@define_mutator(NewTask)
async def create_task(*, tx, ctx, args: NewTask):
    project: Project | None = await tx.run(zql.project.where("id", args.projectID).one())
    if project is None or project["ownerID"] != ctx["userID"]:
        raise ApplicationError("Not your project")
    await mutate(tx).task.insert({"id": args.id, "projectID": args.projectID, "title": args.title})


app = Starlette(
    routes=zero_routes(
        schema=schema,
        database=db,
        queries=define_queries({"projects": {"mine": my_projects}}),
        mutators=define_mutators({"task": {"create": create_task}}),
        authenticate=verify_token,  # your function: request -> user ID, or None when logged out
    )
)
```

Point `ZERO_QUERY_URL` and `ZERO_MUTATE_URL` at `/query` and `/mutate`. With FastAPI, `app.router.routes.extend(zero_routes(...))`. With another framework, call `handle_query_request` and `handle_mutate_request` yourself; `zero_python/integrations/starlette.py` is short enough to copy.

- **Databases.** `SQLAlchemyDatabase` (SQLAlchemy's asyncio engine on asyncpg), `AsyncpgDatabase` (an asyncpg pool) and `PsycopgDatabase` (a psycopg 3 async pool), under `zero_python.server.adapters`. Another driver plugs into `ZQLDatabase` as a `DBConnection` whose `query` takes `$1` placeholders.
- **Validation.** `@define_query(Model)` and `@define_mutator(Model)` parse args with a Pydantic model or `TypeAdapter` first; a failure reaches the client as upstream's validation error.
- **Strict building.** `create_builder(schema, strict=True)` refuses an unknown column when the query is built. Upstream only refuses it when the endpoint maps the query, failing the whole batch, so this is for your tests.

## How compatibility is checked

The published npm package is the definition of correct. Every test diffs Python against it:

- **Wire.** `harness/` runs the real `handleMutateRequest`, `handleQueryRequest`, `PushProcessor` and the query builder under Node. Hypothesis generates pushes (with duplicates, gaps, refusals and cleanup mutations) and builder programs, sends each through both sides against identical Postgres databases, and diffs the responses and every row written.
- **SQL.** The same generated queries run through upstream's `ZQLDatabase.run` and through `tx.run` against one seeded database, and the result rows are compared, never the SQL text.

The wire and SQL tiers run against every adapter.
- **End to end.** `e2e/run.py` starts Postgres, two `rocicorp/zero` containers at the pinned version, the TypeScript reference app server and the Python one. An unmodified Zero client runs the same script through each, and the run fails if the client ends up with different data, the databases differ, or the reference run strays from `e2e/expected.json`.

Run them with `uv run pytest` (needs Node 24 and Docker, or `ZERO_PYTHON_PG` pointing at a Postgres) and `uv run --group e2e python e2e/run.py`.

## Known differences

- **Parse errors.** When a request is malformed, the message after `Failed to parse push body:` describes the bad field in Pydantic's words, not valita's. The rest of the response matches, and zero-cache only logs it.
- **Non-string values against text columns.** Upstream passes such a value to the JavaScript driver as is, so the driver decides how it binds. zero-python binds `String(value)`, which is what postgres.js does except for an array whose first element is a boolean, which it binds as `false`.

## License

Apache-2.0. Zero itself is Apache-2.0, and code ported from [rocicorp/mono](https://github.com/rocicorp/mono) keeps its notices.
