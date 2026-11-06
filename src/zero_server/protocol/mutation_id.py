"""Ported from rocicorp/mono `packages/zero-protocol/src/mutation-id.ts` at zero/v1.11.4
(be94346faaf4ee7e7b2cf983f6e5accae3a6b4fb). Apache-2.0, Copyright Rocicorp.
"""

from pydantic import Field, StrictStr

from zero_server.protocol.wire import Number, WireModel


class MutationID(WireModel):
    id: Number
    client_id: StrictStr = Field(alias="clientID")
