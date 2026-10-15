"""A transaction provider over SQLAlchemy's asyncio engine, with the asyncpg driver.

Ported from rocicorp/mono `packages/zero-server/src/zql-database.ts` and
`packages/zero-server/src/adapters/postgresjs.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from zero_python.protocol.mutation import CleanupResultsArg, CleanupResultsBulkArg, MutationResponse
from zero_python.server.process_mutations import TransactionProviderHooks, TransactionProviderInput
from zero_python.server.transaction import Row, ServerTransaction
from zero_python.shared.js import js_stringify


class PostgresError(Exception):
    """A statement Postgres refused. Named as the postgres.js adapter names its errors upstream."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def ident(name: str) -> str:
    """Quote an identifier as upstream's `sql.ident` does, splitting a dotted name into its parts."""
    return ".".join('"' + part.replace('"', '""') + '"' for part in name.split("."))


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
            raise PostgresError(str(cause)) from error
        return [dict(row) for row in result.mappings()] if result.returns_rows else []


class _Hooks:
    def __init__(self, tx: SQLAlchemyTransaction, transaction_input: TransactionProviderInput) -> None:
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
                    WHERE "clientGroupID" = $1 AND "clientID" = $2 AND "mutationID" <= $3""",
                (args.client_group_id, args.client_id, args.up_to_mutation_id),
            )


class SQLAlchemyDatabase:
    """Runs each mutation in one transaction on `engine`, which must use the asyncpg driver."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def transaction[R](
        self,
        callback: Callable[[ServerTransaction[AsyncConnection], TransactionProviderHooks], Awaitable[R]],
        transaction_input: TransactionProviderInput | None = None,
    ) -> R:
        transaction_input = transaction_input or TransactionProviderInput(
            upstream_schema="", client_group_id="", client_id="", mutation_id=0
        )
        async with self.engine.begin() as connection:
            tx = SQLAlchemyTransaction(connection)
            server_tx = ServerTransaction(tx, transaction_input.client_id, int(transaction_input.mutation_id))
            return await callback(server_tx, _Hooks(tx, transaction_input))
