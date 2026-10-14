"""SQLAlchemy's asyncio engine, with the asyncpg driver.

Needs the `zero-python[sqlalchemy]` extra.
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from zero_python.schema import Schema
from zero_python.server.transaction import DBTransaction, Row
from zero_python.server.zql_database import PostgresError, ZQLDatabase


class SQLAlchemyTransaction:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    @property
    def wrapped_transaction(self) -> AsyncConnection:
        return self._connection

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        try:
            result = await self._connection.exec_driver_sql(sql, tuple(params))
        except DBAPIError as error:
            cause = error.orig.__cause__ if error.orig is not None and error.orig.__cause__ is not None else error.orig
            # asyncpg's `message` is the primary message alone, which is all postgres.js reports.
            message = getattr(cause, "message", None)
            raise PostgresError(message if isinstance(message, str) else str(cause)) from error
        return [dict(row) for row in result.mappings()] if result.returns_rows else []


class SQLAlchemyConnection:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def transaction[R](self, fn: Callable[[DBTransaction[AsyncConnection]], Awaitable[R]]) -> R:
        async with self.engine.begin() as connection:
            return await fn(SQLAlchemyTransaction(connection))


class SQLAlchemyDatabase(ZQLDatabase[AsyncConnection]):
    """A `ZQLDatabase` on `engine`, which must use the asyncpg driver (`postgresql+asyncpg://`)."""

    def __init__(self, engine: AsyncEngine, schema: Schema | None = None) -> None:
        super().__init__(SQLAlchemyConnection(engine), schema)
