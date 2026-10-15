"""SQLAlchemy's asyncio engine, with the asyncpg or the psycopg driver.

Needs the `zero-server[sqlalchemy]` extra, and the driver the engine's URL names.
"""

from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from zero_server.schema import Schema
from zero_server.server.adapters._session import SessionConnection, session
from zero_server.server.transaction import DBTransaction, Row
from zero_server.server.zql_database import PostgresError, ZQLDatabase


class SQLAlchemyTransaction:
    """One transaction of an `AsyncConnection`.

    psycopg's paramstyle is `%s`, so on psycopg a statement goes through psycopg's raw cursor on the
    same connection, inside the transaction SQLAlchemy opened. On asyncpg, SQLAlchemy defers `BEGIN` to
    the first statement it runs itself, so until the transaction is open a statement goes through
    `exec_driver_sql`, which hands the `$1` placeholders to asyncpg as they are. After that, statements
    go straight to the asyncpg connection, as the asyncpg adapter sends them.
    """

    def __init__(
        self,
        connection: AsyncConnection,
        driver_transaction: DBTransaction[Any] | None = None,
        driver_in_transaction: Callable[[], bool] = lambda: True,
    ) -> None:
        self._connection = connection
        self._driver_transaction = driver_transaction
        self._driver_in_transaction = driver_in_transaction

    @property
    def wrapped_transaction(self) -> AsyncConnection:
        return self._connection

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        if self._driver_transaction is not None and self._driver_in_transaction():
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
    if driver not in ("asyncpg", "psycopg"):
        raise ValueError(f"SQLAlchemy driver {driver!r} is not supported; use asyncpg or psycopg")
    driver_connection = (await connection.get_raw_connection()).driver_connection
    # `None` only once SQLAlchemy has invalidated the connection, which it never does mid-transaction.
    assert driver_connection is not None
    if driver == "asyncpg":
        from zero_server.server.adapters.asyncpg import AsyncpgTransaction

        return SQLAlchemyTransaction(
            connection, AsyncpgTransaction(driver_connection), driver_connection.is_in_transaction
        )
    from zero_server.server.adapters.psycopg import PsycopgTransaction

    return SQLAlchemyTransaction(connection, PsycopgTransaction(driver_connection))


class SQLAlchemyConnection:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def transaction[R](self, fn: Callable[[DBTransaction[AsyncConnection]], Awaitable[R]]) -> R:
        async with self.engine.begin() as connection:
            return await fn(await sqlalchemy_transaction(connection))

    def session(self) -> AbstractAsyncContextManager[SessionConnection[AsyncConnection, AsyncConnection]]:
        """One connection for a push's transactions, checked back into the pool once, at the end."""
        return session(self.engine.connect, _close, _transaction)


async def _close(connection: AsyncConnection) -> None:
    await connection.close()


async def _transaction[R](
    connection: AsyncConnection, fn: Callable[[DBTransaction[AsyncConnection]], Awaitable[R]]
) -> R:
    async with connection.begin():
        return await fn(await sqlalchemy_transaction(connection))


class SQLAlchemyDatabase(ZQLDatabase[AsyncConnection]):
    """A `ZQLDatabase` on `engine`, whose URL names the asyncpg or the psycopg driver
    (`postgresql+asyncpg://` or `postgresql+psycopg://`)."""

    def __init__(self, engine: AsyncEngine, schema: Schema | None = None) -> None:
        super().__init__(SQLAlchemyConnection(engine), schema)
