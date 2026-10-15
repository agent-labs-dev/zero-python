"""The transaction a server mutator receives.

Ported from rocicorp/mono `packages/zero-server/src/custom.ts` (`TransactionImpl`) and
`packages/zql/src/mutate/custom.ts` at zero/v1.10.0 (905d08516ba0caaf314cd28e93a5a47e8acdd482).
Apache-2.0, Copyright Rocicorp.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from zero_python.server.crud import Mutate

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
        mutate: "Mutate | None" = None,
    ) -> None:
        self.db_transaction = db_transaction
        self.client_id = client_id
        self.mutation_id = mutation_id
        self._mutate = mutate

    @property
    def mutate(self) -> "Mutate":
        """CRUD on the schema's tables. Needs a database built with a schema."""
        if self._mutate is None:
            raise RuntimeError("tx.mutate needs a schema: pass one to the database")
        return self._mutate
