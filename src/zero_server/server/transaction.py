"""The transaction a server mutator receives.

Ported from rocicorp/mono `packages/zero-server/src/custom.ts` (`TransactionImpl`),
`packages/zero-server/src/closable-db-transaction.ts` and `packages/zql/src/mutate/custom.ts` at zero/v1.11.3
(e91e55f05b7a2c631f77238e1dd6fadf1029a538). Apache-2.0, Copyright Rocicorp.
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, Literal, Protocol, overload

if TYPE_CHECKING:
    from zero_server.query.builder import OneQuery, Query
    from zero_server.schema import Schema
    from zero_server.server.crud import Mutate
    from zero_server.server.server_schema import ServerSchema

type Row = dict[str, Any]

type RetryPredicate = Callable[[BaseException, int], bool | Awaitable[bool]]
"""Decides whether to run a mutator again after a run fails. Gets what was raised and the number of the
re-run being decided: 1 after the first failure. Return `True` to re-run, or an awaitable of `True` to
re-run once it resolves, which is how to back off. Anything else, including raising, counts as no."""


class Queryable(Protocol):
    """Runs one statement. `query` takes SQL with `$1`-style placeholders, as upstream's does."""

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]: ...


class DBTransaction[W](Queryable, Protocol):
    """One open database transaction."""

    @property
    def wrapped_transaction(self) -> W: ...


def _transaction_ended() -> Exception:
    return Exception(
        "This transaction has ended and can no longer be used. "
        "A tx.retryOn() predicate runs after its transaction is over."
    )


class ClosableDBTransaction[W]:
    """A `DBTransaction` that stops working once closed. The mutator's `tx` is built on it, so code that
    outlives the transaction cannot reach a connection that has been committed, rolled back or returned
    to the pool."""

    def __init__(self, inner: DBTransaction[W]) -> None:
        self._inner = inner
        self._closed = False

    def close(self) -> None:
        self._closed = True

    @property
    def wrapped_transaction(self) -> W:
        if self._closed:
            raise _transaction_ended()
        return self._inner.wrapped_transaction

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        if self._closed:
            raise _transaction_ended()
        return await self._inner.query(sql, params)


class ServerTransaction[W]:
    location: Literal["server"] = "server"
    reason: Literal["authoritative"] = "authoritative"

    def __init__(
        self,
        db_transaction: DBTransaction[W],
        client_id: str,
        # As the client sent it: a JavaScript number, so not necessarily an integer.
        mutation_id: int | float,
        schema: "Schema | None" = None,
        server_schema: "ServerSchema | None" = None,
        add_retry_predicate: Callable[[RetryPredicate], None] | None = None,
    ) -> None:
        self.db_transaction = db_transaction
        self.client_id = client_id
        self.mutation_id = mutation_id
        self._schema = schema
        self._server_schema = server_schema
        self._add_retry_predicate = add_retry_predicate

    def retry_on(self, should_retry: RetryPredicate) -> None:
        """If this run fails, ask `should_retry` whether to run the mutator again in a fresh transaction.

        It sees any failure of the run: what the mutator raises, and errors the database raises at
        `COMMIT`, which the mutator cannot catch. Everything the failed run wrote is rolled back, and the
        re-run reads a new snapshot. The mutator is re-run if any predicate registered in the run says
        yes; they are all asked at once, and the first yes starts the re-run. A predicate covers one run,
        so the re-run must register again. Within one push the mutator runs at most 5 times. The
        predicate runs after the transaction is over, so it cannot use `tx`.
        """
        if self._add_retry_predicate is None:
            # Fail loudly: a predicate nothing will consult would silently turn every retryable failure
            # into a recorded error.
            raise Exception(
                "tx.retryOn() is not supported on this transaction: nothing will re-run its mutator. "
                "A custom Database must pass TransactionProviderInput.addRetryPredicate on to createTransaction."
            )
        self._add_retry_predicate(should_retry)

    def _schemas(self, what: str) -> "tuple[Schema, ServerSchema]":
        if self._schema is None or self._server_schema is None:  # python-only: a database built without one
            raise RuntimeError(f"{what} needs a schema: pass one to the database")
        return self._schema, self._server_schema

    @property
    def mutate(self) -> "Mutate":
        """CRUD on the schema's tables, in client names."""
        from zero_server.server.crud import Mutate

        schema, server_schema = self._schemas("tx.mutate")
        return Mutate(schema, self.db_transaction, server_schema)

    @overload
    async def run[R](self, query: "OneQuery[R, Any]") -> R | None: ...
    @overload
    async def run[R](self, query: "Query[R, Any]") -> list[R]: ...
    async def run(self, query: "Query[Any, Any]") -> Any:
        """Run a ZQL query inside this transaction, against Postgres: a list of rows, or for a `one()`
        query the row or `None`. Relationships come back nested under each row, under their names."""
        from zero_server.server.pg_query_executor import execute_postgres_query

        schema, server_schema = self._schemas("tx.run")
        return await execute_postgres_query(self.db_transaction, query.ast, query.format, schema, server_schema)
