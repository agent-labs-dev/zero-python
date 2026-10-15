"""What zero-cache POSTs to a mutate endpoint.

Ported from rocicorp/mono `packages/zero-protocol/src/push.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.
"""

from pydantic import Field, StrictStr

from zero_server.protocol.mutation import Mutation
from zero_server.protocol.wire import Number, WireModel


class PushBody(WireModel):
    """Parsed in passthrough mode upstream, so keys it does not declare are ignored, not refused."""

    client_group_id: StrictStr = Field(alias="clientGroupID")
    mutations: list[Mutation]
    push_version: Number = Field(alias="pushVersion")
    # Only CRUD mutations are tied to a schema version.
    schema_version: Number | None = Field(default=None, alias="schemaVersion")
    timestamp: Number
    request_id: StrictStr = Field(alias="requestID")
    # Deprecated upstream: auth is per client group now, through connect and updateAuth.
    auth: StrictStr | None = None
    traceparent: StrictStr | None = None
