"""The Python side of the benchmark tier: `harness/bench/cases.ts` in Python, group for group.

Run by `python -m bench` as `python -m bench.cases <config.json> <group>`. Prints one JSON line of
results to stdout.
"""

import asyncio
import inspect
import json
import os
import re
import sys
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
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

WARMUP = 0.5
"""Warmup time, as a fraction of the sampling budget."""
SAMPLE_MS = 1
"""The least time one sample takes: enough calls that timer resolution and a stray interrupt matter less."""


@dataclass
class Bench:
    filter: str
    budget_ms: float
    workloads: dict[str, Any]
    schema: Schema
    wire_schema: Schema
    server_schema_rows: list[Row]
    query_url: str
    mutate_url: str
    results: list[dict[str, Any]]

    async def time(self, name: str, impl: str, fn: Callable[[], Any]) -> None:
        """The median time of one call of `fn`, by the same procedure as `harness/bench/cases.ts`."""
        if not re.search(self.filter, name):
            return
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

        budget = self.budget_ms / 1000
        warm_start = time.perf_counter()
        calls = 0
        while time.perf_counter() < warm_start + budget * WARMUP or calls < 5:
            await loop(1)
            calls += 1
        per_call_ms = (time.perf_counter() - warm_start) * 1000 / calls
        inner = max(1, min(10_000, -int(-SAMPLE_MS // per_call_ms)))
        samples: list[float] = []
        end = time.perf_counter() + budget
        while (time.perf_counter() < end or len(samples) < 7) and len(samples) < 5000:
            t0 = time.perf_counter()
            await loop(inner)
            samples.append((time.perf_counter() - t0) * 1e6 / inner)
        samples.sort()
        self.results.append(
            {"case": name, "impl": impl, "median_us": samples[len(samples) // 2], "samples": len(samples)}
        )

    def build(self, program: Program) -> Query:
        return apply(create_builder(self.schema).table(program["table"]), program["ops"])


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


@asynccontextmanager
async def adapters(url: str, schema: Schema) -> AsyncGenerator[dict[str, ZQLDatabase[Any]]]:
    """One `ZQLDatabase` per Python adapter on the database at `url`."""
    pool = await asyncpg.create_pool(url, min_size=1, max_size=10)
    psycopg_pool = AsyncConnectionPool(url, min_size=1, max_size=10, open=False)
    await psycopg_pool.open()
    engine = create_async_engine(url.replace("postgresql://", "postgresql+asyncpg://", 1))
    try:
        databases: dict[str, ZQLDatabase[Any]] = {
            "asyncpg": AsyncpgDatabase(pool, schema),
            "psycopg": PsycopgDatabase(psycopg_pool, schema),
            "sqlalchemy": SQLAlchemyDatabase(engine, schema),
        }
        yield databases
    finally:
        await pool.close()
        await psycopg_pool.close()
        await engine.dispose()


async def query(b: Bench) -> None:
    handler = make_handler(b.schema)
    for kind in ("simple", "complex"):
        for n in b.workloads["batchSizes"]:
            body = transform_body(b.workloads["programs"][kind], n)
            await b.time(
                f"query.{kind}.b{n}",
                "python",
                lambda body=body: handle_query_request(handler=handler, schema=b.schema, body=body, user_id="u1"),
            )


async def query_json(b: Bench) -> None:
    """What an HTTP endpoint adds: parsing the request text and serializing the response."""
    handler = make_handler(b.schema)
    for kind in ("simple", "complex"):
        for n in b.workloads["batchSizes"]:
            text = json.dumps(transform_body(b.workloads["programs"][kind], n)).encode()

            async def with_json(text: bytes = text) -> bytes:
                response = await handle_query_request(handler=handler, schema=b.schema, body=text, user_id="u1")
                return json_response(response.to_json())

            await b.time(f"query_json.{kind}.b{n}", "python", with_json)


TRANSFORM: TypeAdapter[Any] = TypeAdapter(TransformRequestMessage)


async def parse(b: Bench) -> None:
    for m in b.workloads["parseSizes"]:
        push = push_body(b.workloads, "c1", "crud", m)
        await b.time(f"parse.push.m{m}", "python", lambda push=push: PushBody.from_json(push))
        transform = transform_body(b.workloads["programs"]["complex"], m)
        await b.time(f"parse.transform.q{m}", "python", lambda t=transform: parse_wire(TRANSFORM, t))


async def parse_json(b: Bench) -> None:
    for m in b.workloads["parseSizes"]:
        push_text = json.dumps(push_body(b.workloads, "c1", "crud", m)).encode()
        await b.time(f"parse_json.push.m{m}", "python", lambda t=push_text: PushBody.from_json(json.loads(t)))
        transform_text = json.dumps(transform_body(b.workloads["programs"]["complex"], m)).encode()
        await b.time(
            f"parse_json.transform.q{m}",
            "python",
            lambda t=transform_text: parse_wire(TRANSFORM, json.loads(t)),
        )


async def mutate_cpu(b: Bench) -> None:
    """The mutate endpoint over a fake database that answers from memory: the SDK's time only."""
    mutators.use_schema(b.wire_schema)
    fake_db: ZQLDatabase[Any] = ZQLDatabase(FakeConnection(FakeTransaction(b.server_schema_rows)), b.wire_schema)
    for kind in ("put", "crud"):
        for n in b.workloads["pushSizes"]:

            async def run(kind: str = kind, n: int = n) -> bytes:
                response = await handle_mutate_request(
                    db_provider=fake_db,
                    handler=mutators.handler,
                    query=MUTATE_QUERY,
                    body=push_body(b.workloads, fresh_client(), kind, n),
                    user_id="u1",
                )
                return json_response(response.to_json())

            check_push(json.loads(await run()), n)
            await b.time(f"mutate_cpu.{kind}.n{n}", "python", run)


async def z2s(b: Bench) -> None:
    async with adapters(b.query_url, b.schema) as databases:
        server_schema = await databases["asyncpg"].connection.transaction(lambda tx: get_server_schema(tx, b.schema))
    cases = {
        "simple": b.workloads["programs"]["simple"],
        "complex": b.workloads["programs"]["complex"],
        "related": with_limit(b.workloads["run"]["related"], b.workloads["compileRelatedLimit"]),
    }
    for k, program in cases.items():
        q = b.build(program)
        await b.time(f"z2s.{k}", "python", lambda q=q: compile_query(server_schema, b.schema, q.ast, q.format))


async def run(b: Bench) -> None:
    """`tx.run` end to end: compile, query, decode."""
    async with adapters(b.query_url, b.schema) as databases:
        for k, program in b.workloads["run"].items():
            for n in b.workloads["rowCounts"]:
                q = b.build(with_limit(program, n))
                for impl, db in databases.items():
                    rows = await db.run(q)
                    if len(rows) != n:
                        raise RuntimeError(f"run.{k} on {impl}: expected {n} rows, got {len(rows)}")
                    await b.time(f"run.{k}.r{n}", impl, lambda db=db, q=q: db.run(q))


async def mutate(b: Bench) -> None:
    connection = await asyncpg.connect(b.mutate_url)
    await connection.execute('TRUNCATE item, things, pair, zero_0."clients", zero_0."mutations"')
    await connection.close()
    mutators.use_schema(b.wire_schema)
    async with adapters(b.mutate_url, b.wire_schema) as databases:
        for kind in ("put", "crud"):
            for n in b.workloads["pushSizes"]:
                for impl, db in databases.items():

                    async def push(db: ZQLDatabase[Any] = db, kind: str = kind, n: int = n) -> dict[str, Any]:
                        response = await handle_mutate_request(
                            db_provider=mutators.provider(db),
                            handler=mutators.handler,
                            query=MUTATE_QUERY,
                            body=push_body(b.workloads, fresh_client(), kind, n),
                            user_id="u1",
                        )
                        return response.to_json()

                    check_push(await push(), n)
                    await b.time(f"mutate.{kind}.n{n}", impl, push)


async def pushproc(b: Bench) -> None:
    mutators.use_schema(b.wire_schema)
    async with adapters(b.mutate_url, b.wire_schema) as databases:
        for impl, db in databases.items():

            async def process(db: ZQLDatabase[Any] = db) -> Any:
                body = push_body(b.workloads, fresh_client(), "put", 10)
                for m in body["mutations"]:
                    m["name"] = "legacy.put"
                return await PushProcessor(mutators.provider(db), {"tag": "ctx"}).process(
                    mutators.REGISTRY, MUTATE_QUERY, body
                )

            await b.time("pushproc.put.n10", impl, process)


GROUPS: dict[str, Callable[[Bench], Coroutine[Any, Any, None]]] = {
    "query": query,
    "query_json": query_json,
    "parse": parse,
    "parse_json": parse_json,
    "mutate_cpu": mutate_cpu,
    "z2s": z2s,
    "run": run,
    "mutate": mutate,
    "pushproc": pushproc,
}
"""Each group's cases, with only the setup that group needs. A case's group is its name's first part."""


def main(config_path: str, group: str) -> None:
    config = json.loads(Path(config_path).read_text())
    b = Bench(
        filter=config["filter"],
        budget_ms=config["budgetMs"],
        workloads=json.loads(Path(config["workloads"]).read_text()),
        schema=load_schema(Path(config["schema"]).read_text()),
        wire_schema=load_schema(Path(config["wireSchema"]).read_text()),
        server_schema_rows=json.loads(Path(config["serverSchemaRows"]).read_text()),
        query_url=config["queryURL"],
        mutate_url=config["mutateURL"],
        results=[],
    )
    asyncio.run(GROUPS[group](b))
    sys.stdout.write(json.dumps(b.results) + "\n")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
