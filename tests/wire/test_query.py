"""The wire tier for `/query`: the same named queries through upstream's handler and Python's."""

import asyncio
import re
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.harness import Harness
from tests.strategies import as_js, json_values
from tests.wire.errors import SHAPES
from tests.wire.programs import RETURNS, apply, make_handler, programs
from zero_server import Query, create_builder, server_ast
from zero_server.schema import Schema, load_schema
from zero_server.server.process_queries import handle_query_request, query_response_bytes
from zero_server.shared.js import js_stringify


@pytest.fixture(scope="session")
def schema(harness: Harness) -> Schema:
    return load_schema(harness.call("schema"))


def _comparable(response: dict[str, Any]) -> str:
    """A response as JSON text, key order included."""
    return js_stringify(response)


NAMES = [
    "prog", "prog", "prog", "nested.prog", "nested|prog", "validated", "parseError", "appError", "missing",
    "returns", "raw", "appDetails",
    "returnsNothing", "returnsString", "returnsNumber", "returnsObject", "wrapped",
]  # fmt: skip
RETURNED = st.sampled_from(["undefined", *RETURNS]).map(lambda r: [r])


def test_queries_match_upstream(harness: Harness, schema: Schema) -> None:
    program_args = programs(schema).map(lambda p: [p])
    # Only the validated query sees junk arguments: the others are user code, which fails on junk in
    # whatever way its own language does.
    args_for = {
        name: st.one_of(program_args, st.just([]), st.just([1]), st.just([None]))
        if name == "validated"
        else RETURNED
        if name in ("returns", "raw")
        else st.lists(json_values, max_size=1)
        if name == "appDetails"
        else program_args
        for name in NAMES
    }
    request = st.sampled_from(NAMES).flatmap(
        lambda name: st.builds(
            lambda i, args: {"id": str(i), "name": name, "args": args}, st.integers(0, 99), args_for[name]
        )
    )

    @given(st.lists(request, max_size=3), st.sampled_from([None, "u1"]))
    def check(requests: list[Any], user_id: str | None) -> None:
        body = ["transform", requests]
        expected = harness.call("query", body=body, userID=user_id)
        actual = asyncio.run(
            handle_query_request(handler=make_handler(schema), schema=schema, body=body, user_id=user_id)
        )
        assert _comparable(actual.to_json()) == _comparable(expected)
        # The text the HTTP endpoint sends, joined from each AST's own.
        assert query_response_bytes(actual) == _comparable(expected).encode()

    check()


def _run(harness: Harness, schema: Schema, requests: list[Any]) -> tuple[Any, Any]:
    body = ["transform", requests]
    expected = harness.call("query", body=body, userID="u")

    async def run() -> Any:
        handler = make_handler(schema)
        return await asyncio.wait_for(handle_query_request(handler=handler, schema=schema, body=body, user_id="u"), 5)

    return _comparable(asyncio.run(run()).to_json()), _comparable(expected)


def test_handlers_run_concurrently(harness: Harness, schema: Schema) -> None:
    """`gate.wait` finishes only once `gate.open`, later in the batch, has run: upstream runs them together."""
    requests = [{"id": "1", "name": "gate.wait", "args": []}, {"id": "2", "name": "gate.open", "args": []}]
    actual, expected = _run(harness, schema, requests)
    assert actual == expected


@pytest.mark.parametrize("returned", ["undefined", *RETURNS])
def test_handler_returning_no_query_matches_upstream(harness: Harness, schema: Schema, returned: str) -> None:
    """A primitive fails its own query; an object that is not a query fails the batch."""
    ok = {"id": "0", "name": "prog", "args": [{"table": "issue", "ops": []}]}
    for name in ("raw", "returns"):
        actual, expected = _run(harness, schema, [ok, {"id": "1", "name": name, "args": [returned]}])
        assert actual == expected


def _q(table: str, *ops: list[Any]) -> dict[str, Any]:
    return {"table": table, "ops": list(ops)}


eq = ["cmp", "title", "x"]
exists = ["exists", "comments", None, {"flip": True, "scalar": False}]
# A condition the app builds itself keeps the app's key order upstream, so this one has the order the
# Python model writes.
_raw_eq = {
    "type": "simple",
    "left": {"type": "column", "name": "title"},
    "right": {"type": "literal", "value": "x"},
    "op": "=",
}
_deep: Any = []
for _ in range(299):
    _deep = [_deep]
# Builder paths a random draw can miss: condition simplification, negation, and each refusal.
PINNED = [
    _q("issue", ["orderBy", "title", "sideways"], ["orderBy", "id", "\ufffdasc"]),
    # A literal nested deeper than Pydantic's serializer goes.
    _q("issue", ["where", "meta", _deep]),
    _q("issue", ["whereRaw", {"type": "and", "conditions": [_raw_eq]}]),
    _q("issue", ["whereRaw", {"type": "and", "conditions": [_raw_eq, {"type": "or", "conditions": []}]}]),
    _q("issue", ["whereRaw", {"type": "or", "conditions": [_raw_eq, {"type": "and", "conditions": []}]}]),
    _q(
        "issue", ["whereRaw", {"type": "or", "conditions": [_raw_eq, {"type": "or", "conditions": [_raw_eq, _raw_eq]}]}]
    ),
    _q("issue", ["whereFn", ["and", [eq]]], ["whereFn", ["or", [eq]]]),
    _q("issue", ["whereFn", ["or", [eq, ["and", []]]]]),
    _q("issue", ["whereFn", ["and", [eq, ["or", []]]]]),
    _q("issue", ["whereFn", ["or", [["or", []], eq]]]),
    _q("issue", ["whereFn", ["and", [["and", [eq, eq]], ["or", []]]]]),
    _q("issue", ["whereFn", ["not", ["and", [eq, exists]]]]),
    _q("issue", ["whereFn", ["not", ["or", [eq, ["not", exists]]]]]),
    _q("issue", ["where", "title", "x"], ["whereFn", ["and", [eq, ["and", [eq, eq]]]]]),
    _q("issue", ["whereFn", ["or", [["or", [eq]], ["and", [["and", []]]]]]]),
    _q("issue", ["whereExists", "labels", {"ops": [["where", "name", "bug"]]}, {"scalar": True}]),
    _q("issue", ["related", "zsubq_x", None]),
    _q("issue", ["limit", -1]),
    _q("issue", ["limit", 1.5]),
    _q("issue", ["related", "labels", {"ops": [["orderBy", "name", "asc"]]}]),
    _q("issue", ["related", "labels", {"ops": [["limit", 2]]}]),
    _q("issue", ["start", {"id": "a"}, True], ["start", {"id": "b"}, None], ["one"]),
    _q("audit", ["related", "nope", None]),
    # Upstream's builder checks none of these, so each reaches the response as given.
    _q("issue", ["where", "meta", {"a": [1, None]}], ["where", "title", "IN", [None, ["a"]]]),
    _q("issue", ["where", "title", "==", "a"]),
    _q("issue", ["whereFn", ["not", ["cmp", "title", "==", "a"]]]),
    _q("issue", ["whereExists", "owner", None, {"flip": "yes", "scalar": 0}]),
    _q("issue", ["where", 1, "a"]),
    _q("issue", ["start", ["a"], None]),
    _q("issue", ["limit", None]),
    _q("issue", ["limit", "-1"]),
    _q("issue", ["orderBy", "title", "\ufffdasc"], ["orderBy", "id", "ASC"]),
    _q("issue", ["orderBy", "title", None], ["orderBy", "id", 1]),
    _q("issue", ["where", "title", {"$param": ["x", "y"]}], ["where", "id", {"$param": "ab"}]),
    _q("issue", ["limit", 2.0], ["where", ["title"], "x"]),
    _q("issue", ["whereFn", ["cmpLit", "a", "=", None]]),
    _q("user", ["related", "manager", {"ops": [["related", "reports", None]]}]),
    _q("issue", ["related", "label", None], ["whereExists", "label", None, None]),
    _q("gadget", ["where", "serial", "IN", []], ["related", "owner", None]),
    # The builder keeps its AST normalized: relationships sorted by alias, conditions by their values.
    _q("issue", ["related", "owner", None], ["related", "comments", None], ["related", "labels", None]),
    _q("issue", ["where", "priority", ">", 2], ["where", "priority", ">", 1], ["where", "priority", ">", 1]),
    _q("issue", ["where", "open", True], ["where", "open", False]),
    _q(
        "issue",
        ["where", "priority", "IN", [1, 3]],
        ["where", "priority", "IN", [1, 2]],
        ["where", "priority", "IN", [1]],
    ),
    _q("issue", *[["where", "meta", v] for v in ([1], "x", 1, True, {"a": 1}, [True], ["a"], [1, 2])]),
    _q(
        "issue",
        *[
            ["where", "title", {"$param": p}]
            for p in (["authData", "b"], ["authData", "a"], ["authData", ["a", "b"]], ["preMutationRow", "a"])
        ],
        ["where", "title", "a"],
    ),
    # Two unknown columns: upstream maps a query's own condition before its relationships, at every level.
    _q("comment", ["where", "nope", None], ["related", "author", {"ops": [["where", "nope", None]]}]),
    _q("issue", ["related", "labels", {"ops": [["where", "nope", None]]}], ["where", "nope", None]),
    _q(
        "issue",
        [
            "whereExists",
            "owner",
            {"ops": [["related", "issues", {"ops": [["where", "nope", None]]}], ["where", "nope", None]]},
            None,
        ],
    ),
    _q("issue", ["where", "nope", "x"]),
    # Object.entries puts an index-like key first, so upstream maps `1` before `nope`.
    _q("comment", ["start", {"nope": None, "1": None}, None]),
]


def test_async_handlers_match_upstream(harness: Harness, schema: Schema) -> None:
    """A coroutine that finishes without suspending, and one that raises."""
    names = ["async.now", "async.throw"]
    body = ["transform", [{"id": str(n), "name": name, "args": []} for n, name in enumerate(names)]]
    expected = harness.call("query", body=body, userID="u")
    actual = asyncio.run(handle_query_request(handler=make_handler(schema), schema=schema, body=body, user_id="u"))
    assert _comparable(actual.to_json()) == _comparable(expected)


# Every kind of step, for the builder to hand back what it built before when a batch builds it again.
REBUILT = [
    _q(
        "issue",
        ["where", "open", True],
        ["where", "priority", ">", 2.5],
        ["whereFn", ["or", [eq, ["cmpLit", "a", "=", {"$param": ["authData", "sub"]}], exists]]],
        ["whereExists", "labels", {"ops": [["where", "name", "!=", "zzz"]]}, {"flip": True}],
        ["whereRaw", {"type": "and", "conditions": [_raw_eq]}],
        ["related", "owner", None],
        ["related", "labels", {"ops": [["where", "name", "bug"]]}],
        ["related", "comments", {"ops": [["orderBy", "id", "desc"], ["limit", 5]]}],
        ["orderBy", "priority", "desc"],
        ["start", {"priority": 1.5, "id": "i1", "meta": [1, {"a": None}]}, True],
        ["limit", 20],
    ),
    _q("issue", ["where", "open", True], ["where", "priority", ">", 2.5], ["one"]),
    _q("issue", ["where", "meta", [1, "x"]], ["where", "title", "IN", ["a", "b"]]),
    # A two-hop `exists` puts `scalar` only on its junction's condition.
    _q("issue", ["whereExists", "labels", None, {"scalar": True}]),
    _q("issue", ["whereExists", "labels", None, {"scalar": False}]),
]


def test_rebuilt_queries_match_upstream(harness: Harness, schema: Schema) -> None:
    """The same programs, three times over in one batch, built by one builder."""
    body = [
        "transform",
        [{"id": str(n), "name": "prog", "args": [program]} for n, program in enumerate(REBUILT * 3)],
    ]
    expected = harness.call("query", body=body, userID="u")
    actual = asyncio.run(handle_query_request(handler=make_handler(schema), schema=schema, body=body, user_id="u"))
    assert _comparable(actual.to_json()) == _comparable(expected)


def test_each_thrown_shape_matches_upstream(harness: Harness, schema: Schema) -> None:
    names = [
        *(f"throw.{shape}" for shape in SHAPES),
        *("parseError", "parseErrorEmptyCause", "parseErrorNoCause"),
        # Called with no arguments at all.
        "constant",
    ]
    body = ["transform", [{"id": str(n), "name": name, "args": []} for n, name in enumerate(names)]]
    expected = harness.call("query", body=body, userID="u")
    actual = asyncio.run(handle_query_request(handler=make_handler(schema), schema=schema, body=body, user_id="u"))
    assert _comparable(actual.to_json()) == _comparable(expected)


@pytest.mark.parametrize("program", PINNED)
def test_pinned_queries_match_upstream(harness: Harness, schema: Schema, program: dict[str, Any]) -> None:
    body = ["transform", [{"id": "1", "name": "prog", "args": [program]}]]
    expected = harness.call("query", body=body, userID="u")
    actual = asyncio.run(handle_query_request(handler=make_handler(schema), schema=schema, body=body, user_id="u"))
    assert _comparable(actual.to_json()) == _comparable(expected)


@given(st.data())
def test_server_ast_is_what_upstream_sends(harness: Harness, schema: Schema, data: st.DataObject) -> None:
    program = data.draw(programs(schema))
    body = ["transform", [{"id": "1", "name": "prog", "args": [program]}]]
    expected: Any = harness.call("query", body=body, userID=None)

    def build() -> Query:
        return apply(create_builder(schema).table(program["table"]), program["ops"])

    if expected.get("kind") == "TransformFailed":
        # Upstream fails the whole batch when the AST does not map.
        with pytest.raises(Exception, match="unknown"):
            server_ast(build(), schema)
    elif "ast" in expected["queries"][0]:
        assert as_js(server_ast(build(), schema).to_json()) == as_js(expected["queries"][0]["ast"])
    else:
        # A builder refusal, reported for that query alone.
        with pytest.raises(Exception, match=re.escape(expected["queries"][0]["message"])):
            build()


def test_raw_handlers_match_upstream(harness: Harness, schema: Schema) -> None:
    """Handlers that return what they like, rather than a defined query's result."""
    zql = create_builder(schema)
    program = {"table": "issue", "ops": [["where", "title", "x"]]}
    raw: dict[str, Any] = {
        "query": lambda args: apply(zql.table(args["table"]), args["ops"]),
        "wrapped": lambda args: {"query": apply(zql.table(args["table"]), args["ops"])},
        "nothing": lambda args: None,
        "string": lambda args: "abc",
        "number": lambda args: 5,
        "object": lambda args: {},
    }
    for name in raw:
        body = ["transform", [{"id": "1", "name": name, "args": [program]}]]
        expected = harness.call("rawQuery", body=body)
        actual = asyncio.run(
            handle_query_request(handler=lambda n, args: raw[n](args), schema=schema, body=body, user_id="u")
        )
        assert _comparable(actual.to_json()) == _comparable(expected), name
