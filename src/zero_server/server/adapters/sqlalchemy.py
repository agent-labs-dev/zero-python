"""SQLAlchemy's asyncio engine, with the asyncpg or the psycopg driver.

Needs the `zero-server[sqlalchemy]` extra, and the driver the engine's URL names.
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from zero_server.schema import Schema
from zero_server.server.transaction import DBTransaction, Row
from zero_server.server.zql_database import PostgresError, ZQLDatabase


class SQLAlchemyTransaction:
    """One transaction of an `AsyncConnection`.

    On asyncpg a statement goes through `exec_driver_sql`, which hands the `$1` placeholders to asyncpg
    as they are. psycopg's paramstyle is `%s`, so on psycopg it goes through psycopg's raw cursor on the
    same connection, inside the transaction SQLAlchemy opened.
    """

    def __init__(self, connection: AsyncConnection, driver_transaction: DBTransaction[Any] | None = None) -> None:
        self._connection = connection
        self._driver_transaction = driver_transaction

    @property
    def wrapped_transaction(self) -> AsyncConnection:
        return self._connection

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        if self._driver_transaction is not None:
            return await self._driver_transaction.query(sql, params)
        try:
            result = await self._connection.exec_driver_sql(sql, tuple(params))
        except DBAPIError as error:
            cause = error.orig.__cause__ if error.orig is not None and error.orig.__cause__ is not None else error.orig
            # asyncpg's `message` is the primary message alone, which is all postgres.js reports.
            message = getattr(cause, "message", None)
            raise PostgresError(message if isinstance(message, str) else str(cause)) from error
        return [dict(row) for row in result.mappings()] if result.returns_rows else []


async def sqlalchemy_transaction(connection: AsyncConnection) -> SQLAlchemyTransaction:
    """Wrap a connection that is already in a transaction, such as one an `AsyncSession` holds."""
    driver = connection.dialect.driver
    if driver == "asyncpg":
        return SQLAlchemyTransaction(connection)
    if driver == "psycopg":
        from zero_server.server.adapters.psycopg import PsycopgTransaction

        driver_connection = (await connection.get_raw_connection()).driver_connection
        # `None` only once SQLAlchemy has invalidated the connection, which it never does mid-transaction.
        assert driver_connection is not None
        return SQLAlchemyTransaction(connection, PsycopgTransaction(driver_connection))
    raise ValueError(f"SQLAlchemy driver {driver!r} is not supported; use asyncpg or psycopg")


class SQLAlchemyConnection:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def transaction[R](self, fn: Callable[[DBTransaction[AsyncConnection]], Awaitable[R]]) -> R:
        async with self.engine.begin() as connection:
            return await fn(await sqlalchemy_transaction(connection))


class SQLAlchemyDatabase(ZQLDatabase[AsyncConnection]):
    """A `ZQLDatabase` on `engine`, whose URL names the asyncpg or the psycopg driver
    (`postgresql+asyncpg://` or `postgresql+psycopg://`)."""

    def __init__(self, engine: AsyncEngine, schema: Schema | None = None) -> None:
        super().__init__(SQLAlchemyConnection(engine), schema)
