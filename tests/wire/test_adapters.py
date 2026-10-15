"""Sessions: a push's transactions on one connection from the adapter's pool."""

import logging
from types import SimpleNamespace
from typing import Any

import asyncpg
import pytest

from tests.wire.conftest import ADAPTERS, WireDB
from zero_server.server.adapters.asyncpg import AsyncpgDatabase
from zero_server.server.adapters.sqlalchemy import _send_begin
from zero_server.server.zql_database import ZQLDatabase


async def _connection(tx: Any, _hooks: Any) -> Any:
    return tx.db_transaction.wrapped_transaction


async def _fail(_tx: Any, _hooks: Any) -> Any:
    raise RuntimeError("no")


def _asyncpg_url(wire_db: WireDB) -> str:
    return wire_db.py_engine.url.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_a_session_runs_its_transactions_on_one_connection(wire_db: WireDB, adapter: str) -> None:
    async def check() -> None:
        database = wire_db.py[adapter](None)
        async with database.session() as db:
            first = await db.transaction(_connection)
            assert await db.transaction(_connection) is first

    wire_db.run(check())


def test_a_session_takes_and_returns_its_connection_once(wire_db: WireDB) -> None:
    async def check() -> None:
        pool = await asyncpg.create_pool(_asyncpg_url(wire_db), min_size=2, max_size=2)
        async with AsyncpgDatabase(pool).session() as db:
            assert pool.get_idle_size() == 2  # nothing taken before the first transaction
            first = await db.transaction(_connection)
            assert pool.get_idle_size() == 1
            with pytest.raises(RuntimeError):
                await db.transaction(_fail)
            # A failed transaction hands its connection back; the next one takes a connection afresh.
            assert pool.get_idle_size() == 2
            assert await db.transaction(_connection) is not first
        assert pool.get_idle_size() == 2
        await pool.close()

    wire_db.run(check())


def test_a_session_logs_a_connection_the_pool_could_not_take_back(
    wire_db: WireDB, caplog: pytest.LogCaptureFixture
) -> None:
    async def refuse(_connection: Any) -> None:
        raise RuntimeError("cannot reset")

    async def check() -> None:
        pool = await asyncpg.create_pool(_asyncpg_url(wire_db), min_size=1, max_size=1, reset=refuse)
        async with AsyncpgDatabase(pool).session() as db:
            await db.transaction(_connection)
        await pool.close()

    with caplog.at_level(logging.WARNING, logger="zero_server.adapters"):
        wire_db.run(check())
    assert "Failed to return a connection to the pool" in caplog.text


async def test_a_connection_without_sessions_runs_each_transaction_on_its_own() -> None:
    class Connection:
        async def transaction(self, fn: Any) -> Any:
            return await fn(None)

    database = ZQLDatabase(Connection())
    async with database.session() as db:
        assert db is database


async def test_sqlalchemy_begins_on_its_first_statement_without_start_transaction() -> None:
    # A SQLAlchemy without the dialect's private `_start_transaction` sends nothing early.
    await _send_begin(SimpleNamespace(dbapi_connection=object()))  # ty: ignore[invalid-argument-type]
