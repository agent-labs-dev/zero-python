"""Raw request bodies, as a server receives them, through upstream's `Request` path and Python's bytes path.

Responses are compared as the text each side sends: key order and number spelling included, since
zero-cache may hash what it receives.
"""

import base64
import json
import re
from typing import Any

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from starlette.applications import Starlette
from starlette.datastructures import QueryParams
from starlette.testclient import TestClient

from tests.harness import Harness
from tests.strategies import client_ids, strings, transform_requests
from tests.wire.conftest import WireDB
from tests.wire.mutators import handler, provider, use_schema
from tests.wire.programs import make_queries, programs
from zero_server.integrations.starlette import zero_routes
from zero_server.schema import Schema, load_schema
from zero_server.server.process_mutations import handle_mutate_request
from zero_server.shared.js import js_stringify

BOM = b"\xef\xbb\xbf"
# Odd things a body may hold: JSON's missing constants, numbers past a double, and bytes that are not
# UTF-8 (a lone byte, an encoded surrogate, a cut-off sequence).
NUMBERS = [b"NaN", b"Infinity", b"-Infinity", b"1e400", b"-0", b"123456789012345678901"]
NOT_UTF8 = [b"\xff", b"\xed\xa0\x80", b"\xf0\x9f"]
NUMBER = re.compile(rb"(?<=[\[:,])-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
ODD_BODIES = [b"", b" ", b"null", b"[]", b"{}", BOM, BOM + BOM + b"[]", BOM * 3 + b"[]", b"\xff", b'"\xed\xa0\x80"']


HELD = re.compile(rb'"\\u0001(\d+)\\u0001"')


def _hold(value: Any, key: str, held: list[Any]) -> Any:
    """`value` with whatever sits under `key` swapped for a placeholder string, kept in `held`."""
    match value:
        case dict():
            out = {}
            for k, v in value.items():
                if k == key:
                    held.append(v)
                    out[k] = f"\x01{len(held) - 1}\x01"
                else:
                    out[k] = _hold(v, key, held)
            return out
        case list():
            return [_hold(v, key, held) for v in value]
        case _:
            return value


@st.composite
def bodies(draw: st.DrawFn, valid: st.SearchStrategy[Any], protect: str | None = None) -> bytes:
    """A valid body as JSON, or that body with one defect spliced in.

    No defect lands under a `protect` key: what sits there is the app's own input, which each side's app
    code rejects in its own language's words.
    """
    held: list[Any] = []
    value = draw(valid)
    body = json.dumps(_hold(value, protect, held) if protect else value).encode()
    spans = [range(m.start(), m.end()) for m in HELD.finditer(body)]

    def free(at: int) -> bool:
        return not any(at in span for span in spans)

    defect = draw(st.sampled_from(["as is", "number", "string", "anywhere", "bom", "utf-16", "truncated"]))
    match defect:
        case "number":
            numbers = [m for m in NUMBER.finditer(body) if free(m.start())]
            if numbers:
                m = draw(st.sampled_from(numbers))
                body = body[: m.start()] + draw(st.sampled_from(NUMBERS)) + body[m.end() :]
        case "string":
            quotes = [i for i, c in enumerate(body) if c == ord('"') and free(i)]
            if quotes:
                at = draw(st.sampled_from(quotes)) + 1
                body = body[:at] + draw(st.sampled_from(NOT_UTF8)) + body[at:]
        case "anywhere":
            inside = lambda at: any(at - 1 in span and at in span for span in spans)  # noqa: E731
            at = draw(st.integers(0, len(body)).filter(lambda at: not inside(at)))
            splice = draw(st.sampled_from([*NUMBERS, *NOT_UTF8, BOM, b" ", b"\t", b"\x0b", b"\xc2\xa0"]))
            body = body[:at] + splice + body[at:]
    body = HELD.sub(lambda m: json.dumps(held[int(m[1])]).encode(), body)
    match defect:
        case "bom":
            body = BOM + body
        case "utf-16":
            body = body.decode().encode("utf-16")
        case "truncated":
            body = body[: draw(st.integers(0, len(body)))]
    return body


def _with_odd_bodies(*rest: Any) -> Any:
    def decorate(test: Any) -> Any:
        for body in ODD_BODIES:
            test = example(body, *rest)(test)
        return test

    return decorate


def _comparable(text: str) -> str:
    """The response text with a parse failure's message cut to its prefix.

    The rest of that message is valita's, Pydantic's or the JSON parser's own wording.
    """
    response = json.loads(text)
    if response.get("reason") != "parse":
        return text
    message = response["message"]
    return text.replace(js_stringify(message), js_stringify(message.split(":", 1)[0]))


def _b64(body: bytes) -> str:
    return base64.b64encode(body).decode()


@pytest.fixture(scope="session")
def schema(harness: Harness) -> Schema:
    return load_schema(harness.call("schema"))


def test_query_bodies_match_upstream(harness: Harness, schema: Schema) -> None:
    user: dict[str, str | None] = {"id": None}
    routes = zero_routes(
        schema=schema, queries=make_queries(schema), authenticate=lambda _r: user["id"], context=lambda *_: None
    )
    client = TestClient(Starlette(routes=routes))
    request = st.builds(
        lambda i, name, program: {"id": i, "name": name, "args": [program]},
        strings(2),
        # `validated` refuses arguments a splice has broken; `prog` would fail on them as each language does.
        st.sampled_from(["validated", "appError", "parseError", "missing", "m\udc00"]),
        programs(schema, depth=1),
    )
    valid = st.builds(lambda qs: ["transform", qs], st.lists(request, max_size=2)) | transform_requests()

    @given(bodies(valid, protect="args"), st.sampled_from([None, "u1", "u\ud800"]))
    @example(b'["transform",[{"id":"1","name":"prog","args":[NaN]}]]', None)
    @example(BOM + b'["transform",[]]', None)
    @example(
        b'["transform",[{"id":"1","name":"validated","args":[{"table":"comment","ops":[["where","authorID",[1e400]]]}]}]]',
        None,
    )
    @_with_odd_bodies(None)
    def check(body: bytes, user_id: str | None) -> None:
        user["id"] = user_id
        expected = harness.call("queryRequest", body=_b64(body), userID=user_id)
        actual = client.post("/query", content=body)
        assert actual.headers["content-type"] == "application/json"
        assert _comparable(actual.content.decode()) == _comparable(expected)

    check()


SEARCHES = ["schema=zero_0&appID=zero", "schema=zero_0", "appID=zero&schema=zero_0&x=%ED%A0"]


@st.composite
def pushes(draw: st.DrawFn) -> dict[str, Any]:
    mutations = []
    next_id: dict[str, int] = {}
    for _ in range(draw(st.integers(0, 3))):
        client = draw(client_ids)
        next_id[client] = next_id.get(client, 0) + 1
        name = draw(st.sampled_from(["item.put", "item.appError", "item.error"]))
        args = {"id": draw(strings(2)), "value": draw(strings(3)), "message": draw(strings(3))}
        mutations.append(
            {
                "type": "custom",
                "id": next_id[client],
                "clientID": client,
                "name": name,
                "args": [args],
                "timestamp": 1.5,
            }
        )
    return {"clientGroupID": "cg1", "mutations": mutations, "pushVersion": 1, "timestamp": 1, "requestID": "r"}


def test_mutate_bodies_match_upstream(harness: Harness, wire_db: WireDB) -> None:
    schema = load_schema(harness.call("wireSchema"))
    use_schema(schema)
    py_db = provider(wire_db.py["asyncpg"](schema))

    @given(bodies(pushes()), st.sampled_from(SEARCHES), st.sampled_from([None, "u1"]))
    @example(
        b'{"clientGroupID":"cg1","mutations":[],"pushVersion":NaN,"timestamp":1,"requestID":"r"}', SEARCHES[0], None
    )
    @example(
        b'{"clientGroupID":"cg1","mutations":[],"pushVersion":-1e400,"timestamp":1,"requestID":"r"}', SEARCHES[0], None
    )
    @example(
        b'{"clientGroupID":"cg1","mutations":[{"type":"custom","id":1e400,"clientID":"c1","name":"item.put","args":[],'
        b'"timestamp":1}],"pushVersion":1,"timestamp":1,"requestID":"r"}',
        SEARCHES[1],
        None,
    )
    @_with_odd_bodies(SEARCHES[0], None)
    def check(body: bytes, search: str, user_id: str | None) -> None:
        wire_db.reset()
        expected = harness.call("mutateRequest", url=wire_db.ts_url, search=search, body=_b64(body), userID=user_id)
        actual = wire_db.run(
            handle_mutate_request(
                db_provider=py_db, handler=handler, query=dict(QueryParams(search)), body=body, user_id=user_id
            )
        )
        assert _comparable(js_stringify(actual.to_json())) == _comparable(expected)

    check()
