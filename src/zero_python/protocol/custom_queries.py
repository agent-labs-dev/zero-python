"""What zero-cache POSTs to a query endpoint, and what each query becomes.

Ported from rocicorp/mono `packages/zero-protocol/src/custom-queries.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.
"""

from typing import Literal

from pydantic import StrictStr

from zero_python.protocol.ast import AST
from zero_python.protocol.wire import StrictWireModel, WireModel
from zero_python.shared.json import JSONValue


class TransformRequestQuery(StrictWireModel):
    """Parsed in upstream's default (strict) mode, so an undeclared key fails the whole request."""

    id: StrictStr
    name: StrictStr
    args: list[JSONValue]


type TransformRequestBody = list[TransformRequestQuery]
type TransformRequestMessage = tuple[Literal["transform"], TransformRequestBody]


class TransformedQuery(WireModel):
    id: StrictStr
    name: StrictStr
    ast: AST


class AppErroredQuery(WireModel):
    error: Literal["app"] = "app"
    id: StrictStr
    name: StrictStr
    message: StrictStr | None = None
    details: JSONValue | None = None


class ParseErroredQuery(WireModel):
    error: Literal["parse"] = "parse"
    id: StrictStr
    name: StrictStr
    message: StrictStr
    details: JSONValue | None = None


type ErroredQuery = AppErroredQuery | ParseErroredQuery
type TransformResponseBody = list[TransformedQuery | ErroredQuery]
