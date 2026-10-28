"""A transaction provider over any Postgres driver, with `tx.mutate` and `tx.run` for mutators.

Ported from rocicorp/mono `packages/zero-server/src/zql-database.ts`, with the server schema cache of
`CRUDMutatorFactory` in `custom.ts`, at zero/v1.11.3 (e91e55f05b7a2c631f77238e1dd6fadf1029a538).
Apache-2.0, Copyright Rocicorp.

A driver plugs in as a `DBConnection`: something that opens a transaction and hands back a
`DBTransaction` whose `query` takes `$1`-style placeholders. `zero_server.server.adapters` has one per
supported driver.
"""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol, overload

from zero_server.protocol.mutation import CleanupResultsArg, CleanupResultsBulkArg, MutationResponse
from zero_server.query.builder import OneQuery, Query
from zero_server.schema import Schema
from zero_server.server.process_mutations import TransactionProviderHooks, TransactionProviderInput
from zero_server.server.server_schema import ServerSchema, get_server_schema
from zero_server.server.transaction import ClosableDBTransaction, DBTransaction, Queryable, ServerTransaction
from zero_server.shared.js import has_surrogate, js_number, js_stringify, to_well_formed
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

    It may also have `query`, with `DBTransaction.query`'s signature: one statement on the connection
    with no transaction of its own. `ZQLDatabase.run` then reads without `BEGIN`/`COMMIT`.
    """

    async def transaction[R](self, fn: Callable[[DBTransaction[W]], Awaitable[R]]) -> R: ...


def _ill_formed(value: Any) -> bool:
    if type(value) is str:
        return has_surrogate(value)
    return isinstance(value, list | tuple) and any(_ill_formed(item) for item in value)


def _well_formed(value: Any) -> Any:
    if type(value) is str:
        return to_well_formed(value)
    if isinstance(value, list | tuple):
        return [_well_formed(item) for item in value]
    return value


def well_formed_params(params: Sequence[Any]) -> Sequence[Any]:
    """`params` with each lone surrogate in a string replaced by U+FFFD, for an adapter to bind.

    postgres.js encodes a string parameter to UTF-8, which replaces them; Python's drivers refuse them.
    Most parameters are ASCII strings or not strings at all, which the loop passes over without a call.
    """
    for p in params:
        kind = type(p)
        if (kind is str and not p.isascii()) or kind is list or kind is tuple:
            if any(_ill_formed(q) for q in params):
                return [_well_formed(q) for q in params]
            break
    return params


class _SessionOpener[W](Protocol):
    def session(self) -> AbstractAsyncContextManager[DBConnection[W]]: ...


class _Hooks:
    def __init__(self, tx: DBTransaction[Any], transaction_input: TransactionProviderInput) -> None:
        self._tx = tx
        self._input = transaction_input

    @property
    def _schema(self) -> str:
        # Escaped as each statement is written, inside the transaction, where upstream refuses a bad name.
        return ident(self._input.upstream_schema)

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
        # The fetch in flight, so concurrent cold callers share one catalog query.
        self._server_schema_fetch: asyncio.Future[ServerSchema] | None = None

    async def _get_or_fetch_server_schema(self, queryable: Queryable, schema: Schema) -> ServerSchema:
        if self._server_schema is not None:
            return self._server_schema
        fetch = self._server_schema_fetch
        if fetch is None:
            fetch = self._server_schema_fetch = asyncio.ensure_future(get_server_schema(queryable, schema))
            fetch.add_done_callback(self._forget_failed_fetch)
        # Shielded: a caller giving up must not cancel the fetch the others wait on.
        self._server_schema = await asyncio.shield(fetch)
        return self._server_schema

    def _forget_failed_fetch(self, fetch: "asyncio.Future[ServerSchema]") -> None:
        # Don't poison the cache with a transient failure.
        if fetch.cancelled() or fetch.exception() is not None:
            self._server_schema_fetch = None

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
            # What the mutator's `tx` uses. Closed once the callback settles, so a `tx` that outlives its
            # transaction, such as one captured by a `tx.retry_on()` predicate, raises instead of reaching
            # a connection that may already be back in the pool.
            mutator_tx = ClosableDBTransaction(tx)
            try:
                server_schema = (
                    None if self.schema is None else await self._get_or_fetch_server_schema(mutator_tx, self.schema)
                )
                server_tx = ServerTransaction(
                    mutator_tx,
                    transaction_input.client_id,
                    transaction_input.mutation_id,
                    self.schema,
                    server_schema,
                    transaction_input.add_retry_predicate,
                )
                return await callback(server_tx, _Hooks(tx, transaction_input))
            finally:
                mutator_tx.close()

        return await connection.transaction(run)

    @overload
    async def run[R](self, query: OneQuery[R, Any]) -> R | None: ...
    @overload
    async def run[R](self, query: Query[R, Any]) -> list[R]: ...
    async def run(self, query: Query[Any, Any]) -> Any:
        """Run a single read query; see `ServerTransaction.run` for what it returns.

        When the connection has `query`, the SQL goes out as a bare statement with no `BEGIN`/`COMMIT`
        around it, so Postgres releases its locks as soon as it finishes. Otherwise it runs in a
        transaction of its own. For several reads to see one snapshot, run them with `tx.run` inside
        `transaction`.
        """
        connection = self.connection
        if hasattr(connection, "query"):
            target: Queryable = connection  # ty: ignore[invalid-assignment]
            return await self._run_on(target, query)

        async def run(tx: DBTransaction[W]) -> Any:
            return await self._run_on(tx, query)

        return await connection.transaction(run)

    async def _run_on(self, target: Queryable, query: Query[Any, Any]) -> Any:
        from zero_server.server.pg_query_executor import execute_postgres_query

        if self.schema is None:  # python-only: a database built without one
            raise RuntimeError("tx.run needs a schema: pass one to the database")
        server_schema = await self._get_or_fetch_server_schema(target, self.schema)
        return await execute_postgres_query(target, query.ast, query.format, self.schema, server_schema)


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
