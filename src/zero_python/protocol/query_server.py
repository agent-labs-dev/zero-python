"""The query endpoint's response.

Ported from rocicorp/mono `packages/zero-protocol/src/query-server.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.
"""

from typing import Literal

from pydantic import Field, StrictStr

from zero_python.protocol.custom_queries import TransformResponseBody
from zero_python.protocol.error import TransformFailedBody
from zero_python.protocol.wire import WireModel


class QuerySuccess(WireModel):
    """`user_id` is reported to zero-cache only when the caller set it, `None` included.

    Reporting it is what makes zero-cache trust the connection's user as server-validated.
    """

    nullable = frozenset({"user_id"})

    kind: Literal["QueryResponse"] = "QueryResponse"
    user_id: StrictStr | None = Field(default=None, alias="userID")
    queries: TransformResponseBody


type QueryResponse = QuerySuccess | TransformFailedBody
