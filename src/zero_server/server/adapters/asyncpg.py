"""asyncpg, directly. Needs the `zero-server[asyncpg]` extra."""

from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

import asyncpg

from zero_server.schema import Schema
from zero_server.server.adapters._session import SessionConnection, session
from zero_server.server.transaction import DBTransaction, Row
from zero_server.server.transaction_options import IsolationLevel
from zero_server.server.zql_database import PostgresError, ZQLDatabase, well_formed_params


class AsyncpgTransaction:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    @property
    def wrapped_transaction(self) -> asyncpg.Connection:
        return self._connection

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        try:
            records = await self._connection.fetch(sql, *well_formed_params(params))
        except asyncpg.PostgresError as error:
            # asyncpg sets `message` to the primary message alone, or to None for an argument it refused to send.
            message = getattr(error, "message", None)
            raise PostgresError(message if isinstance(message, str) else str(error)) from error
        return [dict(record) for record in records]


class AsyncpgConnection:
    def __init__(self, pool: asyncpg.Pool, isolation_level: IsolationLevel | None = None) -> None:
        self.pool = pool
        # asyncpg spells the level with an underscore; `None` sends a bare `BEGIN`.
        self._isolation = None if isolation_level is None else isolation_level.replace(" ", "_")

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        async with self.pool.acquire() as connection:
            return await AsyncpgTransaction(connection).query(sql, params)

    async def transaction[R](self, fn: Callable[[DBTransaction[asyncpg.Connection]], Awaitable[R]]) -> R:
        async with self.pool.acquire() as connection:
            return await self._transaction(connection, fn)

    async def _transaction[R](
        self, connection: asyncpg.Connection, fn: Callable[[DBTransaction[asyncpg.Connection]], Awaitable[R]]
    ) -> R:
        async with connection.transaction(isolation=self._isolation):
            return await fn(AsyncpgTransaction(connection))

    def session(self) -> AbstractAsyncContextManager[SessionConnection[asyncpg.Connection, asyncpg.Connection]]:
        """One connection for a push's transactions, released (and so reset by the pool) once, at the end."""
        return session(self.pool.acquire, self.pool.release, self._transaction)


class AsyncpgDatabase(ZQLDatabase[asyncpg.Connection]):
    """A `ZQLDatabase` on an asyncpg pool (`await asyncpg.create_pool(dsn)`)."""

    def __init__(
        self, pool: asyncpg.Pool, schema: Schema | None = None, *, isolation_level: IsolationLevel | None = None
    ) -> None:
        super().__init__(AsyncpgConnection(pool, isolation_level), schema)
