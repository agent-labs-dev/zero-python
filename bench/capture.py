"""Regenerate `bench/fixtures/`: what the CPU benchmarks would otherwise need Node and Postgres for.

    uv run python -m bench.capture

`schema.json` and `wire_schema.json` are the harness's schemas as `harness/schema.ts` and
`harness/mutate.ts` emit them. `*_server_schema_rows.json` are the rows Postgres returns for the
server-schema query over each schema's tables, which the CPU benchmarks replay from a fake database.
`tests/wire/test_bench_fixtures.py` fails when any of them goes stale.
"""

import asyncio
import json
from typing import Any

import asyncpg

from bench.workloads import FIXTURES
from tests.harness import Harness
from tests.postgres import postgres_server
from tests.wire import seed
from tests.wire.conftest import DDL as WIRE_DDL
from zero_server.schema import Schema, load_schema
from zero_server.server.server_schema import get_server_schema
from zero_server.server.transaction import Row


async def server_schema_rows(url: str, schema: Schema) -> list[Row]:
    """The rows the server-schema query returns for `schema` on the database at `url`."""
    conn = await asyncpg.connect(url)
    captured: list[Row] = []

    class Recording:
        wrapped_transaction = None

        async def query(self, sql: str, params: Any = ()) -> list[Row]:
            captured[:] = [dict(r) for r in await conn.fetch(sql, *params)]
            return captured

    try:
        await get_server_schema(Recording(), schema)
    finally:
        await conn.close()
    return captured


async def capture(base: str, schemas: dict[str, Any]) -> None:
    admin = await asyncpg.connect(f"{base}/postgres")
    for name, ddl, schema in (
        ("query", seed.DDL, schemas["schema"]),
        ("mutate", WIRE_DDL, schemas["wire_schema"]),
    ):
        await admin.execute(f"DROP DATABASE IF EXISTS bench_capture_{name}")
        await admin.execute(f"CREATE DATABASE bench_capture_{name}")
        conn = await asyncpg.connect(f"{base}/bench_capture_{name}")
        for statement in ddl:
            await conn.execute(statement)
        await conn.close()
        rows = await server_schema_rows(f"{base}/bench_capture_{name}", load_schema(json.dumps(schema)))
        (FIXTURES / f"{name}_server_schema_rows.json").write_text(json.dumps(rows, indent=1) + "\n")
    await admin.close()


def main() -> None:
    harness = Harness()
    schemas = {"schema": harness.call("schema"), "wire_schema": harness.call("wireSchema")}
    harness.close()
    FIXTURES.mkdir(exist_ok=True)
    for name, value in schemas.items():
        (FIXTURES / f"{name}.json").write_text(json.dumps(value, indent=1) + "\n")
    with postgres_server() as base:
        asyncio.run(capture(base, schemas))
    print(f"wrote {FIXTURES}")


if __name__ == "__main__":
    main()
