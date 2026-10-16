"""The request bodies and fakes both benchmark suites build from `workloads.json`, as
`harness/bench/workloads.ts` builds them for the TypeScript side."""

import json
import os
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from zero_server.schema import Schema, load_schema
from zero_server.server.transaction import DBTransaction, Row

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
W: dict[str, Any] = json.loads((HERE / "workloads.json").read_text())

type Program = dict[str, Any]

MUTATE_QUERY = {"schema": "zero_0", "appID": "zero"}


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def schema() -> Schema:
    """The wire tier's query schema, `harness/schema.ts`."""
    return load_schema((FIXTURES / "schema.json").read_text())


def wire_schema() -> Schema:
    """The wire tier's mutate schema, `wireSchema` in `harness/mutate.ts`."""
    return load_schema((FIXTURES / "wire_schema.json").read_text())


def with_limit(program: Program, n: int) -> Program:
    return {**program, "ops": [[n if x == "N" else x for x in op] for op in program["ops"]]}


def transform_body(program: Program, n: int) -> list[Any]:
    return ["transform", [{"id": f"q{i}", "name": "prog", "args": [program]} for i in range(n)]]


def push_body(client_id: str, kind: str, n: int) -> dict[str, Any]:
    def args(i: int) -> dict[str, Any]:
        if kind == "put":
            return {"id": f"{client_id}-{i}", "value": "v"}
        return {"table": "thing", "row": {"id": f"{client_id}-{i}", **W["thingRow"]}}

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
    """A client ID no push has used, so its mutation IDs start at 1."""
    global _client_seq
    _client_seq += 1
    return f"py{os.getpid()}-{_client_seq}"


def check_push(response: dict[str, Any], n: int) -> None:
    pushed = response.get("mutations", [])
    if len(pushed) != n or any("error" in m["result"] for m in pushed):
        raise RuntimeError(f"push failed: {json.dumps(response)[:500]}")


def json_response(value: Any) -> bytes:
    """What Starlette's `JSONResponse` does with a handler's result."""
    from zero_server.shared.js import js_stringify_bytes

    return js_stringify_bytes(value)


class FakeTransaction:
    """Answers the SDK's own statements from memory: the server-schema query from `rows`, the
    lastMutationID bump from a counter, and everything else with no rows."""

    def __init__(self, rows: list[Row]) -> None:
        self.rows = rows
        self.last_mutation_ids: dict[str, int] = {}

    @property
    def wrapped_transaction(self) -> None:
        return None

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        if "pg_catalog" in sql:
            return self.rows
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
