"""Error kinds, origins and reasons, and the bodies of a failed push or transform.

Ported from rocicorp/mono `packages/zero-protocol/src/error.ts`, `error-kind-enum.ts`,
`error-origin-enum.ts` and `error-reason-enum.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.
"""

from enum import StrEnum
from typing import Literal

from pydantic import Field, StrictStr

from zero_python.protocol.mutation_id import MutationID
from zero_python.protocol.wire import Number, WireModel
from zero_python.shared.json import JSONValue


class ErrorKind(StrEnum):
    AUTH_INVALIDATED = "AuthInvalidated"
    CLIENT_NOT_FOUND = "ClientNotFound"
    INVALID_CONNECTION_REQUEST = "InvalidConnectionRequest"
    INVALID_CONNECTION_REQUEST_BASE_COOKIE = "InvalidConnectionRequestBaseCookie"
    INVALID_CONNECTION_REQUEST_LAST_MUTATION_ID = "InvalidConnectionRequestLastMutationID"
    INVALID_CONNECTION_REQUEST_CLIENT_DELETED = "InvalidConnectionRequestClientDeleted"
    INVALID_MESSAGE = "InvalidMessage"
    INVALID_PUSH = "InvalidPush"
    PUSH_FAILED = "PushFailed"
    MUTATION_FAILED = "MutationFailed"
    MUTATION_RATE_LIMITED = "MutationRateLimited"
    REBALANCE = "Rebalance"
    REHOME = "Rehome"
    TRANSFORM_FAILED = "TransformFailed"
    UNAUTHORIZED = "Unauthorized"
    VERSION_NOT_SUPPORTED = "VersionNotSupported"
    SCHEMA_VERSION_NOT_SUPPORTED = "SchemaVersionNotSupported"
    SERVER_OVERLOADED = "ServerOverloaded"
    INTERNAL = "Internal"


class ErrorOrigin(StrEnum):
    CLIENT = "client"
    SERVER = "server"
    ZERO_CACHE = "zeroCache"


class ErrorReason(StrEnum):
    DATABASE = "database"
    PARSE = "parse"
    OUT_OF_ORDER_MUTATION = "oooMutation"
    UNSUPPORTED_PUSH_VERSION = "unsupportedPushVersion"
    INTERNAL = "internal"
    HTTP = "http"
    TIMEOUT = "timeout"


type ServerPushFailedReason = Literal[
    ErrorReason.DATABASE,
    ErrorReason.PARSE,
    ErrorReason.OUT_OF_ORDER_MUTATION,
    ErrorReason.UNSUPPORTED_PUSH_VERSION,
    ErrorReason.INTERNAL,
]
type ServerTransformFailedReason = Literal[ErrorReason.DATABASE, ErrorReason.PARSE, ErrorReason.INTERNAL]
type ZeroCacheFailedReason = Literal[ErrorReason.TIMEOUT, ErrorReason.PARSE, ErrorReason.INTERNAL]


class _PushFailedBase(WireModel):
    kind: Literal[ErrorKind.PUSH_FAILED] = ErrorKind.PUSH_FAILED
    details: JSONValue | None = None
    # The mutations that went unprocessed, which can be a tail of the request's.
    mutation_ids: list[MutationID] = Field(alias="mutationIDs")
    message: StrictStr


class ServerPushFailed(_PushFailedBase):
    origin: Literal[ErrorOrigin.SERVER] = ErrorOrigin.SERVER
    reason: ServerPushFailedReason


class ZeroCachePushHTTPFailed(_PushFailedBase):
    origin: Literal[ErrorOrigin.ZERO_CACHE]
    reason: Literal[ErrorReason.HTTP]
    status: Number
    body_preview: StrictStr | None = Field(default=None, alias="bodyPreview")


class ZeroCachePushFailed(_PushFailedBase):
    origin: Literal[ErrorOrigin.ZERO_CACHE]
    reason: ZeroCacheFailedReason


type PushFailedBody = ServerPushFailed | ZeroCachePushHTTPFailed | ZeroCachePushFailed


class _TransformFailedBase(WireModel):
    kind: Literal[ErrorKind.TRANSFORM_FAILED] = ErrorKind.TRANSFORM_FAILED
    details: JSONValue | None = None
    query_ids: list[StrictStr] = Field(alias="queryIDs")
    message: StrictStr


class ServerTransformFailed(_TransformFailedBase):
    origin: Literal[ErrorOrigin.SERVER] = ErrorOrigin.SERVER
    reason: ServerTransformFailedReason


class ZeroCacheTransformHTTPFailed(_TransformFailedBase):
    origin: Literal[ErrorOrigin.ZERO_CACHE]
    reason: Literal[ErrorReason.HTTP]
    status: Number
    body_preview: StrictStr | None = Field(default=None, alias="bodyPreview")


class ZeroCacheTransformFailed(_TransformFailedBase):
    origin: Literal[ErrorOrigin.ZERO_CACHE]
    reason: ZeroCacheFailedReason


type TransformFailedBody = ServerTransformFailed | ZeroCacheTransformHTTPFailed | ZeroCacheTransformFailed
