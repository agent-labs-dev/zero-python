"""psycopg 3, async. Needs the `zero-server[psycopg]` extra.

Statements run through a raw cursor, which takes Postgres's own `$1` placeholders, so the SQL is the
same text the other adapters send.
"""

from collections.abc import Awaitable, Callable, Sequence
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
from zero_server.server.transaction import DBTransaction, Row
from zero_server.server.zql_database import PostgresError, ZQLDatabase


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
        bound = [_NulText(p) if isinstance(p, str) and "\x00" in p else p for p in params]
        try:
            await cursor.execute(sql, bound)  # ty: ignore[invalid-argument-type]
            return await cursor.fetchall() if cursor.description is not None else []
        except psycopg.errors.Error as error:
            raise PostgresError(error.diag.message_primary or str(error)) from error
        finally:
            await cursor.close()


class PsycopgConnection:
    def __init__(self, pool: "AsyncConnectionPool") -> None:
        self.pool = pool

    async def transaction[R](self, fn: Callable[[DBTransaction[psycopg.AsyncConnection[Any]]], Awaitable[R]]) -> R:
        async with self.pool.connection() as connection, connection.transaction():
            return await fn(PsycopgTransaction(connection))


class PsycopgDatabase(ZQLDatabase[psycopg.AsyncConnection[Any]]):
    """A `ZQLDatabase` on a psycopg pool (`AsyncConnectionPool(conninfo, open=False)`, then `await pool.open()`)."""

    def __init__(self, pool: "AsyncConnectionPool", schema: Schema | None = None) -> None:
        super().__init__(PsycopgConnection(pool), schema)
