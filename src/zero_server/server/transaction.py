"""The transaction a server mutator receives.

Ported from rocicorp/mono `packages/zero-server/src/custom.ts` (`TransactionImpl`) and
`packages/zql/src/mutate/custom.ts` at zero/v1.10.0 (905d08516ba0caaf314cd28e93a5a47e8acdd482).
Apache-2.0, Copyright Rocicorp.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal, Protocol, overload

if TYPE_CHECKING:
    from zero_server.query.builder import OneQuery, Query
    from zero_server.schema import Schema
    from zero_server.server.crud import Mutate
    from zero_server.server.server_schema import ServerSchema

type Row = dict[str, Any]


class DBTransaction[W](Protocol):
    """One open database transaction. `query` takes SQL with `$1`-style placeholders, as upstream's does."""

    @property
    def wrapped_transaction(self) -> W: ...

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]: ...


class ServerTransaction[W]:
    location: Literal["server"] = "server"
    reason: Literal["authoritative"] = "authoritative"

    def __init__(
        self,
        db_transaction: DBTransaction[W],
        client_id: str,
        mutation_id: int,
        schema: "Schema | None" = None,
        server_schema: "ServerSchema | None" = None,
    ) -> None:
        self.db_transaction = db_transaction
        self.client_id = client_id
        self.mutation_id = mutation_id
        self._schema = schema
        self._server_schema = server_schema

    def _schemas(self, what: str) -> "tuple[Schema, ServerSchema]":
        if self._schema is None or self._server_schema is None:
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
