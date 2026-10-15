"""The Python twin of `harness/mutate.ts`: the same mutators, handler and failing provider."""

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from zero_server.mutators import define_mutator, define_mutators
from zero_server.protocol.application_error import ApplicationError
from zero_server.protocol.mutation import CustomMutation, MutationResponse
from zero_server.query.builder import create_builder
from zero_server.schema import Schema
from zero_server.server.process_mutations import (
    Database,
    TransactFn,
    TransactionProviderHooks,
    TransactionProviderInput,
)
from zero_server.server.transaction import ServerTransaction
from zero_server.server.zql_database import ZQLDatabase

type Tx = ServerTransaction[AsyncConnection]

PUT = """INSERT INTO item (id, value) VALUES ($1, $2)
  ON CONFLICT (id) DO UPDATE SET value = item.value || '+' || excluded.value"""


def _js_string(value: Any, default: str) -> str:
    if value is None:
        return default
    if isinstance(value, bool):
        return "true" if value else "false"
    from zero_server.shared.js import js_string

    return js_string(value) if not isinstance(value, dict) else "[object Object]"


async def put(tx: Tx, args: dict[str, Any]) -> None:
    await tx.db_transaction.query(PUT, (_js_string(args.get("id"), "x"), _js_string(args.get("value"), "")))


async def app_error(tx: Tx, args: dict[str, Any]) -> None:
    await put(tx, args)
    raise ApplicationError(_js_string(args.get("message"), "refused"), details=args.get("details"))


async def error(tx: Tx, args: dict[str, Any]) -> None:
    await put(tx, args)
    raise Exception(_js_string(args.get("message"), "broke"))


async def type_error(tx: Tx, args: dict[str, Any]) -> None:
    await put(tx, args)
    raise TypeError(_js_string(args.get("message"), "bad type"))


async def db_error(tx: Tx, _args: dict[str, Any]) -> None:
    await tx.db_transaction.query("SELECT * FROM missing_table")


def crud(kind: str) -> Callable[[Tx, dict[str, Any]], Awaitable[None]]:
    async def run(tx: Tx, args: dict[str, Any]) -> None:
        # `tx.mutate[table]` in the TypeScript twin: the key is `String(table)`, and an unknown table is
        # undefined, which throws when its method is read.
        table = "undefined" if args.get("table") is None else _js_string(args["table"], "")
        if _zql is None or table not in _zql._schema.tables:
            raise TypeError(f"Cannot read properties of undefined (reading '{kind}')")
        await getattr(tx.mutate[table], kind)(args.get("row"))

    return run


_zql: Any = None


def use_schema(schema: Schema) -> None:
    global _zql
    _zql = create_builder(schema)


async def count(tx: Tx, args: dict[str, Any]) -> None:
    rows = await tx.run(_zql.item.where("value", "!=", _js_string(args.get("value"), "")).order_by("value", "desc"))
    await put(tx, {"id": "count", "value": ",".join(r["id"] for r in rows)})


MUTATORS: dict[str, Callable[[Tx, dict[str, Any]], Awaitable[None]]] = {
    "item.count": count,
    **{f"crud.{kind}": crud(kind) for kind in ("insert", "upsert", "update", "delete")},
    "item.put": put,
    "item.appError": app_error,
    "item.error": error,
    "item.typeError": type_error,
    "item.dbError": db_error,
}


def _args(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


async def handler(transact: TransactFn[Tx], mutation: CustomMutation) -> MutationResponse:
    args = _args(mutation.args[0] if mutation.args else None)
    if mutation.name == "pre.appError":
        raise ApplicationError(_js_string(args.get("message"), "pre"), details=args.get("details"))
    if mutation.name == "pre.error":
        raise Exception(_js_string(args.get("message"), "pre"))

    async def run(tx: Tx, name: str, mutator_args: Any) -> None:
        mutator = MUTATORS.get(name.replace("post.", "item.", 1) if name.startswith("post.") else name)
        if mutator is None:
            raise Exception(f"unknown mutator {name}")
        await mutator(tx, _args(mutator_args))

    result = await transact(run)
    if mutation.name.startswith("post."):
        raise Exception("after commit")
    return result


class FailingForCfail:
    """Fails to open any transaction for client `cfail`, as the TypeScript twin does."""

    def __init__(self, db: ZQLDatabase[Any]) -> None:
        self.db = db

    async def transaction(
        self,
        callback: Callable[[Tx, TransactionProviderHooks], Awaitable[Any]],
        transaction_input: TransactionProviderInput | None = None,
    ) -> Any:
        if transaction_input is not None and transaction_input.client_id == "cfail":
            raise Exception("cannot open")
        return await self.db.transaction(callback, transaction_input)


def provider(db: ZQLDatabase[Any]) -> Database[Tx]:
    return FailingForCfail(db)


async def _registry_put(*, tx: Tx, args: Any, ctx: Any) -> None:
    args = _args(args)
    await put(tx, {**args, "value": f"{_js_string(args.get('value'), '')}@{ctx['tag']}"})


async def _registry_app_error(*, tx: Tx, args: Any, ctx: Any) -> None:
    await app_error(tx, _args(args))


async def _legacy_put(tx: Tx, args: Any) -> None:
    await put(tx, _args(args))


REGISTRY = {
    **define_mutators(
        {"item": {"put": define_mutator(_registry_put), "appError": define_mutator(_registry_app_error)}}
    ),
    "legacy": {"put": _legacy_put},
}
