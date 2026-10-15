"""The HTTP tier: the bytes of one request through upstream's handlers, given a Fetch `Request`, and through
Python's Starlette routes. Diffs what the client would receive and every row either side wrote.

Upstream reads the query string with `URLSearchParams` and the body with `Request.json()`, and the app
sends its result through `JSON.stringify`, so this is where Python's reading of raw bytes and its writing
of the response are compared."""

import base64
import json
from typing import Any

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from starlette.applications import Starlette
from starlette.datastructures import QueryParams
from starlette.requests import Request

from tests.harness import Harness
from tests.strategies import as_js
from tests.wire.conftest import WireDB
from tests.wire.mutators import HTTP_REGISTRY, REGISTRY, provider, use_schema
from tests.wire.programs import make_queries, programs
from tests.wire.test_json_parse import near_json
from tests.wire.test_mutate import PROCESSOR_NAMES, scenarios
from zero_server.integrations.starlette import zero_routes
from zero_server.schema import Schema, load_schema
from zero_server.server.push_processor import PushProcessor


async def post(app: Any, path: str, search: str, body: bytes, user_id: str | None) -> Any:
    """POST `body` to `app` at `path` + `search` over ASGI, returning the decoded JSON response."""
    headers = [] if user_id is None else [(b"x-user", user_id.encode())]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": search.removeprefix("?").encode(),
        "root_path": "",
        "headers": headers,
        "server": ("localhost", 80),
        "client": ("127.0.0.1", 1),
    }
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    status: list[int] = []
    chunks: list[bytes] = []

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status.append(message["status"])
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    assert status == [200]
    return json.loads(b"".join(chunks))


def _user(request: Request) -> str | None:
    return request.headers.get("x-user")


_PARAMS = [
    "schema=zero_0", "appID=zero", "schema=zero%5F0", "app%49D=zero", "appID=ze+ro", "appID=zero", "x=1",
    "schema", "=zero", "appID=%zz", "appID=%C3%A9", "appID=%FF", "schema=zero_0;appID=zero", "",
]  # fmt: skip
searches = st.one_of(
    st.just("?schema=zero_0&appID=zero"),
    st.just("?schema=zero_0&appID=zero"),
    st.just(""),
    st.lists(st.sampled_from(_PARAMS), max_size=4).map(lambda parts: "?" + "&".join(parts)),
)

# A lone surrogate is a mutator name a client could send, which the response echoes in its error. A
# namespace such as `item` resolves upstream, and fails in the app's `.fn(...)` call, worded from its source.
HTTP_NAMES = ["item.put", "item.put", "item.appError", "item.error", "nope", "x\ud800"]


@st.composite
def bodies(draw: st.DrawFn, value: Any) -> bytes:
    """`value` as a client would send it, or now and then bytes `Request.json()` would refuse."""
    text = json.dumps(value, ensure_ascii=draw(st.booleans()))
    match draw(st.sampled_from(["json"] * 8 + ["bom", "junk", "edit"])):
        case "json":
            return text.encode("utf-8", "surrogatepass")
        case "bom":
            return b"\xef\xbb\xbf" + text.encode("utf-8", "surrogatepass")
        case "junk":
            return text.encode("utf-8", "surrogatepass") + b"\xff"
        case _:
            return draw(near_json()).encode("utf-8", "surrogatepass")


def _b64(body: bytes) -> str:
    return base64.b64encode(body).decode()


_MUTATION = b'{"type": "custom", "id": %s, "clientID": "c1", "name": "%s", "args": [], "timestamp": 1}'
_PUSH = b'{"clientGroupID": "cg1", "mutations": [%s], "pushVersion": 1, "timestamp": 1, "requestID": "r"}'


def _nested(depth: int) -> bytes:
    return b"[" * depth + b"]" * depth


@example(
    [
        # Arguments nested past Pydantic's limits, and past where both sides run out of stack.
        *(
            (
                _PUSH
                % (_MUTATION % (b"%d" % (n + 1), b"item.put")).replace(
                    b'"args": []', b'"args": [' + _nested(depth) + b"]"
                ),
                "?schema=zero_0&appID=zero",
                None,
            )
            for n, depth in enumerate([300, 1000, 4000])
        ),
        (
            _PUSH % (_MUTATION % (b"4", b"item.put")).replace(b'"args": []', b'"args": [' + _nested(12000) + b"]"),
            "?schema=zero_0&appID=zero",
            None,
        ),
        # An infinite number, which `JSON.stringify` writes as null, and a name with a lone surrogate.
        (b'{"clientGroupID": 1e400}', "?schema=zero_0&appID=zero", None),
        (_PUSH % (_MUTATION % (b"1e400", b"item.put")), "?schema=zero_0&appID=zero", None),
        (_PUSH % (_MUTATION % (b"1", b"x\\ud800")), "?schema=zero_0&appID=zero", "u1"),
        (b'{"clientGroupID": 1}', "?schema=zero_0&appID=zero", None),
        (b"\xef\xbb\xbf{", "?schema=zero_0&appID=zero", "u1"),
        (b"[]", "?appID=zero", None),
    ]
)
@given(
    scenarios(HTTP_NAMES).flatmap(
        lambda pushes: st.tuples(
            *(st.tuples(bodies(body), searches, st.just(user_id)) for body, _query, user_id in pushes)
        ).map(list)
    )
)
def test_mutate_route_matches_upstream(harness: Harness, wire_db: WireDB, pushes: list[Any]) -> None:
    wire_db.reset()
    schema = load_schema(harness.call("wireSchema"))
    use_schema(schema)
    app = Starlette(
        routes=zero_routes(
            schema=schema,
            database=provider(wire_db.py["sqlalchemy"](schema)),
            mutators=HTTP_REGISTRY,
            authenticate=_user,
        )
    )
    for body, search, user_id in pushes:
        expected = harness.call("httpMutate", url=wire_db.ts_url, search=search, body=_b64(body), userID=user_id)
        actual = wire_db.run(post(app, "/mutate", search, body, user_id))
        assert as_js(actual) == as_js(expected)
    assert as_js(wire_db.dump(wire_db.py_engine)) == as_js(wire_db.dump(wire_db.ts_engine))


QUERY_NAMES = ["prog", "prog", "nested.prog", "missing", "appError", "returnsNothing", "x\ud800"]


@pytest.fixture(scope="session")
def schema(harness: Harness) -> Schema:
    return load_schema(harness.call("schema"))


def test_query_route_matches_upstream(harness: Harness, wire_db: WireDB, schema: Schema) -> None:
    app = Starlette(routes=zero_routes(schema=schema, queries=make_queries(schema), authenticate=_user))
    request = st.builds(
        lambda i, name, program: {"id": i, "name": name, "args": [program]},
        st.sampled_from(["1", "2", "\ud800"]),
        st.sampled_from(QUERY_NAMES),
        programs(schema, depth=1),
    )
    body = st.lists(request, max_size=2).flatmap(lambda requests: bodies(["transform", requests]))

    @given(body, searches, st.sampled_from([None, "u1"]))
    def check(body: bytes, search: str, user_id: str | None) -> None:
        expected = harness.call("httpQuery", search=search, body=_b64(body), userID=user_id)
        actual = wire_db.run(post(app, "/query", search, body, user_id))
        assert as_js(actual) == as_js(expected)

    check()


def test_mutate_route_reads_the_query_string_as_upstream(harness: Harness, wire_db: WireDB) -> None:
    """Only the query string varies, over a push upstream would otherwise accept."""
    schema = load_schema(harness.call("wireSchema"))
    app = Starlette(
        routes=zero_routes(
            schema=schema,
            database=provider(wire_db.py["sqlalchemy"](schema)),
            mutators=HTTP_REGISTRY,
            authenticate=_user,
        )
    )
    body = json.dumps(
        {"clientGroupID": "cg", "mutations": [], "pushVersion": 1, "timestamp": 1, "requestID": "r"}
    ).encode()

    @given(searches)
    def check(search: str) -> None:
        expected = harness.call("httpMutate", url=wire_db.ts_url, search=search, body=_b64(body), userID=None)
        assert as_js(wire_db.run(post(app, "/mutate", search, body, None))) == as_js(expected)

    check()


@given(
    scenarios(PROCESSOR_NAMES).flatmap(
        lambda pushes: st.tuples(*(st.tuples(bodies(body), searches) for body, _query, _user in pushes)).map(list)
    )
)
def test_push_processor_reads_a_request_as_upstream(harness: Harness, wire_db: WireDB, pushes: list[Any]) -> None:
    """`PushProcessor.process(mutators, request)` upstream; in Python, the query string as Starlette parses
    it and the body as bytes."""
    wire_db.reset()
    processor = PushProcessor(provider(wire_db.py["sqlalchemy"](None)), context={"tag": "ctx"})
    for body, search in pushes:
        expected = harness.call("pushProcessorRequest", url=wire_db.ts_url, search=search, body=_b64(body))
        query = dict(QueryParams(search.removeprefix("?")))
        actual = wire_db.run(processor.process(REGISTRY, query, body)).to_json()
        assert as_js(actual) == as_js(expected)
    assert as_js(wire_db.dump(wire_db.py_engine)) == as_js(wire_db.dump(wire_db.ts_engine))
