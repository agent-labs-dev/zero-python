"""The Python worker of the paired wall-clock benchmarks: `harness/bench/cases.ts` in Python, case for case,
speaking the same line protocol to the driver in `bench/__main__.py`.

Usage: `python -m bench.cases <config.json>`, then one JSON request per line on stdin (see cases.ts).
"""

import asyncio
import inspect
import json
import re
import sys
import time
from collections.abc import Callable, Coroutine
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.ext.asyncio import create_async_engine

from bench import cpu
from bench.workloads import MUTATE_QUERY, W, check_push, fresh_client, push_body, schema, wire_schema, with_limit
from tests.wire import mutators
from tests.wire.programs import apply
from zero_server.query.builder import create_builder
from zero_server.schema import Schema
from zero_server.server.adapters.asyncpg import AsyncpgDatabase
from zero_server.server.adapters.psycopg import PsycopgDatabase
from zero_server.server.adapters.sqlalchemy import SQLAlchemyDatabase
from zero_server.server.process_mutations import handle_mutate_request
from zero_server.server.push_processor import PushProcessor
from zero_server.server.zql_database import ZQLDatabase

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
    query_url: str
    mutate_url: str
    exit_stack: AsyncExitStack
    cases: dict[str, Case] = field(default_factory=dict)
    registered: list[tuple[str, str]] = field(default_factory=list)
    _adapters: dict[str, dict[str, ZQLDatabase[Any]]] = field(default_factory=dict)
    _cpu: dict[str, Callable[[], Any]] | None = None

    def register(self, name: str, impl: str, fn: Callable[[], Any]) -> None:
        if re.search(self.filter, name):
            self.cases[f"{name} {impl}"] = Case(fn)
            self.registered.append((name, impl))

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


async def run(b: Bench) -> None:
    """`tx.run` end to end: compile, query, decode."""
    query_schema = schema()
    zql = create_builder(query_schema)
    databases = await b.adapters(b.query_url, query_schema)
    for k, program in W["run"].items():
        for n in W["rowCounts"]:
            limited = with_limit(program, n)
            q = apply(zql.table(limited["table"]), limited["ops"])
            for impl, db in databases.items():
                rows = await db.run(q)
                if len(rows) != n:
                    raise RuntimeError(f"run.{k} on {impl}: expected {n} rows, got {len(rows)}")
                b.register(f"run.{k}.r{n}", impl, lambda db=db, q=q: db.run(q))


async def mutate(b: Bench) -> None:
    mutators.use_schema(wire_schema())
    databases = await b.adapters(b.mutate_url, wire_schema())
    for kind in ("put", "crud"):
        for n in W["pushSizes"]:
            for impl, db in databases.items():

                async def push(db: ZQLDatabase[Any] = db, kind: str = kind, n: int = n) -> dict[str, Any]:
                    response = await handle_mutate_request(
                        db_provider=mutators.provider(db),
                        handler=mutators.handler,
                        query=MUTATE_QUERY,
                        body=push_body(fresh_client(), kind, n),
                        user_id="u1",
                    )
                    return response.to_json()

                check_push(await push(), n)
                b.register(f"mutate.{kind}.n{n}", impl, push)


async def pushproc(b: Bench) -> None:
    mutators.use_schema(wire_schema())
    databases = await b.adapters(b.mutate_url, wire_schema())
    for impl, db in databases.items():

        async def process(db: ZQLDatabase[Any] = db) -> Any:
            body = push_body(fresh_client(), "put", 10)
            for m in body["mutations"]:
                m["name"] = "legacy.put"
            return await PushProcessor(mutators.provider(db), {"tag": "ctx"}).process(
                mutators.REGISTRY, MUTATE_QUERY, body
            )

        b.register("pushproc.put.n10", impl, process)


def cpu_group(group: str) -> Callable[[Bench], Coroutine[Any, Any, None]]:
    """The CPU-only cases of `group`, from `bench/cpu.py`."""

    async def setup(b: Bench) -> None:
        if b._cpu is None:
            b._cpu = await cpu.cases()
        for name, fn in b._cpu.items():
            if name.split(".")[0] == group:
                b.register(name, "python", fn)

    return setup


GROUPS: dict[str, Callable[[Bench], Coroutine[Any, Any, None]]] = {
    **{g: cpu_group(g) for g in ("query", "query_json", "build", "map", "parse", "parse_json", "mutate_cpu", "z2s")},
    "run": run,
    "mutate": mutate,
    "pushproc": pushproc,
}
"""Each group's cases. A case's group is its name's first part."""


async def serve(config: dict[str, Any]) -> None:
    """Answer the driver's requests until stdin closes."""
    async with AsyncExitStack() as exit_stack:
        b = Bench(config["filter"], config["queryURL"], config["mutateURL"], exit_stack)
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
    asyncio.run(serve(json.loads(Path(config_path).read_text())))


if __name__ == "__main__":
    main(sys.argv[1])
