"""Ported from rocicorp/mono `packages/zero-protocol/src/mutation-id.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.
"""

from pydantic import Field, StrictStr

from zero_server.protocol.wire import Number, WireModel


class MutationID(WireModel):
    id: Number
    client_id: StrictStr = Field(alias="clientID")
