"""The query endpoint: turn each named query zero-cache forwards into its AST, in server names.

Ported from rocicorp/mono `packages/zero-server/src/queries/process-queries.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.
"""

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import TypeAdapter, ValidationError

from zero_server.protocol.ast import AST, map_ast
from zero_server.protocol.custom_queries import (
    AppErroredQuery,
    ParseErroredQuery,
    TransformedQuery,
    TransformRequestMessage,
    TransformRequestQuery,
)
from zero_server.protocol.error import ErrorReason, ServerTransformFailed
from zero_server.protocol.query_server import QuerySuccess
from zero_server.protocol.wire import parse_wire
from zero_server.query.builder import Query, as_query_internals
from zero_server.query.registry import QueryParseError
from zero_server.schema import Schema, client_to_server
from zero_server.shared.error import get_error_details, get_error_message
from zero_server.shared.js import js_check_in, js_truthy
from zero_server.shared.json import JSONValue, parse_json_body

logger = logging.getLogger("zero_server.query")

type QueryRequestHandler = Callable[[str, JSONValue | None], Query | Awaitable[Query]]
"""Turns a query's name and its single argument into a `Query`."""

type QueryResponse = QuerySuccess | ServerTransformFailed

_transform_request: TypeAdapter[Any] = TypeAdapter(TransformRequestMessage)


def server_ast(query: Query, schema: Schema) -> AST:
    """`query`'s AST in server names: what the query endpoint sends zero-cache.

    Python only; upstream inlines `mapAST(ast, clientToServer(schema.tables))` in its query handler.
    Raises on a table or column `schema` does not have.
    """
    return map_ast(query.ast, client_to_server(schema.tables))


async def handle_query_request(
    *,
    handler: QueryRequestHandler,
    schema: Schema,
    body: object,
    user_id: str | None,
) -> QueryResponse:
    """Process one `/query` request from zero-cache.

    `body` is the request's JSON, raw bytes or already parsed. `user_id` is the authenticated user,
    `None` when logged out; reporting it is what makes zero-cache trust the connection's user.
    """
    query_ids: list[str] = []
    try:
        json_body: Any = parse_json_body(body) if isinstance(body, bytes) else body
        parsed = parse_wire(_transform_request, json_body)
        query_ids = [r.id for r in parsed[1]]
    except (ValueError, ValidationError) as error:
        logger.error("Failed to parse query request", exc_info=error)
        return ServerTransformFailed(
            reason=ErrorReason.PARSE,
            message=f"Failed to parse query request: {get_error_message(error)}",
            queryIDs=query_ids,
            details={"name": "TypeError" if isinstance(error, ValidationError) else "SyntaxError"},
        )

    async def transform_one(request: TransformRequestQuery) -> TransformedQuery | AppErroredQuery | ParseErroredQuery:
        try:
            result = handler(request.name, request.args[0] if request.args else None)
            if inspect.isawaitable(result):
                result = await result
            # Upstream unwraps a deprecated `{query}` result here, which needs an object.
            js_check_in("query", result)
        except Exception as error:
            details = get_error_details(error)
            errored = ParseErroredQuery if isinstance(error, QueryParseError) else AppErroredQuery
            return errored(
                id=request.id,
                name=request.name,
                message=get_error_message(error),
                details=details if js_truthy(details) else None,
            )
        query = as_query_internals(result)
        try:
            ast = map_ast(query.ast, mapper)
        except Exception as error:
            logger.error("Failed to map AST", exc_info=error)
            raise
        return TransformedQuery(id=request.id, name=request.name, ast=ast)

    try:
        mapper = client_to_server(schema.tables)
        # All at once, as upstream's `Promise.all`: the first failure fails the batch, the rest run on.
        responses = await asyncio.gather(*(transform_one(request) for request in parsed[1]))
        return QuerySuccess(queries=list(responses), userID=user_id)
    except Exception as error:
        details = get_error_details(error)
        return ServerTransformFailed(
            reason=ErrorReason.INTERNAL,
            message=get_error_message(error),
            queryIDs=query_ids,
            details=details if js_truthy(details) else None,
        )
