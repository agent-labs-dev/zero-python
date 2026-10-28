import asyncio
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.postgres import postgres_server
from tests.wire import seed
from zero_server.schema import Schema
from zero_server.server.adapters.asyncpg import AsyncpgDatabase
from zero_server.server.adapters.psycopg import PsycopgDatabase
from zero_server.server.adapters.sqlalchemy import SQLAlchemyDatabase
from zero_server.server.zql_database import ZQLDatabase

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
    "CREATE TYPE mood AS ENUM ('happy', 'sad')",
    """CREATE TABLE things (
        id TEXT PRIMARY KEY,
        n INTEGER,
        f DOUBLE PRECISION,
        b BOOLEAN,
        j JSONB,
        happened_at TIMESTAMPTZ,
        d DATE,
        s VARCHAR(8),
        tags TEXT[],
        u UUID,
        mood mood,
        tm TIME,
        ttz TIMETZ,
        amount NUMERIC(10, 2),
        ip INET,
        stamps TIMESTAMPTZ[],
        clocks TIMETZ[])""",
    "CREATE TABLE pair (a TEXT, b INTEGER, note TEXT, PRIMARY KEY (a, b))",
    # Extension types: `isbn` is user-defined and Zero takes it as a string; `citext` and `point` it does not.
    "CREATE EXTENSION isn",
    "CREATE EXTENSION citext",
    "CREATE TABLE oddity (id TEXT PRIMARY KEY, code isbn, name citext, at point)",
]

ADAPTERS = ["sqlalchemy", "sqlalchemy-psycopg", "asyncpg", "psycopg"]
"""Every Python adapter runs the same wire and SQL tiers."""

type Databases = dict[str, Callable[..., ZQLDatabase[Any]]]
"""Per adapter, `(schema, isolation_level=None) -> ZQLDatabase`."""


async def open_adapters(url: str, engine: AsyncEngine) -> tuple[Databases, Callable[[], Any]]:
    """A `ZQLDatabase` factory per adapter on the database at `url`, and a coroutine that closes them."""
    import asyncpg
    from psycopg_pool import AsyncConnectionPool

    pool = await asyncpg.create_pool(url, min_size=1, max_size=4)
    psycopg_pool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
    await psycopg_pool.open()
    psycopg_engine = create_async_engine(f"postgresql+psycopg://{url.split('://', 1)[1]}")

    async def close() -> None:
        await pool.close()
        await psycopg_pool.close()
        await psycopg_engine.dispose()

    return {
        "sqlalchemy": lambda schema, isolation_level=None: SQLAlchemyDatabase(
            engine, schema, isolation_level=isolation_level
        ),
        "sqlalchemy-psycopg": lambda schema, isolation_level=None: SQLAlchemyDatabase(
            psycopg_engine, schema, isolation_level=isolation_level
        ),
        "asyncpg": lambda schema, isolation_level=None: AsyncpgDatabase(pool, schema, isolation_level=isolation_level),
        "psycopg": lambda schema, isolation_level=None: PsycopgDatabase(
            psycopg_pool, schema, isolation_level=isolation_level
        ),
    }, close


# Both drivers prepare statements. Once Postgres switches one to a generic plan, a parameter cast that
# cannot succeed is evaluated per row instead of at planning, so it stops failing against an empty table,
# and when that happens depends on each pooled connection's history. Custom plans keep it deterministic.
DETERMINISTIC_PLANS = "ALTER DATABASE {name} SET plan_cache_mode = force_custom_plan"

RESET = 'TRUNCATE item, things, pair, zero_0."clients", zero_0."mutations"'


@dataclass
class WireDB:
    """One database per side, built identically, so each side's writes can be diffed."""

    runner: asyncio.Runner
    ts_url: str
    py_engine: AsyncEngine
    ts_engine: AsyncEngine
    py: Databases

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
                    "mutations": 'SELECT "clientGroupID", "clientID", "mutationID", "result"::text AS result '
                    'FROM zero_0."mutations" ORDER BY 1, 2, 3',
                    "item": "SELECT * FROM item ORDER BY id",
                    "things": "SELECT id, n, f, b, j::text AS j, happened_at::text AS t, d::text AS d, s, "
                    "tags::text AS tags, u::text AS u, mood::text AS mood, tm::text AS tm, ttz::text AS ttz, "
                    "amount::text AS amount, ip::text AS ip, stamps::text AS stamps, clocks::text AS clocks "
                    "FROM things ORDER BY id",
                    "pair": "SELECT * FROM pair ORDER BY a, b",
                }
                dumped = {
                    name: [dict(row) for row in (await conn.execute(text(sql))).mappings()]
                    for name, sql in queries.items()
                }
                # Compared as the exact text each side stored: `json` keeps it verbatim.
                return dumped

        return self.run(dump())


@dataclass
class SqlDB:
    """One seeded database both sides read: the SQL tier only reads."""

    runner: asyncio.Runner
    url: str
    engine: AsyncEngine
    py: Databases

    def run(self, coro: Any) -> Any:
        return self.runner.run(coro)


@pytest.fixture(scope="session")
def sql_db(wire_db: WireDB) -> Iterator[SqlDB]:
    base = wire_db.ts_url.rsplit("/", 1)[0]

    async def setup() -> AsyncEngine:
        admin = create_async_engine(
            f"postgresql+asyncpg://{base.split('://', 1)[1]}/postgres", isolation_level="AUTOCOMMIT"
        )
        async with admin.connect() as conn:
            await conn.execute(text("DROP DATABASE IF EXISTS zp_sql"))
            await conn.execute(text("CREATE DATABASE zp_sql"))
            await conn.execute(text(DETERMINISTIC_PLANS.format(name="zp_sql")))
        await admin.dispose()
        engine = create_async_engine(f"postgresql+asyncpg://{base.split('://', 1)[1]}/zp_sql")
        async with engine.begin() as conn:
            for statement in seed.DDL:
                await conn.execute(text(statement))
            for statement, params in seed.inserts():
                await conn.execute(text(statement), params)
        return engine

    engine = wire_db.run(setup())
    py, close = wire_db.run(open_adapters(f"{base}/zp_sql", engine))
    yield SqlDB(runner=wire_db.runner, url=f"{base}/zp_sql", engine=engine, py=py)
    wire_db.run(close())
    wire_db.run(engine.dispose())


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
                    await conn.execute(text(DETERMINISTIC_PLANS.format(name=name)))
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
        py, close = runner.run(open_adapters(f"{base}/zp_wire_py", py_engine))
        yield WireDB(runner=runner, ts_url=f"{base}/zp_wire_ts", py_engine=py_engine, ts_engine=ts_engine, py=py)
        runner.run(close())
        runner.run(ts_engine.dispose())
        runner.run(py_engine.dispose())


def with_isolation(wire_db: WireDB, adapter: str, level: Any) -> ZQLDatabase[Any]:
    return wire_db.py[adapter](None, isolation_level=level)


class _Recorded:
    """The twin of the connection `recordedRun` in `harness/mutate.ts` builds: logs what it is asked to do,
    and fails the first statement when told to."""

    def __init__(self, inner: Any, fail_first: bool) -> None:
        self.inner = inner
        self.log: list[str] = []
        self._failing = fail_first

    async def record(self, event: str, run: Callable[[], Any]) -> Any:
        self.log.append(event)
        if self._failing:
            self._failing = False
            raise Exception("statement failed")
        return await run()

    async def transaction(self, fn: Callable[[Any], Any]) -> Any:
        self.log.append("transaction")

        async def wrapped(tx: Any) -> Any:
            return await fn(_RecordedTransaction(self, tx))

        return await self.inner.transaction(wrapped)


class _RecordedTransaction:
    def __init__(self, recorded: _Recorded, tx: Any) -> None:
        self._recorded = recorded
        self._tx = tx

    @property
    def wrapped_transaction(self) -> Any:
        return self._tx.wrapped_transaction

    async def query(self, sql: str, params: Any = ()) -> Any:
        return await self._recorded.record("query in transaction", lambda: self._tx.query(sql, params))


class _RecordedBare(_Recorded):
    async def query(self, sql: str, params: Any = ()) -> Any:
        return await self.record("query", lambda: self.inner.query(sql, params))


async def recorded_run(
    wire_db: WireDB, adapter: str, schema: Schema, *, bare: bool, failFirst: bool, concurrent: bool, runs: int
) -> dict[str, Any]:
    from zero_server.query.builder import create_builder

    inner = wire_db.py[adapter](schema).connection
    connection = _RecordedBare(inner, failFirst) if bare else _Recorded(inner, failFirst)
    db = ZQLDatabase(connection, schema)
    query = create_builder(schema).item.where("value", "!=", "x").order_by("id", "asc")

    async def one() -> dict[str, Any]:
        try:
            return {"rows": await db.run(query)}
        except Exception as error:
            return {"error": str(error)}

    if concurrent:
        results = list(await asyncio.gather(*(one() for _ in range(runs))))
    else:
        results = [await one() for _ in range(runs)]
    return {"results": results, "log": connection.log}
