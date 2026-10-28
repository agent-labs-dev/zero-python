"""What zero-cache POSTs to a query endpoint, and what each query becomes.

Ported from rocicorp/mono `packages/zero-protocol/src/custom-queries.ts` at zero/v1.11.3
(e91e55f05b7a2c631f77238e1dd6fadf1029a538). Apache-2.0, Copyright Rocicorp.
"""

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import StrictStr

from zero_server.protocol.ast import AST
from zero_server.protocol.wire import PlainJSON, RefusesNullOffWire, WireModel, json_model
from zero_server.shared.js import js_stringify_bytes
from zero_server.shared.json import JSONValue


class TransformRequestQuery(WireModel):
    """Parsed in strip mode, so a newer zero-cache can add fields to the request."""

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

    def to_json_bytes(self) -> bytes:
        """`js_stringify_bytes(self.to_json())`, from the text the AST keeps."""
        id_, name = js_stringify_bytes(self.id), js_stringify_bytes(self.name)
        return b'{"id":%b,"name":%b,"ast":%b}' % (id_, name, self.ast.to_json_bytes())


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
