"""Mutations as a push carries them, and the result recorded for each.

Ported from rocicorp/mono `packages/zero-protocol/src/mutation.ts` and `mutation-type-enum.ts` at
zero/v1.11.3 (e91e55f05b7a2c631f77238e1dd6fadf1029a538). Apache-2.0, Copyright Rocicorp.
"""

from typing import Annotated, Any, Literal

from pydantic import Field, StrictBool, StrictStr

from zero_server.protocol.ast import PrimaryKey, Row
from zero_server.protocol.mutation_id import MutationID
from zero_server.protocol.wire import Number, PlainJSON, RefusesNullOffWire, StrictWireModel, WireModel
from zero_server.shared.json import JSONValue

CRUD_MUTATION_NAME = "_zero_crud"

# zero-cache sends this to delete results its clients have acknowledged. It is not a client mutation.
CLEANUP_RESULTS_MUTATION_NAME = "_zero_cleanupResults"

type NonEmptyKey = Annotated[PrimaryKey, Field(min_length=1)]
type PrimaryKeyValue = StrictStr | Number | StrictBool


class CleanupResultsLegacyArg(StrictWireModel):
    """The oldest form, with no `type`. Treated as `single`."""

    client_group_id: StrictStr = Field(alias="clientGroupID")
    client_id: StrictStr = Field(alias="clientID")
    up_to_mutation_id: Number = Field(alias="upToMutationID")


class CleanupResultsSingleArg(StrictWireModel):
    type: Literal["single"]
    client_group_id: StrictStr = Field(alias="clientGroupID")
    client_id: StrictStr = Field(alias="clientID")
    up_to_mutation_id: Number = Field(alias="upToMutationID")


class CleanupResultsBulkArg(StrictWireModel):
    type: Literal["bulk"]
    client_group_id: StrictStr = Field(alias="clientGroupID")
    client_ids: Annotated[list[StrictStr], Field(min_length=1)] = Field(alias="clientIDs")


type CleanupResultsArg = CleanupResultsLegacyArg | CleanupResultsSingleArg | CleanupResultsBulkArg


class InsertOp(WireModel):
    op: Literal["insert"]
    table_name: StrictStr = Field(alias="tableName")
    primary_key: NonEmptyKey = Field(alias="primaryKey")
    value: Row


class UpsertOp(WireModel):
    op: Literal["upsert"]
    table_name: StrictStr = Field(alias="tableName")
    primary_key: NonEmptyKey = Field(alias="primaryKey")
    value: Row


class UpdateOp(WireModel):
    op: Literal["update"]
    table_name: StrictStr = Field(alias="tableName")
    primary_key: NonEmptyKey = Field(alias="primaryKey")
    value: Row


class DeleteOp(WireModel):
    op: Literal["delete"]
    table_name: StrictStr = Field(alias="tableName")
    primary_key: NonEmptyKey = Field(alias="primaryKey")
    value: dict[str, PrimaryKeyValue]


type CRUDOp = Annotated[InsertOp | UpsertOp | UpdateOp | DeleteOp, Field(discriminator="op")]


class CRUDMutationArg(WireModel):
    ops: list[CRUDOp]


class CRUDMutation(WireModel):
    type: Literal["crud"]
    id: Number
    client_id: StrictStr = Field(alias="clientID")
    name: Literal["_zero_crud"]
    args: tuple[CRUDMutationArg]
    timestamp: Number


class CustomMutation(WireModel):
    type: Literal["custom"]
    id: Number
    client_id: StrictStr = Field(alias="clientID")
    name: StrictStr
    args: PlainJSON[list[JSONValue]]
    timestamp: Number


type Mutation = Annotated[CRUDMutation | CustomMutation, Field(discriminator="type")]


class MutationOk(RefusesNullOffWire):
    # Whatever the mutator returned, if anything.
    data: PlainJSON[JSONValue | None] = None


class MutationAppError(RefusesNullOffWire):
    error: Literal["app"] = "app"
    message: StrictStr | None = None
    details: PlainJSON[JSONValue | None] = None


class MutationZeroError(RefusesNullOffWire):
    # `oooMutation` is reported as a failed push now; the literal stays for older peers.
    error: Literal["oooMutation", "alreadyProcessed"]
    details: PlainJSON[JSONValue | None] = None


type MutationResult = MutationAppError | MutationZeroError | MutationOk


class MutationResponse(WireModel):
    id: MutationID
    result: MutationResult

    def _ordered(self, dumped: dict[str, Any]) -> dict[str, Any]:
        # Upstream writes a response's ID `clientID` first, unlike the IDs a failed push lists.
        mutation_id = dumped["id"]
        return {**dumped, "id": {"clientID": mutation_id["clientID"], "id": mutation_id["id"]}}
