"""A transaction provider over any Postgres driver, with `tx.mutate` and `tx.run` for mutators.

Ported from rocicorp/mono `packages/zero-server/src/zql-database.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.

A driver plugs in as a `DBConnection`: something that opens a transaction and hands back a
`DBTransaction` whose `query` takes `$1`-style placeholders. `zero_server.server.adapters` has one per
supported driver.
"""

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol, overload

from zero_server.protocol.mutation import CleanupResultsArg, CleanupResultsBulkArg, MutationResponse
from zero_server.query.builder import OneQuery, Query
from zero_server.schema import Schema
from zero_server.server.process_mutations import TransactionProviderHooks, TransactionProviderInput
from zero_server.server.server_schema import ServerSchema, get_server_schema
from zero_server.server.transaction import DBTransaction, ServerTransaction
from zero_server.shared.js import js_number, js_stringify
from zero_server.z2s.sql import ident


class PostgresError(Exception):
    """A statement Postgres refused, carrying its primary message.

    Named as the postgres.js adapter names its errors upstream, so the error a client sees for a refused
    statement is the same whichever server ran it.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class DBConnection[W](Protocol):
    """Opens one transaction, runs `fn` in it, and commits when `fn` returns or rolls back when it raises.

    A connection may also have a `session()`: an async context manager yielding a `DBConnection` whose
    transactions all run on one connection from the pool, in turn, as a push's mutations do.
    """

    async def transaction[R](self, fn: Callable[[DBTransaction[W]], Awaitable[R]]) -> R: ...


class _SessionOpener[W](Protocol):
    def session(self) -> AbstractAsyncContextManager[DBConnection[W]]: ...


class _Hooks:
    def __init__(self, tx: DBTransaction[Any], transaction_input: TransactionProviderInput) -> None:
        self._tx = tx
        self._input = transaction_input
        self._schema = ident(transaction_input.upstream_schema)

    async def update_client_mutation_id(self) -> int:
        rows = await self._tx.query(
            f"""INSERT INTO {self._schema}.clients
                    as current ("clientGroupID", "clientID", "lastMutationID")
                VALUES ($1, $2, $3)
                ON CONFLICT ("clientGroupID", "clientID")
                DO UPDATE SET "lastMutationID" = current."lastMutationID" + 1
                RETURNING "lastMutationID\"""",
            (self._input.client_group_id, self._input.client_id, 1),
        )
        return int(rows[0]["lastMutationID"])

    async def write_mutation_result(self, result: MutationResponse) -> None:
        await self._tx.query(
            f"""INSERT INTO {self._schema}.mutations
                    ("clientGroupID", "clientID", "mutationID", "result")
                VALUES ($1, $2, $3, $4::text::json)""",
            (
                self._input.client_group_id,
                result.id.client_id,
                result.id.id,
                js_stringify(result.result.to_json()),
            ),
        )

    async def delete_mutation_results(self, args: CleanupResultsArg) -> None:
        if isinstance(args, CleanupResultsBulkArg):
            await self._tx.query(
                f"""DELETE FROM {self._schema}."mutations"
                    WHERE "clientGroupID" = $1 AND "clientID" = ANY($2)""",
                (args.client_group_id, list(args.client_ids)),
            )
        else:
            await self._tx.query(
                f"""DELETE FROM {self._schema}."mutations"
                    WHERE "clientGroupID" = $1 AND "clientID" = $2 AND "mutationID" <= $3::text::bigint""",
                # postgres.js sends the number as JavaScript prints it, which a fraction or a value past
                # bigint's range fails to parse.
                (args.client_group_id, args.client_id, js_number(args.up_to_mutation_id)),
            )


class ZQLDatabase[W]:
    """Runs each mutation in one transaction of `connection`.

    With a `schema`, mutators get `tx.mutate` and `tx.run`, and the first transaction checks the schema
    against the database's columns.
    """

    def __init__(self, connection: DBConnection[W], schema: Schema | None = None) -> None:
        self.connection = connection
        self.schema = schema
        self._server_schema: ServerSchema | None = None

    async def transaction[R](
        self,
        callback: Callable[[ServerTransaction[W], TransactionProviderHooks], Awaitable[R]],
        transaction_input: TransactionProviderInput | None = None,
    ) -> R:
        return await self._transaction(self.connection, callback, transaction_input)

    @asynccontextmanager
    async def session(self) -> "AsyncGenerator[_Session[W] | ZQLDatabase[W]]":
        """This database, with every transaction on one connection until the block ends.

        `handle_mutate_request` runs a push's mutations in one, so the push takes a connection from the
        pool once rather than once per mutation. A connection without sessions runs as before.
        """
        if not hasattr(self.connection, "session"):
            yield self
            return
        opener: _SessionOpener[W] = self.connection  # ty: ignore[invalid-assignment]
        async with opener.session() as connection:
            yield _Session(self, connection)

    async def _transaction[R](
        self,
        connection: DBConnection[W],
        callback: Callable[[ServerTransaction[W], TransactionProviderHooks], Awaitable[R]],
        transaction_input: TransactionProviderInput | None,
    ) -> R:
        transaction_input = transaction_input or TransactionProviderInput(
            upstream_schema="", client_group_id="", client_id="", mutation_id=0
        )

        async def run(tx: DBTransaction[W]) -> R:
            if self.schema is not None and self._server_schema is None:
                self._server_schema = await get_server_schema(tx, self.schema)
            server_tx = ServerTransaction(
                tx, transaction_input.client_id, int(transaction_input.mutation_id), self.schema, self._server_schema
            )
            return await callback(server_tx, _Hooks(tx, transaction_input))

        return await connection.transaction(run)

    @overload
    async def run[R](self, query: OneQuery[R, Any]) -> R | None: ...
    @overload
    async def run[R](self, query: Query[R, Any]) -> list[R]: ...
    async def run(self, query: Query[Any, Any]) -> Any:
        """Run a ZQL query in a transaction of its own; see `ServerTransaction.run`."""

        async def run(tx: ServerTransaction[W], _hooks: TransactionProviderHooks) -> Any:
            return await tx.run(query)

        return await self.transaction(run)


class _Session[W]:
    """A `ZQLDatabase`'s transactions, on the one connection of a session of its `DBConnection`."""

    def __init__(self, database: ZQLDatabase[W], connection: DBConnection[W]) -> None:
        self._database = database
        self._connection = connection

    async def transaction[R](
        self,
        callback: Callable[[ServerTransaction[W], TransactionProviderHooks], Awaitable[R]],
        transaction_input: TransactionProviderInput | None = None,
    ) -> R:
        return await self._database._transaction(self._connection, callback, transaction_input)
