"""The Python twin of `harness/mutate.ts`: the same mutators, handler and failing provider."""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from tests.wire.errors import SHAPES
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
from zero_server.server.transaction import RetryPredicate, ServerTransaction
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


async def details_error(tx: Tx, args: dict[str, Any]) -> None:
    await put(tx, args)
    error = TypeError("with details")
    if "details" in args:
        error.details = args["details"]  # ty: ignore[unresolved-attribute]
    raise error


async def db_error(tx: Tx, _args: dict[str, Any]) -> None:
    await tx.db_transaction.query("SELECT * FROM missing_table")


def crud(kind: str) -> Callable[[Tx, dict[str, Any]], Awaitable[None]]:
    async def run(tx: Tx, args: dict[str, Any]) -> None:
        # `tx.mutate[table]` in the TypeScript twin: the key is `String(table)`, and an unknown table is
        # undefined, which throws when its method is read.
        table = "undefined" if args.get("table") is None else _js_string(args["table"], "")
        if _zql is None or table not in _zql._schema.tables:
            raise TypeError(f"Cannot read properties of undefined (reading '{kind}')")
        # `tx.mutate.thing` as the twin writes `tx.mutate[table]`: attribute access is Python's property access.
        await getattr(getattr(tx.mutate, table), kind)(args.get("row"))

    return run


_zql: Any = None


def use_schema(schema: Schema) -> None:
    global _zql
    _zql = create_builder(schema)


async def count(tx: Tx, args: dict[str, Any]) -> None:
    rows = await tx.run(_zql.item.where("value", "!=", _js_string(args.get("value"), "")).order_by("value", "desc"))
    await put(tx, {"id": "count", "value": ",".join(r["id"] for r in rows)})


def throwing(make: Callable[[], BaseException]) -> Callable[[Tx, dict[str, Any]], Awaitable[None]]:
    async def run(tx: Tx, args: dict[str, Any]) -> None:
        await put(tx, args)
        raise make()

    return run


# How many times item.retry has run for each mutation, by `clientID:mutationID`. Reset per test example.
RUNS: dict[str, int] = {}
_kept: list[Tx] = []


def reset_runs() -> None:
    RUNS.clear()
    _kept.clear()


async def _later(_error: BaseException, _attempt: int) -> bool:
    await asyncio.sleep(0.001)
    return True


def _throw(_error: BaseException, _attempt: int) -> bool:
    raise Exception("predicate broke")


async def _reject(_error: BaseException, _attempt: int) -> bool:
    raise Exception("predicate rejected")


PREDICATES: dict[str, RetryPredicate] = {  # ty: ignore[invalid-assignment] - "truthy" answers 1, not a bool
    "yes": lambda _error, _attempt: True,
    "no": lambda _error, _attempt: False,
    "later": _later,
    "throw": _throw,
    "reject": _reject,
    "truthy": lambda _error, _attempt: 1,
    "firstOnly": lambda _error, attempt: attempt == 1,
    "matches": lambda error, _attempt: "failed" in str(error),
}


async def return_(tx: Tx, args: dict[str, Any]) -> Any:
    await put(tx, args)
    return args.get("ret")


async def retry(tx: Tx, args: dict[str, Any]) -> int:
    from zero_server.shared.js import js_number

    for name in args.get("predicates") or []:
        tx.retry_on(PREDICATES[name])
    await put(tx, args)
    key = f"{tx.client_id}:{js_number(tx.mutation_id)}"
    run = RUNS[key] = RUNS.get(key, 0) + 1
    if run <= args.get("fail", 0):
        if args.get("db"):
            await tx.db_transaction.query("SELECT * FROM missing_table")
        raise Exception(f"run {run} failed")
    return run


async def keep(tx: Tx, args: dict[str, Any]) -> bool:
    _kept[:] = [tx]
    await put(tx, args)
    # Still open, so this is the driver's transaction.
    return tx.db_transaction.wrapped_transaction is None


async def use_kept(_tx: Tx, args: dict[str, Any]) -> bool:
    if not _kept:
        raise Exception("nothing kept")
    if args.get("db"):
        await _kept[0].db_transaction.query("SELECT 1")
    if args.get("run"):
        await _kept[0].run(_zql.item)
    return _kept[0].db_transaction.wrapped_transaction is None


MUTATORS: dict[str, Callable[[Tx, dict[str, Any]], Awaitable[Any]]] = {
    **{f"throw.{shape}": throwing(make) for shape, make in SHAPES.items()},
    "item.count": count,
    **{f"crud.{kind}": crud(kind) for kind in ("insert", "upsert", "update", "delete")},
    "item.put": put,
    "item.appError": app_error,
    "item.error": error,
    "item.typeError": type_error,
    "item.detailsError": details_error,
    "item.dbError": db_error,
    "item.return": return_,
    "item.retry": retry,
    "item.keep": keep,
    "item.useKept": use_kept,
}


def _args(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


async def handler(transact: TransactFn[Tx], mutation: CustomMutation) -> MutationResponse:
    args = _args(mutation.args[0] if mutation.args else None)
    if mutation.name == "pre.appError":
        raise ApplicationError(_js_string(args.get("message"), "pre"), details=args.get("details"))
    if mutation.name == "pre.error":
        raise Exception(_js_string(args.get("message"), "pre"))

    async def run(tx: Tx, name: str, mutator_args: Any) -> Any:
        mutator = MUTATORS.get(name.replace("post.", "item.", 1) if name.startswith("post.") else name)
        if mutator is None:
            raise Exception(f"unknown mutator {name}")
        return await mutator(tx, _args(mutator_args))

    result = await transact(run)
    if mutation.name.startswith("post."):
        raise Exception("after commit")
    return result


class FailingForCfail:
    """Fails to open any transaction for client `cfail`, as the TypeScript twin does."""

    def __init__(self, db: Database[Tx]) -> None:
        self.db = db

    async def transaction(
        self,
        callback: Callable[[Tx, TransactionProviderHooks], Awaitable[Any]],
        transaction_input: TransactionProviderInput | None = None,
    ) -> Any:
        if transaction_input is not None and transaction_input.client_id == "cfail":
            raise Exception("cannot open")
        return await self.db.transaction(callback, transaction_input)


class FailingForCfailInSessions(FailingForCfail):
    @asynccontextmanager
    async def session(self) -> AsyncGenerator["FailingForCfail"]:
        # The Python database runs a push in a session; this forwards it, failing as before.
        assert isinstance(self.db, ZQLDatabase)
        async with self.db.session() as db:
            yield FailingForCfail(db)


def provider(db: ZQLDatabase[Any], *, sessions: bool = True) -> Database[Tx]:
    """`db`, failing for `cfail`. Without `sessions`, each transaction takes its own connection."""
    return FailingForCfailInSessions(db) if sessions else FailingForCfail(db)


async def _registry_put(*, tx: Tx, args: Any, ctx: Any) -> None:
    args = _args(args)
    await put(tx, {**args, "value": f"{_js_string(args.get('value'), '')}@{ctx['tag']}"})


async def _registry_app_error(*, tx: Tx, args: Any, ctx: Any) -> None:
    await app_error(tx, _args(args))


async def _registry_ret(*, tx: Tx, args: Any, ctx: Any) -> Any:
    return await return_(tx, _args(args))


async def _legacy_put(tx: Tx, args: Any) -> None:
    await put(tx, _args(args))


async def _legacy_ret(tx: Tx, args: Any) -> Any:
    return await return_(tx, _args(args))


REGISTRY = {
    **define_mutators(
        {
            "item": {
                "put": define_mutator(_registry_put),
                "appError": define_mutator(_registry_app_error),
                "ret": define_mutator(_registry_ret),
            }
        }
    ),
    "legacy": {"put": _legacy_put, "ret": _legacy_ret},
}


async def _http_put(*, tx: Tx, args: Any, ctx: Any) -> None:
    from zero_server.shared.js import js_string

    args = _args(args)
    await put(tx, {**args, "value": f"{_js_string(args.get('value'), '')}@{js_string(ctx['userID'])}"})


async def _http_error(*, tx: Tx, args: Any, ctx: Any) -> None:
    await error(tx, _args(args))


HTTP_REGISTRY = define_mutators(
    {
        "item": {
            "put": define_mutator(_http_put),
            "appError": define_mutator(_registry_app_error),
            "error": define_mutator(_http_error),
        }
    }
)
"""The twin of `httpMutators` in `harness/http.ts`, behind the Starlette routes."""
