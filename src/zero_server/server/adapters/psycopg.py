"""psycopg 3, async. Needs the `zero-server[psycopg]` extra.

Statements run through a raw cursor, which takes Postgres's own `$1` placeholders, so the SQL is the
same text the other adapters send.
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from zero_server.schema import Schema
from zero_server.server.transaction import DBTransaction, Row
from zero_server.server.zql_database import PostgresError, ZQLDatabase


class PsycopgTransaction:
    def __init__(self, connection: psycopg.AsyncConnection[Any]) -> None:
        self._connection = connection

    @property
    def wrapped_transaction(self) -> psycopg.AsyncConnection[Any]:
        return self._connection

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        cursor = psycopg.AsyncRawCursor(self._connection, row_factory=dict_row)
        try:
            await cursor.execute(sql, list(params))  # ty: ignore[invalid-argument-type]
            return await cursor.fetchall() if cursor.description is not None else []
        except psycopg.errors.Error as error:
            if error.diag.sqlstate is None and "NUL (0x00)" in str(error):
                # psycopg refuses a NUL in a text value before sending it. Report what Postgres reports
                # for the same value through the other drivers.
                raise PostgresError('invalid byte sequence for encoding "UTF8": 0x00') from error
            raise PostgresError(error.diag.message_primary or str(error)) from error
        finally:
            await cursor.close()


class PsycopgConnection:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def transaction[R](self, fn: Callable[[DBTransaction[psycopg.AsyncConnection[Any]]], Awaitable[R]]) -> R:
        async with self.pool.connection() as connection, connection.transaction():
            return await fn(PsycopgTransaction(connection))


class PsycopgDatabase(ZQLDatabase[psycopg.AsyncConnection[Any]]):
    """A `ZQLDatabase` on a psycopg pool (`AsyncConnectionPool(conninfo, open=False)`, then `await pool.open()`)."""

    def __init__(self, pool: AsyncConnectionPool, schema: Schema | None = None) -> None:
        super().__init__(PsycopgConnection(pool), schema)
