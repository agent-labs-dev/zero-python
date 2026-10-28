"""psycopg 3, async. Needs the `zero-server[psycopg]` extra.

Statements run through a raw cursor, which takes Postgres's own `$1` placeholders, so the SQL is the
same text the other adapters send.
"""

from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg import pq
from psycopg.adapt import Dumper
from psycopg.rows import dict_row

if TYPE_CHECKING:
    # Only `PsycopgDatabase` takes a pool. The SQLAlchemy adapter reuses `PsycopgTransaction` on
    # SQLAlchemy's own connection, and must not need psycopg-pool installed for it.
    from psycopg_pool import AsyncConnectionPool

from zero_server.schema import Schema
from zero_server.server.adapters._session import SessionConnection, session
from zero_server.server.transaction import DBTransaction, Row
from zero_server.server.transaction_options import IsolationLevel, set_isolation_statement
from zero_server.server.zql_database import PostgresError, ZQLDatabase, well_formed_params


class _NulText(str):
    """A string holding a NUL, which psycopg refuses to send as text."""


class _NulTextDumper(Dumper):
    # Binary goes out unchecked, so Postgres refuses the NUL when it binds the value, after planning the
    # statement, as it does for the drivers that send text.
    format = pq.Format.BINARY

    def dump(self, obj: str) -> bytes:
        return obj.encode()


class PsycopgTransaction:
    def __init__(self, connection: psycopg.AsyncConnection[Any]) -> None:
        self._connection = connection

    @property
    def wrapped_transaction(self) -> psycopg.AsyncConnection[Any]:
        return self._connection

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        cursor = psycopg.AsyncRawCursor(self._connection, row_factory=dict_row)
        cursor.adapters.register_dumper(_NulText, _NulTextDumper)
        bound = [_NulText(p) if isinstance(p, str) and "\x00" in p else p for p in well_formed_params(params)]
        try:
            await cursor.execute(sql, bound)  # ty: ignore[invalid-argument-type]
            return await cursor.fetchall() if cursor.description is not None else []
        except psycopg.errors.Error as error:
            raise PostgresError(error.diag.message_primary or str(error)) from error
        finally:
            await cursor.close()


class PsycopgConnection:
    def __init__(self, pool: "AsyncConnectionPool", isolation_level: IsolationLevel | None = None) -> None:
        self.pool = pool
        self._isolation_level = isolation_level

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        async with self.pool.connection() as connection:
            # In autocommit, so psycopg sends no `BEGIN` of its own.
            autocommit = connection.autocommit
            await connection.set_autocommit(True)
            try:
                return await PsycopgTransaction(connection).query(sql, params)
            finally:
                await connection.set_autocommit(autocommit)

    async def transaction[R](self, fn: Callable[[DBTransaction[psycopg.AsyncConnection[Any]]], Awaitable[R]]) -> R:
        async with self.pool.connection() as connection:
            return await self._transaction(connection, fn)

    async def _transaction[R](
        self,
        connection: psycopg.AsyncConnection[Any],
        fn: Callable[[DBTransaction[psycopg.AsyncConnection[Any]]], Awaitable[R]],
    ) -> R:
        return await _transaction(connection, fn, self._isolation_level)

    def session(
        self,
    ) -> AbstractAsyncContextManager[SessionConnection[psycopg.AsyncConnection[Any], psycopg.AsyncConnection[Any]]]:
        """One connection for a push's transactions, returned to the pool once, at the end."""
        return session(self.pool.getconn, self.pool.putconn, self._transaction)


async def _transaction[R](
    connection: psycopg.AsyncConnection[Any],
    fn: Callable[[DBTransaction[psycopg.AsyncConnection[Any]]], Awaitable[R]],
    isolation_level: IsolationLevel | None,
) -> R:
    async with connection.transaction():
        tx = PsycopgTransaction(connection)
        if isolation_level is not None:
            await tx.query(set_isolation_statement(isolation_level))
        return await fn(tx)


class PsycopgDatabase(ZQLDatabase[psycopg.AsyncConnection[Any]]):
    """A `ZQLDatabase` on a psycopg pool (`AsyncConnectionPool(conninfo, open=False)`, then `await pool.open()`)."""

    def __init__(
        self,
        pool: "AsyncConnectionPool",
        schema: Schema | None = None,
        *,
        isolation_level: IsolationLevel | None = None,
    ) -> None:
        super().__init__(PsycopgConnection(pool, isolation_level), schema)
