"""The Python side of the benchmark tier: `harness/bench/cases.ts` in Python, group for group and case
for case, speaking the same line protocol to the driver in `bench/__main__.py`.

Usage: `python -m bench.cases <config.json>`, then one JSON request per line on stdin (see cases.ts).
"""

import asyncio
import inspect
import json
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
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

SAMPLE_MS = 1
"""A sample takes at least this long, so timer resolution and a stray interrupt matter less."""


@dataclass
class Case:
    fn: Callable[[], Any]
    is_async: bool = False
    inner: int = 1

    async def loop(self, n: int) -> None:
        if self.is_async:
            for _ in range(n):
                await self.fn()
        else:
            for _ in range(n):
                self.fn()

    async def warm(self, ms: float) -> None:
        """Run for `ms`, then size a sample to `SAMPLE_MS`."""
        first = self.fn()
        self.is_async = inspect.isawaitable(first)
        if inspect.isawaitable(first):
            await first
        start = time.perf_counter()
        calls = 0
        while time.perf_counter() < start + ms / 1000 or calls < 5:
            await self.loop(1)
            calls += 1
        per_call_ms = (time.perf_counter() - start) * 1000 / calls
        self.inner = max(1, min(10_000, -int(-SAMPLE_MS // per_call_ms)))

    async def time(self, ms: float) -> float:
        """The median microseconds per call over samples taken for `ms`."""
        samples: list[float] = []
        end = time.perf_counter() + ms / 1000
        while time.perf_counter() < end or not samples:
            t0 = time.perf_counter()
            await self.loop(self.inner)
            samples.append((time.perf_counter() - t0) * 1e6 / self.inner)
        samples.sort()
        return samples[len(samples) // 2]


@dataclass
class Bench:
    filter: str
    workloads: dict[str, Any]
    schema: Schema
    wire_schema: Schema
    server_schema_rows: list[Row]
    query_url: str
    mutate_url: str
    exit_stack: AsyncExitStack
    cases: dict[str, Case] = field(default_factory=dict)
    registered: list[tuple[str, str]] = field(default_factory=list)
    _adapters: dict[str, dict[str, ZQLDatabase[Any]]] = field(default_factory=dict)

    def register(self, name: str, impl: str, fn: Callable[[], Any]) -> None:
        if re.search(self.filter, name):
            self.cases[f"{name} {impl}"] = Case(fn)
            self.registered.append((name, impl))

    def build(self, program: Program) -> Query:
        return apply(create_builder(self.schema).table(program["table"]), program["ops"])

    async def adapters(self, url: str, schema: Schema) -> dict[str, ZQLDatabase[Any]]:
        """One `ZQLDatabase` per Python adapter on the database at `url`, open until the worker exits."""
        if url not in self._adapters:
            pool = await asyncpg.create_pool(url, min_size=1, max_size=10)
            self.exit_stack.push_async_callback(pool.close)
            psycopg_pool = AsyncConnectionPool(url, min_size=1, max_size=10, open=False)
            await psycopg_pool.open()
            self.exit_stack.push_async_callback(psycopg_pool.close)
            engine = create_async_engine(url.replace("postgresql://", "postgresql+asyncpg://", 1))
            self.exit_stack.push_async_callback(engine.dispose)
            self._adapters[url] = {
                "asyncpg": AsyncpgDatabase(pool, schema),
                "psycopg": PsycopgDatabase(psycopg_pool, schema),
                "sqlalchemy": SQLAlchemyDatabase(engine, schema),
            }
        return self._adapters[url]


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


async def query(b: Bench) -> None:
    handler = make_handler(b.schema)
    for kind in ("simple", "complex"):
        for n in b.workloads["batchSizes"]:
            body = transform_body(b.workloads["programs"][kind], n)
            b.register(
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

            b.register(f"query_json.{kind}.b{n}", "python", with_json)


TRANSFORM: TypeAdapter[Any] = TypeAdapter(TransformRequestMessage)


async def parse(b: Bench) -> None:
    for m in b.workloads["parseSizes"]:
        push = push_body(b.workloads, "c1", "crud", m)
        b.register(f"parse.push.m{m}", "python", lambda push=push: PushBody.from_json(push))
        transform = transform_body(b.workloads["programs"]["complex"], m)
        b.register(f"parse.transform.q{m}", "python", lambda t=transform: parse_wire(TRANSFORM, t))


async def parse_json(b: Bench) -> None:
    for m in b.workloads["parseSizes"]:
        push_text = json.dumps(push_body(b.workloads, "c1", "crud", m)).encode()
        b.register(f"parse_json.push.m{m}", "python", lambda t=push_text: PushBody.from_json(json.loads(t)))
        transform_text = json.dumps(transform_body(b.workloads["programs"]["complex"], m)).encode()
        b.register(
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
            b.register(f"mutate_cpu.{kind}.n{n}", "python", run)


async def z2s(b: Bench) -> None:
    databases = await b.adapters(b.query_url, b.schema)
    server_schema = await databases["asyncpg"].connection.transaction(lambda tx: get_server_schema(tx, b.schema))
    cases = {
        "simple": b.workloads["programs"]["simple"],
        "complex": b.workloads["programs"]["complex"],
        "related": with_limit(b.workloads["run"]["related"], b.workloads["compileRelatedLimit"]),
    }
    for k, program in cases.items():
        q = b.build(program)
        b.register(f"z2s.{k}", "python", lambda q=q: compile_query(server_schema, b.schema, q.ast, q.format))


async def run(b: Bench) -> None:
    """`tx.run` end to end: compile, query, decode."""
    databases = await b.adapters(b.query_url, b.schema)
    for k, program in b.workloads["run"].items():
        for n in b.workloads["rowCounts"]:
            q = b.build(with_limit(program, n))
            for impl, db in databases.items():
                rows = await db.run(q)
                if len(rows) != n:
                    raise RuntimeError(f"run.{k} on {impl}: expected {n} rows, got {len(rows)}")
                b.register(f"run.{k}.r{n}", impl, lambda db=db, q=q: db.run(q))


async def mutate(b: Bench) -> None:
    mutators.use_schema(b.wire_schema)
    databases = await b.adapters(b.mutate_url, b.wire_schema)
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
                b.register(f"mutate.{kind}.n{n}", impl, push)


async def pushproc(b: Bench) -> None:
    mutators.use_schema(b.wire_schema)
    databases = await b.adapters(b.mutate_url, b.wire_schema)
    for impl, db in databases.items():

        async def process(db: ZQLDatabase[Any] = db) -> Any:
            body = push_body(b.workloads, fresh_client(), "put", 10)
            for m in body["mutations"]:
                m["name"] = "legacy.put"
            return await PushProcessor(mutators.provider(db), {"tag": "ctx"}).process(
                mutators.REGISTRY, MUTATE_QUERY, body
            )

        b.register("pushproc.put.n10", impl, process)


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


async def serve(inputs: dict[str, Any]) -> None:
    """Answer the driver's requests until stdin closes. `inputs` are `Bench`'s fields, files already read."""
    async with AsyncExitStack() as exit_stack:
        b = Bench(**inputs, exit_stack=exit_stack)
        while line := await asyncio.to_thread(sys.stdin.readline):
            req = json.loads(line)
            if req["op"] == "setup":
                b.registered = []
                await GROUPS[req["group"]](b)
                res: dict[str, Any] = {"cases": b.registered}
            elif req["op"] == "warm":
                await b.cases[f"{req['case']} {req['impl']}"].warm(req["ms"])
                res = {}
            else:
                res = {"us": await b.cases[f"{req['case']} {req['impl']}"].time(req["ms"])}
            sys.stdout.write(json.dumps(res) + "\n")
            sys.stdout.flush()


def main(config_path: str) -> None:
    config = json.loads(Path(config_path).read_text())
    inputs = {
        "filter": config["filter"],
        "workloads": json.loads(Path(config["workloads"]).read_text()),
        "schema": load_schema(Path(config["schema"]).read_text()),
        "wire_schema": load_schema(Path(config["wireSchema"]).read_text()),
        "server_schema_rows": json.loads(Path(config["serverSchemaRows"]).read_text()),
        "query_url": config["queryURL"],
        "mutate_url": config["mutateURL"],
    }
    asyncio.run(serve(inputs))


if __name__ == "__main__":
    main(sys.argv[1])
