import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.postgres import postgres_server

DDL = [
    "CREATE SCHEMA zero_0",
    """CREATE TABLE zero_0."clients" (
        "clientGroupID"  TEXT NOT NULL,
        "clientID"       TEXT NOT NULL,
        "lastMutationID" BIGINT NOT NULL,
        "userID"         TEXT,
        PRIMARY KEY("clientGroupID", "clientID"))""",
    """CREATE TABLE zero_0."mutations" (
        "clientGroupID"  TEXT NOT NULL,
        "clientID"       TEXT NOT NULL,
        "mutationID"     BIGINT NOT NULL,
        "result"         JSON NOT NULL,
        PRIMARY KEY("clientGroupID", "clientID", "mutationID"))""",
    "CREATE TABLE item (id TEXT PRIMARY KEY, value TEXT NOT NULL)",
]

RESET = 'TRUNCATE item, zero_0."clients", zero_0."mutations"'


@dataclass
class WireDB:
    """One database per side, built identically, so each side's writes can be diffed."""

    runner: asyncio.Runner
    ts_url: str
    py_engine: AsyncEngine
    ts_engine: AsyncEngine

    def run(self, coro: Any) -> Any:
        return self.runner.run(coro)

    def reset(self) -> None:
        async def reset() -> None:
            for engine in (self.py_engine, self.ts_engine):
                async with engine.begin() as conn:
                    await conn.execute(text(RESET))

        self.run(reset())

    def dump(self, engine: AsyncEngine) -> dict[str, list[Any]]:
        async def dump() -> dict[str, list[Any]]:
            async with engine.connect() as conn:
                queries = {
                    "clients": 'SELECT * FROM zero_0."clients" ORDER BY 1, 2',
                    "mutations": 'SELECT "clientGroupID", "clientID", "mutationID", "result"::jsonb AS result '
                    'FROM zero_0."mutations" ORDER BY 1, 2, 3',
                    "item": "SELECT * FROM item ORDER BY id",
                }
                return {
                    name: [dict(row) for row in (await conn.execute(text(sql))).mappings()]
                    for name, sql in queries.items()
                }

        return self.run(dump())


@pytest.fixture(scope="session")
def wire_db() -> Iterator[WireDB]:
    with postgres_server() as base, asyncio.Runner() as runner:

        async def setup() -> tuple[AsyncEngine, AsyncEngine]:
            admin = create_async_engine(
                f"postgresql+asyncpg://{base.split('://', 1)[1]}/postgres", isolation_level="AUTOCOMMIT"
            )
            async with admin.connect() as conn:
                for name in ("zp_wire_ts", "zp_wire_py"):
                    await conn.execute(text(f"DROP DATABASE IF EXISTS {name}"))
                    await conn.execute(text(f"CREATE DATABASE {name}"))
            await admin.dispose()
            engines = []
            for name in ("zp_wire_ts", "zp_wire_py"):
                engine = create_async_engine(f"postgresql+asyncpg://{base.split('://', 1)[1]}/{name}")
                async with engine.begin() as conn:
                    for statement in DDL:
                        await conn.execute(text(statement))
                engines.append(engine)
            return engines[0], engines[1]

        ts_engine, py_engine = runner.run(setup())
        yield WireDB(runner=runner, ts_url=f"{base}/zp_wire_ts", py_engine=py_engine, ts_engine=ts_engine)
        runner.run(ts_engine.dispose())
        runner.run(py_engine.dispose())
