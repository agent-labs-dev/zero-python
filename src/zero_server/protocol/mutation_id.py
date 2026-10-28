"""Ported from rocicorp/mono `packages/zero-protocol/src/mutation-id.ts` at zero/v1.11.3
(e91e55f05b7a2c631f77238e1dd6fadf1029a538). Apache-2.0, Copyright Rocicorp.
"""

from pydantic import Field, StrictStr

from zero_server.protocol.wire import Number, WireModel


class MutationID(WireModel):
    id: Number
    client_id: StrictStr = Field(alias="clientID")
