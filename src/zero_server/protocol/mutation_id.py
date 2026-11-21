"""Ported from rocicorp/mono `packages/zero-protocol/src/mutation-id.ts` at zero/v1.12.0
(d489a3218ad603b3fd65e33cec76ae77cea7c7af). Apache-2.0, Copyright Rocicorp.
"""

from pydantic import Field, StrictStr

from zero_server.protocol.wire import Number, WireModel


class MutationID(WireModel):
    id: Number
    client_id: StrictStr = Field(alias="clientID")
