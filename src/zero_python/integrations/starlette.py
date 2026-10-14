"""`/query` and `/mutate` as Starlette routes. Needs the `zero-python[starlette]` extra.

    app = Starlette(routes=zero_routes(
        schema=schema, database=db, queries=queries, mutators=mutators, authenticate=user_id_from_jwt,
    ))

FastAPI is Starlette underneath: `app.router.routes.extend(zero_routes(...))`.
"""

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from zero_python.mutators import must_get_mutator
from zero_python.protocol.mutation import CustomMutation, MutationResponse
from zero_python.query.registry import must_get_query
from zero_python.schema import Schema
from zero_python.server.process_mutations import Database, TransactFn, handle_mutate_request
from zero_python.server.process_queries import handle_query_request
from zero_python.shared.json import JSONValue
from zero_python.shared.registry import Registry

type Authenticate = Callable[[Request], str | Awaitable[str | None] | None]
"""The request's user ID, or `None` when logged out. Raise `Unauthorized` to answer 401."""
type MakeContext = Callable[[Request, str | None], Any]
"""The `ctx` queries and mutators receive. May be async."""


class Unauthorized(Exception):
    """Raised by `authenticate` for a token it rejects. The endpoint answers 401 instead of running."""


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _default_context(_request: Request, user_id: str | None) -> dict[str, Any]:
    return {"userID": user_id}


def zero_routes(
    *,
    schema: Schema,
    database: Database[Any] | None = None,
    queries: Registry | None = None,
    mutators: Registry | None = None,
    authenticate: Authenticate,
    context: MakeContext = _default_context,
    query_path: str = "/query",
    mutate_path: str = "/mutate",
) -> list[Route]:
    """Routes for whichever of the query and mutate endpoints you pass a registry for.

    Queries resolve by name through `queries` and mutators through `mutators`, each called with the
    `ctx` that `context` builds (by default `{"userID": user_id}`). Mutations run on `database`.
    """
    routes: list[Route] = []

    async def caller(request: Request) -> tuple[str | None, Any] | Response:
        try:
            user_id = await _maybe_await(authenticate(request))
        except Unauthorized as error:
            return JSONResponse({"error": str(error) or "Unauthorized"}, status_code=401)
        return user_id, await _maybe_await(context(request, user_id))

    if queries is not None:

        async def query(request: Request) -> Response:
            who = await caller(request)
            if isinstance(who, Response):
                return who
            user_id, ctx = who
            response = await handle_query_request(
                handler=lambda name, args: must_get_query(queries, name).fn(args=args, ctx=ctx),
                schema=schema,
                body=await request.body(),
                user_id=user_id,
            )
            return JSONResponse(response.to_json())

        routes.append(Route(query_path, query, methods=["POST"]))

    if mutators is not None:
        if database is None:
            raise ValueError("the mutate endpoint needs a database")
        db = database

        async def mutate(request: Request) -> Response:
            who = await caller(request)
            if isinstance(who, Response):
                return who
            user_id, ctx = who

            async def handler(transact: TransactFn[Any], _mutation: CustomMutation) -> MutationResponse:
                async def run(tx: Any, name: str, args: JSONValue | None) -> None:
                    await must_get_mutator(mutators, name).fn(tx=tx, ctx=ctx, args=args)

                return await transact(run)

            response = await handle_mutate_request(
                db_provider=db,
                handler=handler,
                query=dict(request.query_params),
                body=await request.body(),
                user_id=user_id,
            )
            return JSONResponse(response.to_json())

        routes.append(Route(mutate_path, mutate, methods=["POST"]))

    return routes
