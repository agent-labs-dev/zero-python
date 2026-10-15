"""asyncpg, directly. Needs the `zero-server[asyncpg]` extra."""

from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

import asyncpg

from zero_server.schema import Schema
from zero_server.server.adapters._session import SessionConnection, session
from zero_server.server.transaction import DBTransaction, Row
from zero_server.server.zql_database import PostgresError, ZQLDatabase


class AsyncpgTransaction:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    @property
    def wrapped_transaction(self) -> asyncpg.Connection:
        return self._connection

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        try:
            records = await self._connection.fetch(sql, *params)
        except asyncpg.PostgresError as error:
            # asyncpg sets `message` to the primary message alone, or to None for an argument it refused to send.
            message = getattr(error, "message", None)
            raise PostgresError(message if isinstance(message, str) else str(error)) from error
        return [dict(record) for record in records]


class AsyncpgConnection:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def transaction[R](self, fn: Callable[[DBTransaction[asyncpg.Connection]], Awaitable[R]]) -> R:
        async with self.pool.acquire() as connection:
            return await _transaction(connection, fn)

    def session(self) -> AbstractAsyncContextManager[SessionConnection[asyncpg.Connection, asyncpg.Connection]]:
        """One connection for a push's transactions, released (and so reset by the pool) once, at the end."""
        return session(self.pool.acquire, self.pool.release, _transaction)


async def _transaction[R](
    connection: asyncpg.Connection, fn: Callable[[DBTransaction[asyncpg.Connection]], Awaitable[R]]
) -> R:
    async with connection.transaction():
        return await fn(AsyncpgTransaction(connection))


class AsyncpgDatabase(ZQLDatabase[asyncpg.Connection]):
    """A `ZQLDatabase` on an asyncpg pool (`await asyncpg.create_pool(dsn)`)."""

    def __init__(self, pool: asyncpg.Pool, schema: Schema | None = None) -> None:
        super().__init__(AsyncpgConnection(pool), schema)
