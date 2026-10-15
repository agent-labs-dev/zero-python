"""The Python app server: the same queries and mutators as `app/*.ts`, served by zero-python."""

import os
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import create_async_engine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from zero_python import (
    ApplicationError,
    create_builder,
    define_mutator,
    define_mutators,
    define_queries,
    define_query,
    handle_mutate_request,
    handle_query_request,
    load_schema,
    must_get_mutator,
    must_get_query,
)
from zero_python.server.sqlalchemy import SQLAlchemyDatabase

schema = load_schema(Path(os.environ["SCHEMA_JSON"]))
zql = create_builder(schema)
db = SQLAlchemyDatabase(create_async_engine(os.environ["UPSTREAM_DB"]), schema)

queries = define_queries(
    {
        "projects": {
            "mine": define_query(
                lambda *, args, ctx: (
                    zql.project.where("ownerID", ctx["userID"])
                    .related("owner")
                    .related("tasks", lambda q: q.order_by("priority", "desc").order_by("id", "asc").related("labels"))
                    .order_by("name", "asc")
                )
            ),
        },
        "tasks": {
            "open": define_query(
                lambda *, args, ctx: (
                    zql.task.where("projectID", args["projectID"])
                    .where("done", False)
                    .where_exists("project", lambda p: p.where("name", "NOT LIKE", "archived%"))
                    .order_by("title", "asc")
                    .limit(10)
                )
            ),
            "search": define_query(
                lambda *, args, ctx: (
                    zql.task.where(
                        lambda eb: eb.or_(
                            eb.cmp("title", "ILIKE", f"%{args['text']}%"),
                            eb.exists("labels", lambda label: label.where("name", "IN", args["labels"])),
                        )
                    )
                    .where("priority", ">=", 1)
                    .order_by("priority", "asc")
                    .order_by("id", "asc")
                )
            ),
            "byID": define_query(lambda *, args, ctx: zql.task.where("id", args["id"]).related("project").one()),
        },
    }
)


async def register(*, tx: Any, ctx: Any, args: Any) -> None:
    await tx.mutate.user.upsert({"id": ctx["userID"], "name": args["name"]})


async def create_project(*, tx: Any, ctx: Any, args: Any) -> None:
    if args["name"].strip() == "":
        raise ApplicationError("A project needs a name", details={"field": "name"})
    await tx.mutate.project.insert({"id": args["id"], "name": args["name"], "ownerID": ctx["userID"]})


async def rename_project(*, tx: Any, ctx: Any, args: Any) -> None:
    await tx.mutate.project.update({"id": args["id"], "name": args["name"]})


async def create_task(*, tx: Any, ctx: Any, args: Any) -> None:
    await tx.mutate.task.insert({**args, "done": False})


async def toggle_task(*, tx: Any, ctx: Any, args: Any) -> None:
    await tx.mutate.task.update({"id": args["id"], "done": args["done"]})


async def retitle_task(*, tx: Any, ctx: Any, args: Any) -> None:
    if "forbidden" in args["title"]:
        raise ApplicationError("That title is not allowed", details={"title": args["title"]})
    await tx.mutate.task.update({"id": args["id"], "title": args["title"]})


async def remove_task(*, tx: Any, ctx: Any, args: Any) -> None:
    await tx.mutate.task.delete({"id": args["id"]})


async def upsert_label(*, tx: Any, ctx: Any, args: Any) -> None:
    await tx.mutate.label.upsert(args)


async def attach_label(*, tx: Any, ctx: Any, args: Any) -> None:
    await tx.mutate.taskLabel.upsert(args)


mutators = define_mutators(
    {
        "user": {"register": define_mutator(register)},
        "project": {"create": define_mutator(create_project), "rename": define_mutator(rename_project)},
        "task": {
            "create": define_mutator(create_task),
            "toggle": define_mutator(toggle_task),
            "retitle": define_mutator(retitle_task),
            "remove": define_mutator(remove_task),
        },
        "label": {"upsert": define_mutator(upsert_label), "attach": define_mutator(attach_label)},
    }
)


def user_id_of(request: Request) -> str | None:
    """The test app trusts the bearer token as the user ID. A real app would verify it."""
    authorization = request.headers.get("authorization", "")
    return authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else None


async def query(request: Request) -> JSONResponse:
    user_id = user_id_of(request)
    ctx = {"userID": user_id or "anon"}
    response = await handle_query_request(
        handler=lambda name, args: must_get_query(queries, name).fn(args=args, ctx=ctx),
        schema=schema,
        body=await request.body(),
        user_id=user_id,
    )
    return JSONResponse(response.to_json())


async def mutate(request: Request) -> JSONResponse:
    user_id = user_id_of(request)
    ctx = {"userID": user_id or "anon"}

    async def handler(transact: Any, mutation: Any) -> Any:
        return await transact(lambda tx, name, args: must_get_mutator(mutators, name).fn(tx=tx, ctx=ctx, args=args))

    response = await handle_mutate_request(
        db_provider=db,
        handler=handler,
        query=dict(request.query_params),
        body=await request.body(),
        user_id=user_id,
    )
    return JSONResponse(response.to_json())


app = Starlette(routes=[Route("/query", query, methods=["POST"]), Route("/mutate", mutate, methods=["POST"])])
