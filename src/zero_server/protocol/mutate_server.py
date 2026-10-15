"""The mutate endpoint's response and query string.

Ported from rocicorp/mono `packages/zero-protocol/src/mutate-server.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.
"""

from typing import Literal

from pydantic import Field, StrictStr

from zero_server.protocol.error import PushFailedBody
from zero_server.protocol.mutation import MutationResponse
from zero_server.protocol.wire import WireModel


class MutateSuccess(WireModel):
    """`user_id` is reported to zero-cache only when the caller set it, `None` included."""

    nullable = frozenset({"user_id"})

    kind: Literal["MutateResponse"] = "MutateResponse"
    user_id: StrictStr | None = Field(default=None, alias="userID")
    mutations: list[MutationResponse]


# The wire body. Handlers see `zero_server.MutateResponse`, what `handle_mutate_request` returns.
type MutateResponse = MutateSuccess | PushFailedBody


class MutateParams(WireModel):
    """The query string zero-cache appends: the upstream schema holding its bookkeeping, and the app."""

    schema_: StrictStr = Field(alias="schema")
    app_id: StrictStr = Field(alias="appID")
