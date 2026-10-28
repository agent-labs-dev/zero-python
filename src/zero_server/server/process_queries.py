"""The query endpoint: turn each named query zero-cache forwards into its AST, in server names.

Ported from rocicorp/mono `packages/zero-server/src/queries/process-queries.ts` at zero/v1.11.3
(e91e55f05b7a2c631f77238e1dd6fadf1029a538). Apache-2.0, Copyright Rocicorp.
"""

import asyncio
import functools
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import TypeAdapter, ValidationError

from zero_server.protocol.ast import AST
from zero_server.protocol.custom_queries import (
    AppErroredQuery,
    ParseErroredQuery,
    TransformedQuery,
    TransformRequestMessage,
    TransformRequestQuery,
)
from zero_server.protocol.error import ErrorReason, ServerTransformFailed
from zero_server.protocol.query_server import QuerySuccess
from zero_server.protocol.schemas import refusal, transform_request_message_schema
from zero_server.protocol.wire import parse_wire
from zero_server.query.builder import Query, as_query_internals, mapped_ast
from zero_server.query.registry import QueryParseError
from zero_server.schema import Schema
from zero_server.shared.error import error_name, get_error_details, get_error_message
from zero_server.shared.js import js_check_in, js_stringify_bytes, js_truthy
from zero_server.shared.json import JSONValue
from zero_server.shared.json_parse import parse_body

logger = logging.getLogger("zero_server.query")

type QueryRequestHandler = Callable[[str, JSONValue | None], Any]
"""Turns a query's name and its single argument into a `Query`, or `{"query": query}`, maybe awaitable."""

type QueryResponse = QuerySuccess | ServerTransformFailed
type Transformed = TransformedQuery | AppErroredQuery | ParseErroredQuery


@functools.cache
def _transform_request() -> TypeAdapter[Any]:
    return TypeAdapter(TransformRequestMessage)


def server_ast(query: Query, schema: Schema) -> AST:
    """`query`'s AST in server names: what the query endpoint sends zero-cache.

    Python only; upstream inlines `mapAST(ast, clientToServer(schema.tables))` in its query handler.
    Raises on a table or column `schema` does not have.
    """
    return mapped_ast(query, schema.client_to_server_mapper)


def query_response_bytes(response: QueryResponse) -> bytes:
    """`response` as the JSON text a TypeScript server sends: `JSON.stringify(response)`."""
    if isinstance(response, QuerySuccess):
        return response.to_json_bytes()
    return js_stringify_bytes(response.to_json())


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
        parsed = parse_wire(_transform_request(), body)
        query_ids = [r.id for r in parsed[1]]
    except (ValueError, ValidationError) as error:
        logger.error("Failed to parse query request", exc_info=error)
        message = get_error_message(error)
        if isinstance(error, ValidationError):
            decoded = parse_body(body) if isinstance(body, bytes) else body
            message = refusal(decoded, transform_request_message_schema, "strip", message)
        return ServerTransformFailed(
            reason=ErrorReason.PARSE,
            message=f"Failed to parse query request: {message}",
            queryIDs=query_ids,
            details={"name": "TypeError" if isinstance(error, ValidationError) else error_name(error)},
        )

    def errored(request: TransformRequestQuery, error: Exception) -> AppErroredQuery | ParseErroredQuery:
        details = get_error_details(error)
        kind = ParseErroredQuery if isinstance(error, QueryParseError) else AppErroredQuery
        return kind(
            id=request.id,
            name=request.name,
            message=get_error_message(error),
            details=details if js_truthy(details) else None,
        )

    def transformed(request: TransformRequestQuery, result: object) -> Transformed:
        try:
            # Upstream unwraps a deprecated `{query}` result here, which needs an object.
            js_check_in("query", result)
            if isinstance(result, Mapping) and "query" in result:
                result = result["query"]
        except Exception as error:
            return errored(request, error)
        query = as_query_internals(result)
        try:
            ast = mapped_ast(query, mapper)
        except Exception as error:
            logger.error("Failed to map AST", exc_info=error)
            raise
        return TransformedQuery(id=request.id, name=request.name, ast=ast)

    async def settle(request: TransformRequestQuery, result: Awaitable[object]) -> Transformed:
        try:
            resolved = await result
        except Exception as error:
            return errored(request, error)
        return transformed(request, resolved)

    try:
        mapper = schema.client_to_server_mapper
        # All at once, as upstream's `Promise.all`: the first failure fails the batch, the rest run on.
        # Each handler is called before the first is awaited, as upstream's are. One that returns a
        # query, or an awaitable that never suspends, is done without a task or a trip through the loop.
        responses: list[Transformed | None] = []
        pending: list[tuple[int, asyncio.Task[Transformed]]] = []
        failure: Exception | None = None
        for request in parsed[1]:
            try:
                result = handler(request.name, request.args[0] if request.args else None)
            except Exception as error:
                responses.append(errored(request, error))
                continue
            try:
                if not inspect.isawaitable(result):
                    responses.append(transformed(request, result))
                    continue
                task = asyncio.eager_task_factory(asyncio.get_running_loop(), settle(request, result))
                if task.done():
                    responses.append(task.result())
                else:
                    pending.append((len(responses), task))
                    responses.append(None)
            except Exception as error:
                failure = failure or error
                responses.append(None)
        if failure is not None:
            raise failure
        if pending:
            for (i, _), response in zip(pending, await asyncio.gather(*(task for _, task in pending)), strict=True):
                responses[i] = response
        return QuerySuccess(queries=responses, user_id=user_id)  # ty: ignore[invalid-argument-type]
    except Exception as error:
        details = get_error_details(error)
        return ServerTransformFailed(
            reason=ErrorReason.INTERNAL,
            message=get_error_message(error),
            queryIDs=query_ids,
            details=details if js_truthy(details) else None,
        )
