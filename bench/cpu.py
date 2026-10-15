"""The CPU-only cases, Python side: every case that does no I/O, named as `harness/bench/cpu_cases.ts`
names the TypeScript side. `bench/test_cpu.py` runs them under CodSpeed; `python -m bench` times them
against TypeScript on the wall clock.

A case is a callable with no arguments. The async ones are `async def`; the rest return their result.
"""

import json
from collections.abc import Callable
from typing import Any

from pydantic import TypeAdapter

from bench.workloads import (
    MUTATE_QUERY,
    FakeConnection,
    FakeTransaction,
    W,
    check_push,
    fixture,
    fresh_client,
    json_response,
    push_body,
    schema,
    transform_body,
    wire_schema,
    with_limit,
)
from tests.wire import mutators
from tests.wire.programs import apply, make_handler
from zero_server.protocol.custom_queries import TransformRequestMessage
from zero_server.protocol.push import PushBody
from zero_server.protocol.wire import parse_wire
from zero_server.query.builder import create_builder
from zero_server.server.process_mutations import handle_mutate_request
from zero_server.server.process_queries import handle_query_request
from zero_server.server.server_schema import get_server_schema
from zero_server.server.zql_database import ZQLDatabase
from zero_server.z2s.compiler import compile_query

TRANSFORM: TypeAdapter[Any] = TypeAdapter(TransformRequestMessage)


async def cases() -> dict[str, Callable[[], Any]]:
    out: dict[str, Callable[[], Any]] = {}
    query_schema, mutate_schema = schema(), wire_schema()
    handler = make_handler(query_schema)

    for kind in ("simple", "complex"):
        for n in W["batchSizes"]:
            body = transform_body(W["programs"][kind], n)
            text = json.dumps(body).encode()

            async def query(body: Any = body) -> Any:
                return await handle_query_request(handler=handler, schema=query_schema, body=body, user_id="u1")

            async def query_json(text: bytes = text) -> bytes:
                response = await handle_query_request(handler=handler, schema=query_schema, body=text, user_id="u1")
                return json_response(response.to_json())

            out[f"query.{kind}.b{n}"] = query
            out[f"query_json.{kind}.b{n}"] = query_json

    for m in W["parseSizes"]:
        push = push_body("c1", "crud", m)
        push_text = json.dumps(push).encode()
        transform = transform_body(W["programs"]["complex"], m)
        transform_text = json.dumps(transform).encode()
        out[f"parse.push.m{m}"] = lambda push=push: PushBody.from_json(push)
        out[f"parse.transform.q{m}"] = lambda t=transform: parse_wire(TRANSFORM, t)
        out[f"parse_json.push.m{m}"] = lambda t=push_text: PushBody.from_json(t)
        out[f"parse_json.transform.q{m}"] = lambda t=transform_text: parse_wire(TRANSFORM, t)

    # The mutate endpoint over a database that answers from memory: the SDK's own work, no I/O.
    mutators.use_schema(mutate_schema)
    fake_db: ZQLDatabase[Any] = ZQLDatabase(
        FakeConnection(FakeTransaction(fixture("mutate_server_schema_rows.json"))), mutate_schema
    )
    for kind in ("put", "crud"):
        for n in W["pushSizes"]:

            async def push_fake(kind: str = kind, n: int = n) -> bytes:
                response = await handle_mutate_request(
                    db_provider=fake_db,
                    handler=mutators.handler,
                    query=MUTATE_QUERY,
                    body=push_body(fresh_client(), kind, n),
                    user_id="u1",
                )
                return json_response(response.to_json())

            check_push(json.loads(await push_fake()), n)
            out[f"mutate_cpu.{kind}.n{n}"] = push_fake

    server_schema = await get_server_schema(FakeTransaction(fixture("query_server_schema_rows.json")), query_schema)
    zql = create_builder(query_schema)
    for name, program in {
        "simple": W["programs"]["simple"],
        "complex": W["programs"]["complex"],
        "related": with_limit(W["run"]["related"], W["compileRelatedLimit"]),
    }.items():
        q = apply(zql.table(program["table"]), program["ops"])
        out[f"z2s.{name}"] = lambda q=q: compile_query(server_schema, query_schema, q.ast, q.format)
    return out
