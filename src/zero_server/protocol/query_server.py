"""The query endpoint's response.

Ported from rocicorp/mono `packages/zero-protocol/src/query-server.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.
"""

from typing import Any, Final, Literal

from zero_server.protocol.custom_queries import TransformResponseBody
from zero_server.protocol.error import TransformFailedBody
from zero_server.protocol.wire import dump_json, json_model

_UNSET: Final[Any] = object()


@json_model
class QuerySuccess:
    """`user_id` is reported to zero-cache only when the caller set it, `None` included.

    Reporting it is what makes zero-cache trust the connection's user as server-validated. Only ever
    built here and sent, so a plain class rather than a wire model.
    """

    __slots__ = ("_user_id", "queries")

    kind: Final[Literal["QueryResponse"]] = "QueryResponse"

    def __init__(self, *, queries: TransformResponseBody, user_id: str | None = _UNSET) -> None:
        self.queries = queries
        self._user_id = user_id

    @property
    def user_id(self) -> str | None:  # python-only: reading the response back in Python
        return None if self._user_id is _UNSET else self._user_id

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": "QueryResponse", "queries": [q.to_json() for q in self.queries]}
        if self._user_id is not _UNSET:  # python-only: a response built in Python without a user ID
            out["userID"] = dump_json(self._user_id)
        return out


type QueryResponse = QuerySuccess | TransformFailedBody
