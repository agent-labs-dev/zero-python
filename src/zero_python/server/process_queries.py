"""The query endpoint: turn each named query zero-cache forwards into its AST, in server names.

Ported from rocicorp/mono `packages/zero-server/src/queries/process-queries.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.
"""

import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import TypeAdapter, ValidationError

from zero_python.protocol.ast import map_ast
from zero_python.protocol.custom_queries import (
    AppErroredQuery,
    ParseErroredQuery,
    TransformedQuery,
    TransformRequestMessage,
)
from zero_python.protocol.error import ErrorReason, ServerTransformFailed
from zero_python.protocol.query_server import QuerySuccess
from zero_python.protocol.wire import parse_wire
from zero_python.query.builder import Query
from zero_python.query.registry import QueryParseError
from zero_python.schema import Schema, client_to_server
from zero_python.shared.asserts import check
from zero_python.shared.error import get_error_details, get_error_message
from zero_python.shared.js import js_truthy
from zero_python.shared.json import JSONValue

logger = logging.getLogger("zero_python.query")

type QueryRequestHandler = Callable[[str, JSONValue | None], Query | Awaitable[Query]]
"""Turns a query's name and its single argument into a `Query`."""

type QueryResponse = QuerySuccess | ServerTransformFailed

_transform_request: TypeAdapter[Any] = TypeAdapter(TransformRequestMessage)


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
        json_body: Any = json.loads(body) if isinstance(body, bytes) else body
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

    try:
        mapper = client_to_server(schema.tables)
        responses: list[Any] = []
        for request in parsed[1]:
            try:
                result = request_query = handler(request.name, request.args[0] if request.args else None)
                if inspect.isawaitable(request_query):
                    result = await request_query
            except Exception as error:
                details = get_error_details(error)
                errored = ParseErroredQuery if isinstance(error, QueryParseError) else AppErroredQuery
                responses.append(
                    errored(
                        id=request.id,
                        name=request.name,
                        message=get_error_message(error),
                        details=details if js_truthy(details) else None,
                    )
                )
                continue
            check(
                isinstance(result, Query),
                "Query does not implement QueryInternals. This likely means there are two copies of Zero in your "
                "runtime.",
            )
            assert isinstance(result, Query)
            try:
                ast = map_ast(result.ast, mapper)
            except Exception as error:
                logger.error("Failed to map AST", exc_info=error)
                raise
            responses.append(TransformedQuery(id=request.id, name=request.name, ast=ast))
        return QuerySuccess(queries=responses, userID=user_id)
    except Exception as error:
        details = get_error_details(error)
        return ServerTransformFailed(
            reason=ErrorReason.INTERNAL,
            message=get_error_message(error),
            queryIDs=query_ids,
            details=details if js_truthy(details) else None,
        )
