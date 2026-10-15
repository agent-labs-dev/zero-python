"""The Python side of the benchmark tier: `harness/bench/cases.ts` in Python, case for case.

Run by `python -m bench`, which passes a config file and a group of cases, and reads one JSON line of
results from stdout.
"""

import asyncio
import inspect
import json
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

import asyncpg
from psycopg_pool import AsyncConnectionPool
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import create_async_engine

from tests.wire import mutators
from tests.wire.programs import apply, make_handler
from zero_server.protocol.custom_queries import TransformRequestMessage
from zero_server.protocol.push import PushBody
from zero_server.protocol.wire import parse_wire
from zero_server.query.builder import Query, create_builder
from zero_server.schema import Schema, load_schema
from zero_server.server.adapters.asyncpg import AsyncpgDatabase
from zero_server.server.adapters.psycopg import PsycopgDatabase
from zero_server.server.adapters.sqlalchemy import SQLAlchemyDatabase
from zero_server.server.process_mutations import handle_mutate_request
from zero_server.server.process_queries import handle_query_request
from zero_server.server.push_processor import PushProcessor
from zero_server.server.server_schema import get_server_schema
from zero_server.server.transaction import DBTransaction, Row
from zero_server.server.zql_database import ZQLDatabase
from zero_server.z2s.compiler import compile_query

type Program = dict[str, Any]

CONFIG: dict[str, Any] = json.loads(Path(sys.argv[1]).read_text()) if __name__ == "__main__" else {}
GROUP = sys.argv[2] if __name__ == "__main__" else ""
"""Only cases whose name starts with this part run."""
results: list[dict[str, Any]] = []


async def bench(name: str, impl: str, fn: Callable[[], Any]) -> None:
    """The median time of one call of `fn`, by the same procedure as `harness/bench/cases.ts`."""
    if name.split(".")[0] != GROUP or not re.search(CONFIG["filter"], name):
        return
    budget = CONFIG["budgetMs"] / 1000
    first = fn()
    if inspect.isawaitable(first):
        await first

        async def loop(n: int) -> None:
            for _ in range(n):
                await fn()
    else:

        async def loop(n: int) -> None:
            for _ in range(n):
                fn()

    # Enough calls per sample that one sample takes at least 0.2 ms.
    t0 = time.perf_counter()
    await loop(1)
    inner = max(1, min(1000, -int(-0.2 // max((time.perf_counter() - t0) * 1000, 1e-4))))
    warm_end = time.perf_counter() + budget * 0.3
    warmups = 0
    while time.perf_counter() < warm_end or warmups < 5:
        await loop(1)
        warmups += 1
    samples: list[float] = []
    end = time.perf_counter() + budget
    while (time.perf_counter() < end or len(samples) < 7) and len(samples) < 5000:
        t0 = time.perf_counter()
        await loop(inner)
        samples.append((time.perf_counter() - t0) * 1e6 / inner)
    samples.sort()
    results.append({"case": name, "impl": impl, "median_us": samples[len(samples) // 2], "samples": len(samples)})


def with_limit(program: Program, n: int) -> Program:
    return {**program, "ops": [[n if x == "N" else x for x in op] for op in program["ops"]]}


def transform_body(program: Program, n: int) -> list[Any]:
    return ["transform", [{"id": f"q{i}", "name": "prog", "args": [program]} for i in range(n)]]


def push_body(workloads: dict[str, Any], client_id: str, kind: str, n: int) -> dict[str, Any]:
    def args(i: int) -> dict[str, Any]:
        if kind == "put":
            return {"id": f"{client_id}-{i}", "value": "v"}
        return {"table": "thing", "row": {"id": f"{client_id}-{i}", **workloads["thingRow"]}}

    return {
        "clientGroupID": "cg",
        "pushVersion": 1,
        "timestamp": 1700000000000,
        "requestID": client_id,
        "mutations": [
            {
                "type": "custom",
                "id": i + 1,
                "clientID": client_id,
                "name": "item.put" if kind == "put" else "crud.insert",
                "timestamp": 1700000000000 + i,
                "args": [args(i)],
            }
            for i in range(n)
        ],
    }


_client_seq = 0


def fresh_client() -> str:
    global _client_seq
    _client_seq += 1
    return f"py{os.getpid()}-{_client_seq}"


MUTATE_QUERY = {"schema": "zero_0", "appID": "zero"}


def check_push(response: dict[str, Any], n: int) -> None:
    pushed = response.get("mutations", [])
    if len(pushed) != n or any("error" in m["result"] for m in pushed):
        raise RuntimeError(f"push failed: {json.dumps(response)[:500]}")


def json_response(value: Any) -> bytes:
    """What Starlette's `JSONResponse` does with a handler's result."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


class FakeTransaction:
    """Answers the mutate endpoint's own statements from memory, so only the SDK's CPU time is measured."""

    def __init__(self, server_schema_rows: list[Row]) -> None:
        self.server_schema_rows = server_schema_rows
        self.last_mutation_ids: dict[str, int] = {}

    @property
    def wrapped_transaction(self) -> None:
        return None

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        if "pg_catalog" in sql:
            return self.server_schema_rows
        if "lastMutationID" in sql:
            client = str(params[1])
            self.last_mutation_ids[client] = self.last_mutation_ids.get(client, 0) + 1
            return [{"lastMutationID": self.last_mutation_ids[client]}]
        return []


class FakeConnection:
    def __init__(self, tx: FakeTransaction) -> None:
        self.tx = tx

    async def transaction[R](self, fn: Callable[[DBTransaction[None]], Awaitable[R]]) -> R:
        return await fn(self.tx)


async def open_adapters(url: str, schema: Schema) -> tuple[dict[str, ZQLDatabase[Any]], Callable[[], Awaitable[None]]]:
    pool = await asyncpg.create_pool(url, min_size=1, max_size=10)
    psycopg_pool = AsyncConnectionPool(url, min_size=1, max_size=10, open=False)
    await psycopg_pool.open()
    engine = create_async_engine(url.replace("postgresql://", "postgresql+asyncpg://", 1))

    async def close() -> None:
        await pool.close()
        await psycopg_pool.close()
        await engine.dispose()

    return {
        "asyncpg": AsyncpgDatabase(pool, schema),
        "psycopg": PsycopgDatabase(psycopg_pool, schema),
        "sqlalchemy": SQLAlchemyDatabase(engine, schema),
    }, close


async def main(workloads: dict[str, Any], schema: Schema, wire_schema: Schema, server_schema_rows: list[Row]) -> None:
    zql = create_builder(schema)
    handler = make_handler(schema)

    def build(program: Program) -> Query:
        return apply(zql.table(program["table"]), program["ops"])

    # ---- query endpoint ----
    for kind in ("simple", "complex"):
        for n in workloads["batchSizes"]:
            body = transform_body(workloads["programs"][kind], n)
            text = json.dumps(body).encode()
            await bench(
                f"query.{kind}.b{n}",
                "python",
                lambda body=body: handle_query_request(handler=handler, schema=schema, body=body, user_id="u1"),
            )

            async def with_json(text: bytes = text) -> bytes:
                response = await handle_query_request(handler=handler, schema=schema, body=text, user_id="u1")
                return json_response(response.to_json())

            await bench(f"query_json.{kind}.b{n}", "python", with_json)

    # ---- request parsing ----
    transform_adapter: TypeAdapter[Any] = TypeAdapter(TransformRequestMessage)
    for m in workloads["parseSizes"]:
        push = push_body(workloads, "c1", "crud", m)
        push_text = json.dumps(push).encode()
        await bench(f"parse.push.m{m}", "python", lambda push=push: PushBody.from_json(push))
        await bench(f"parse_json.push.m{m}", "python", lambda t=push_text: PushBody.from_json(json.loads(t)))
        transform = transform_body(workloads["programs"]["complex"], m)
        transform_text = json.dumps(transform).encode()
        await bench(f"parse.transform.q{m}", "python", lambda t=transform: parse_wire(transform_adapter, t))
        await bench(
            f"parse_json.transform.q{m}",
            "python",
            lambda t=transform_text: parse_wire(transform_adapter, json.loads(t)),
        )

    # ---- mutate endpoint, CPU only: a fake database that answers from memory ----
    mutators.use_schema(wire_schema)
    fake_db: ZQLDatabase[Any] = ZQLDatabase(FakeConnection(FakeTransaction(server_schema_rows)), wire_schema)
    for kind in ("put", "crud"):
        for n in workloads["pushSizes"]:

            async def run_fake(kind: str = kind, n: int = n) -> bytes:
                response = await handle_mutate_request(
                    db_provider=fake_db,
                    handler=mutators.handler,
                    query=MUTATE_QUERY,
                    body=push_body(workloads, fresh_client(), kind, n),
                    user_id="u1",
                )
                return json_response(response.to_json())

            check_push(json.loads(await run_fake()), n)
            await bench(f"mutate_cpu.{kind}.n{n}", "python", run_fake)

    # ---- z2s compile, and tx.run end to end ----
    databases, close = await open_adapters(CONFIG["queryURL"], schema)
    server_schema = await databases["asyncpg"].connection.transaction(lambda tx: get_server_schema(tx, schema))
    compile_cases = {
        "simple": workloads["programs"]["simple"],
        "complex": workloads["programs"]["complex"],
        "related": with_limit(workloads["run"]["related"], workloads["compileRelatedLimit"]),
    }
    for k, program in compile_cases.items():
        q = build(program)
        await bench(f"z2s.{k}", "python", lambda q=q: compile_query(server_schema, schema, q.ast, q.format))
    for k, program in workloads["run"].items():
        for n in workloads["rowCounts"]:
            q = build(with_limit(program, n))
            for impl, db in databases.items():
                rows = await db.run(q)
                if len(rows) != n:
                    raise RuntimeError(f"run.{k} on {impl}: expected {n} rows, got {len(rows)}")
                await bench(f"run.{k}.r{n}", impl, lambda db=db, q=q: db.run(q))
    await close()

    # ---- mutate endpoint against Postgres ----
    connection = await asyncpg.connect(CONFIG["mutateURL"])
    await connection.execute('TRUNCATE item, things, pair, zero_0."clients", zero_0."mutations"')
    await connection.close()
    databases, close = await open_adapters(CONFIG["mutateURL"], wire_schema)
    for kind in ("put", "crud"):
        for n in workloads["pushSizes"]:
            for impl, db in databases.items():

                async def run_push(db: ZQLDatabase[Any] = db, kind: str = kind, n: int = n) -> dict[str, Any]:
                    response = await handle_mutate_request(
                        db_provider=mutators.provider(db),
                        handler=mutators.handler,
                        query=MUTATE_QUERY,
                        body=push_body(workloads, fresh_client(), kind, n),
                        user_id="u1",
                    )
                    return response.to_json()

                check_push(await run_push(), n)
                await bench(f"mutate.{kind}.n{n}", impl, run_push)
    for impl, db in databases.items():

        async def push_processor(db: ZQLDatabase[Any] = db) -> Any:
            body = push_body(workloads, fresh_client(), "put", 10)
            for m in body["mutations"]:
                m["name"] = "legacy.put"
            return await PushProcessor(mutators.provider(db), {"tag": "ctx"}).process(
                mutators.REGISTRY, MUTATE_QUERY, body
            )

        await bench("pushproc.put.n10", impl, push_processor)
    await close()

    sys.stdout.write(json.dumps(results) + "\n")


if __name__ == "__main__":
    asyncio.run(
        main(
            json.loads(Path(CONFIG["workloads"]).read_text()),
            load_schema(Path(CONFIG["schema"]).read_text()),
            load_schema(Path(CONFIG["wireSchema"]).read_text()),
            json.loads(Path(CONFIG["serverSchemaRows"]).read_text()),
        )
    )
