"""The Python app server: the same queries and mutators as `app/*.ts`, served by zero-python.

It is written the way an app would use the library: types generated from the schema
(`zero_schema.py`, by `zero-python codegen`), the decorator forms, and the Starlette routes.
"""

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.applications import Starlette
from starlette.requests import Request
from zero_schema import Project, TaskInsert, create_builder, mutate

from zero_python import ApplicationError, define_mutator, define_mutators, define_queries, define_query, load_schema
from zero_python.integrations.starlette import zero_routes
from zero_python.server.adapters.sqlalchemy import SQLAlchemyDatabase
from zero_python.server.transaction import ServerTransaction

schema = load_schema(Path(os.environ["SCHEMA_JSON"]))
zql = create_builder(schema)
db = SQLAlchemyDatabase(create_async_engine(os.environ["UPSTREAM_DB"]), schema)

type Tx = ServerTransaction[Any]
type Ctx = dict[str, str]


class ProjectArgs(BaseModel):
    projectID: str


class SearchArgs(BaseModel):
    text: str
    labels: list[str]


@define_query
def my_projects(*, args: None, ctx: Ctx) -> Any:
    return (
        zql.project.where("ownerID", ctx["userID"])
        .related("owner")
        .related("tasks", lambda q: q.order_by("priority", "desc").order_by("id", "asc").related("labels"))
        .order_by("name", "asc")
    )


@define_query(ProjectArgs)
def open_tasks(*, args: ProjectArgs, ctx: Ctx) -> Any:
    return (
        zql.task.where("projectID", args.projectID)
        .where("done", False)
        .where_exists("project", lambda p: p.where("name", "NOT LIKE", "archived%"))
        .order_by("title", "asc")
        .limit(10)
    )


@define_query(SearchArgs)
def search_tasks(*, args: SearchArgs, ctx: Ctx) -> Any:
    return (
        zql.task.where(
            lambda eb: eb.or_(
                eb.cmp("title", "ILIKE", f"%{args.text}%"),
                eb.exists("labels", lambda label: label.where("name", "IN", args.labels)),
            )
        )
        .where("priority", ">=", 1)
        .order_by("priority", "asc")
        .order_by("id", "asc")
    )


@define_query
def task_by_id(*, args: dict[str, str], ctx: Ctx) -> Any:
    return zql.task.where("id", args["id"]).related("project").one()


queries = define_queries(
    {
        "projects": {"mine": my_projects},
        "tasks": {"open": open_tasks, "search": search_tasks, "byID": task_by_id},
    }
)


@define_mutator
async def register(*, tx: Tx, ctx: Ctx, args: dict[str, str]) -> None:
    await mutate(tx).user.upsert({"id": ctx["userID"], "name": args["name"]})


@define_mutator
async def create_project(*, tx: Tx, ctx: Ctx, args: dict[str, str]) -> None:
    if args["name"].strip() == "":
        raise ApplicationError("A project needs a name", details={"field": "name"})
    await mutate(tx).project.insert({"id": args["id"], "name": args["name"], "ownerID": ctx["userID"]})


@define_mutator
async def rename_project(*, tx: Tx, ctx: Ctx, args: dict[str, str]) -> None:
    await mutate(tx).project.update({"id": args["id"], "name": args["name"]})


@define_mutator
async def create_task(*, tx: Tx, ctx: Ctx, args: TaskInsert) -> None:
    # Authorization reads through tx.run, z2s against Postgres in the mutation's own transaction.
    project: Project | None = await tx.run(zql.project.where("id", args["projectID"]).one())
    if project is None or project["ownerID"] != ctx["userID"]:
        raise ApplicationError("Not your project", details={"projectID": args["projectID"]})
    await mutate(tx).task.insert({**args, "done": False})


@define_mutator
async def toggle_task(*, tx: Tx, ctx: Ctx, args: dict[str, Any]) -> None:
    await mutate(tx).task.update({"id": args["id"], "done": args["done"]})


@define_mutator
async def retitle_task(*, tx: Tx, ctx: Ctx, args: dict[str, str]) -> None:
    if "forbidden" in args["title"]:
        raise ApplicationError("That title is not allowed", details={"title": args["title"]})
    await mutate(tx).task.update({"id": args["id"], "title": args["title"]})


@define_mutator
async def remove_task(*, tx: Tx, ctx: Ctx, args: dict[str, str]) -> None:
    await mutate(tx).task.delete({"id": args["id"]})


@define_mutator
async def upsert_label(*, tx: Tx, ctx: Ctx, args: dict[str, str]) -> None:
    await mutate(tx).label.upsert({"id": args["id"], "name": args["name"]})


@define_mutator
async def attach_label(*, tx: Tx, ctx: Ctx, args: dict[str, str]) -> None:
    await mutate(tx).taskLabel.upsert({"taskID": args["taskID"], "labelID": args["labelID"]})


mutators = define_mutators(
    {
        "user": {"register": register},
        "project": {"create": create_project, "rename": rename_project},
        "task": {"create": create_task, "toggle": toggle_task, "retitle": retitle_task, "remove": remove_task},
        "label": {"upsert": upsert_label, "attach": attach_label},
    }
)


def user_id_of(request: Request) -> str | None:
    """The test app trusts the bearer token as the user ID. A real app would verify it."""
    authorization = request.headers.get("authorization", "")
    return authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else None


app = Starlette(
    routes=zero_routes(
        schema=schema,
        database=db,
        queries=queries,
        mutators=mutators,
        authenticate=user_id_of,
        context=lambda _request, user_id: {"userID": user_id or "anon"},
    )
)
