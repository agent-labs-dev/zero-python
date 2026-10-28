"""SQLAlchemy's asyncio engine, with the asyncpg or the psycopg driver.

Needs the `zero-server[sqlalchemy]` extra, and the driver the engine's URL names.
"""

from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.pool import PoolProxiedConnection

from zero_server.schema import Schema
from zero_server.server.adapters._session import SessionConnection, session
from zero_server.server.transaction import DBTransaction, Row
from zero_server.server.transaction_options import IsolationLevel, set_isolation_statement
from zero_server.server.zql_database import PostgresError, ZQLDatabase, well_formed_params


class SQLAlchemyTransaction:
    """One transaction of an `AsyncConnection`.

    psycopg's paramstyle is `%s`, so on psycopg a statement goes through psycopg's raw cursor on the
    same connection, inside the transaction SQLAlchemy opened. On asyncpg, SQLAlchemy defers `BEGIN` to
    the first statement it runs itself, so until the transaction is open a statement goes through
    `exec_driver_sql`, which hands the `$1` placeholders to asyncpg as they are. After that, statements
    go straight to the asyncpg connection, as the asyncpg adapter sends them. In a push's session,
    `BEGIN` goes out before the callback runs, so every statement does.
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
            result = await self._connection.exec_driver_sql(sql, tuple(well_formed_params(params)))
        except DBAPIError as error:
            cause = error.orig.__cause__ if error.orig is not None and error.orig.__cause__ is not None else error.orig
            # asyncpg's `message` is the primary message alone, which is all postgres.js reports.
            message = getattr(cause, "message", None)
            raise PostgresError(message if isinstance(message, str) else str(cause)) from error
        return [dict(row) for row in result.mappings()] if result.returns_rows else []


async def sqlalchemy_transaction(connection: AsyncConnection) -> SQLAlchemyTransaction:
    """Wrap a connection that is already in a transaction, such as one an `AsyncSession` holds."""
    driver = connection.dialect.driver
    if driver not in ("asyncpg", "psycopg"):  # python-only: the drivers this adapter supports
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
    def __init__(self, engine: AsyncEngine, isolation_level: IsolationLevel | None = None) -> None:
        self.engine = engine
        self._isolation_level = isolation_level

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        async with self.engine.connect() as connection:
            await _settle(connection)
            # Autocommit, so SQLAlchemy begins no transaction; the pool resets it when the connection returns.
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            return await (await sqlalchemy_transaction(connection)).query(sql, params)

    async def transaction[R](self, fn: Callable[[DBTransaction[AsyncConnection]], Awaitable[R]]) -> R:
        async with self.engine.connect() as connection:
            await _settle(connection)
            async with connection.begin():
                return await fn(await _begun(connection, self._isolation_level))

    async def _transaction[R](
        self, connection: AsyncConnection, fn: Callable[[DBTransaction[AsyncConnection]], Awaitable[R]]
    ) -> R:
        await _settle(connection)
        async with connection.begin():
            if connection.dialect.driver == "asyncpg":
                await _send_begin(await connection.get_raw_connection())
            return await fn(await _begun(connection, self._isolation_level))

    def session(self) -> AbstractAsyncContextManager[SessionConnection[AsyncConnection, AsyncConnection]]:
        """One connection for a push's transactions, checked back into the pool once, at the end."""
        return session(self.engine.connect, _close, self._transaction)


async def _settle(connection: AsyncConnection) -> None:
    """Commit a transaction the pool handed the connection out in, keeping what it set.

    A pool `checkout` hook that runs a statement through the driver opens one. Left open, switching to
    autocommit fails on psycopg and rolls it back on asyncpg, and a transaction's isolation level can't be set.
    """
    driver_connection = (await connection.get_raw_connection()).driver_connection
    assert driver_connection is not None
    if connection.dialect.driver == "asyncpg":
        in_transaction = driver_connection.is_in_transaction()
    else:
        from psycopg.pq import TransactionStatus

        in_transaction = driver_connection.info.transaction_status == TransactionStatus.INTRANS
    if in_transaction:
        # SQLAlchemy sends no `BEGIN` of its own here, so this commits the transaction already open.
        await (await connection.begin()).commit()


async def _begun(connection: AsyncConnection, isolation_level: IsolationLevel | None) -> SQLAlchemyTransaction:
    tx = await sqlalchemy_transaction(connection)
    if isolation_level is not None:
        await tx.query(set_isolation_statement(isolation_level))
    return tx


async def _close(connection: AsyncConnection) -> None:
    await connection.close()


async def _send_begin(pooled: PoolProxiedConnection) -> None:
    """Have SQLAlchemy's asyncpg dialect send `BEGIN` now, so every statement can go straight to asyncpg.

    The dialect defers `BEGIN` to the first statement it runs itself and has no public way to send it
    sooner; that first statement through SQLAlchemy cost a push of 100 puts about 20%.
    """
    # Private: the asyncpg dialect's `_start_transaction`. A public way to begin, or its removal, retires this.
    start = getattr(pooled.dbapi_connection, "_start_transaction", None)
    if start is not None:
        await start()


class SQLAlchemyDatabase(ZQLDatabase[AsyncConnection]):
    """A `ZQLDatabase` on `engine`, whose URL names the asyncpg or the psycopg driver
    (`postgresql+asyncpg://` or `postgresql+psycopg://`)."""

    def __init__(
        self, engine: AsyncEngine, schema: Schema | None = None, *, isolation_level: IsolationLevel | None = None
    ) -> None:
        super().__init__(SQLAlchemyConnection(engine, isolation_level), schema)
