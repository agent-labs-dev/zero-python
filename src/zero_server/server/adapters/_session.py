"""A session: one pooled connection that a push's transactions run on, one after another."""

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Protocol

from zero_server.server.transaction import DBTransaction

logger = logging.getLogger("zero_server.adapters")


class RunTransaction[C, W](Protocol):
    """Runs `fn` in one transaction on `connection`, as the adapter's own `transaction` does."""

    def __call__[R](self, connection: C, fn: Callable[[DBTransaction[W]], Awaitable[R]], /) -> Awaitable[R]: ...


class SessionConnection[C, W]:
    """A `DBConnection` whose transactions all run on one connection from a pool.

    The connection is taken from the pool for the first transaction. After a transaction fails it goes
    back, as the connection may be what failed, and the next transaction takes a fresh one.
    """

    def __init__(
        self,
        acquire: Callable[[], Awaitable[C]],
        release: Callable[[C], Awaitable[object]],
        run: RunTransaction[C, W],
    ) -> None:
        self._acquire = acquire
        self._release = release
        self._run = run
        self._connection: C | None = None

    async def transaction[R](self, fn: Callable[[DBTransaction[W]], Awaitable[R]]) -> R:
        if self._connection is None:
            self._connection = await self._acquire()
        try:
            return await self._run(self._connection, fn)
        except BaseException:
            await self.release()
            raise

    async def release(self) -> None:
        connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            await self._release(connection)
        except Exception as error:
            # The push's mutations have committed; the pool failing to take the connection back
            # must not report them failed.
            logger.warning("Failed to return a connection to the pool", exc_info=error)


@asynccontextmanager
async def session[C, W](
    acquire: Callable[[], Awaitable[C]],
    release: Callable[[C], Awaitable[object]],
    run: RunTransaction[C, W],
) -> AsyncGenerator[SessionConnection[C, W]]:
    connection = SessionConnection(acquire, release, run)
    try:
        yield connection
    finally:
        await connection.release()
