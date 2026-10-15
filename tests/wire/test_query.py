"""The wire tier for `/query`: the same named queries through upstream's handler and Python's."""

import asyncio
import re
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.harness import Harness
from tests.strategies import as_js
from tests.wire.programs import apply, make_handler, programs
from zero_server import Query, create_builder, server_ast
from zero_server.schema import Schema, load_schema
from zero_server.server.process_queries import handle_query_request


@pytest.fixture(scope="session")
def schema(harness: Harness) -> Schema:
    return load_schema(harness.call("schema"))


def _comparable(response: dict[str, Any]) -> Any:
    if response.get("reason") == "parse":
        response = {**response, "message": response["message"].split(":", 1)[0]}
    return as_js(response)


NAMES = ["prog", "prog", "prog", "nested.prog", "nested|prog", "validated", "parseError", "appError", "missing"]


def test_queries_match_upstream(harness: Harness, schema: Schema) -> None:
    handler = make_handler(schema)
    program_args = programs(schema).map(lambda p: [p])
    # Only the validated query sees junk arguments: the others are user code, which fails on junk in
    # whatever way its own language does.
    args_for = {
        name: st.one_of(program_args, st.just([]), st.just([1]), st.just([None]))
        if name == "validated"
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
        actual = asyncio.run(handle_query_request(handler=handler, schema=schema, body=body, user_id=user_id))
        assert _comparable(actual.to_json()) == _comparable(expected)

    check()


def _q(table: str, *ops: list[Any]) -> dict[str, Any]:
    return {"table": table, "ops": list(ops)}


eq = ["cmp", "title", "x"]
exists = ["exists", "comments", None, {"flip": True, "scalar": False}]
# Builder paths a random draw can miss: condition simplification, negation, and each refusal.
PINNED = [
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
    _q("user", ["related", "manager", {"ops": [["related", "reports", None]]}]),
    _q("issue", ["related", "label", None], ["whereExists", "label", None, None]),
    _q("gadget", ["where", "serial", "IN", []], ["related", "owner", None]),
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
]


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
