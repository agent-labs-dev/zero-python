"""What zero-cache POSTs to a query endpoint, and what each query becomes.

Ported from rocicorp/mono `packages/zero-protocol/src/custom-queries.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.
"""

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import StrictStr

from zero_server.protocol.ast import AST
from zero_server.protocol.wire import PlainJSON, RefusesNullOffWire, StrictWireModel, json_model
from zero_server.shared.json import JSONValue


class TransformRequestQuery(StrictWireModel):
    """Parsed in upstream's default (strict) mode, so an undeclared key fails the whole request."""

    id: StrictStr
    name: StrictStr
    args: PlainJSON[list[JSONValue]]


type TransformRequestBody = list[TransformRequestQuery]
type TransformRequestMessage = tuple[Literal["transform"], TransformRequestBody]


@json_model
@dataclass(slots=True, kw_only=True)
class TransformedQuery:
    """Only ever built here and sent, so a plain class: one per query in every response."""

    id: str
    name: str
    ast: AST

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "ast": self.ast.to_json()}


class AppErroredQuery(RefusesNullOffWire):
    error: Literal["app"] = "app"
    id: StrictStr
    name: StrictStr
    message: StrictStr | None = None
    details: PlainJSON[JSONValue | None] = None


class ParseErroredQuery(RefusesNullOffWire):
    error: Literal["parse"] = "parse"
    id: StrictStr
    name: StrictStr
    message: StrictStr
    details: PlainJSON[JSONValue | None] = None


type ErroredQuery = AppErroredQuery | ParseErroredQuery
type TransformResponseBody = list[TransformedQuery | ErroredQuery]
