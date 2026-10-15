"""The mutate endpoint's response and query string.

Ported from rocicorp/mono `packages/zero-protocol/src/mutate-server.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.
"""

from collections.abc import Mapping
from typing import Literal

from pydantic import Field, StrictStr

from zero_server.protocol.error import PushFailedBody
from zero_server.protocol.mutation import MutationResponse
from zero_server.protocol.wire import WireModel


class MutateSuccess(WireModel):
    """`user_id` is reported to zero-cache only when the caller set it, `None` included."""

    nullable = frozenset({"user_id"})

    kind: Literal["MutateResponse"] = "MutateResponse"
    mutations: list[MutationResponse]
    user_id: StrictStr | None = Field(default=None, alias="userID")


# The wire body. Handlers see `zero_server.MutateResponse`, what `handle_mutate_request` returns.
type MutateResponse = MutateSuccess | PushFailedBody


class MutateParams(WireModel):
    """The query string zero-cache appends: the upstream schema holding its bookkeeping, and the app."""

    schema_: StrictStr = Field(alias="schema")
    app_id: StrictStr = Field(alias="appID")

    @classmethod
    def from_query(cls, query: Mapping[str, str]) -> "MutateParams":
        """Parse the query string. Raises `ValidationError` on a malformed one."""
        schema, app_id = query.get("schema"), query.get("appID")
        # Field names pydantic would also read are left to it.
        if type(schema) is str and type(app_id) is str and "schema_" not in query and "app_id" not in query:
            return cls.trusted(schema_=schema, app_id=app_id)
        return cls.from_json(dict(query))
