"""Python accepts exactly the requests upstream's parsers accept, in the mode each parses in."""

import json
from collections.abc import Callable
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from tests.harness import Harness
from tests.strategies import cleanup_args, corrupted, mutate_params, push_bodies, transform_requests
from zero_server.protocol.custom_queries import TransformRequestMessage
from zero_server.protocol.mutate_server import MutateParams
from zero_server.protocol.mutation import CleanupResultsArg
from zero_server.protocol.push import PushBody
from zero_server.protocol.schemas import (
    cleanup_results_arg_schema,
    mutate_params_schema,
    push_body_schema,
    transform_request_message_schema,
)
from zero_server.protocol.wire import parse_wire
from zero_server.shared import valita

transform_request: TypeAdapter[Any] = TypeAdapter(TransformRequestMessage)
cleanup_results_arg: TypeAdapter[Any] = TypeAdapter(CleanupResultsArg)

CASES: dict[str, tuple[st.SearchStrategy[Any], Callable[[Any], object]]] = {
    "pushBody": (push_bodies(), PushBody.from_json),
    "transformRequest": (transform_requests(), lambda v: parse_wire(transform_request, v)),
    "cleanupResultsArg": (cleanup_args(), lambda v: parse_wire(cleanup_results_arg, v)),
    "mutateParams": (mutate_params(), MutateParams.from_json),
}


# Each schema as upstream parses it: the schema and the mode its handler passes.
VALITA: dict[str, tuple[valita.Type, valita.Mode | None]] = {
    "pushBody": (push_body_schema, "passthrough"),
    "transformRequest": (transform_request_message_schema, "strip"),
    "cleanupResultsArg": (cleanup_results_arg_schema, None),
    "mutateParams": (mutate_params_schema, "passthrough"),
}


def accepts(parse: Callable[[Any], object], value: Any) -> bool:
    try:
        parse(value)
    except ValidationError:
        return False
    return True


@pytest.mark.parametrize("encoded", [False, True], ids=["json", "bytes"])
@pytest.mark.parametrize("schema", sorted(CASES))
def test_accepts_what_upstream_accepts(harness: Harness, schema: str, encoded: bool) -> None:
    """As decoded JSON, and as the raw body the handlers parse from bytes without checking its JSON values."""
    strategy, parse = CASES[schema]

    @given(corrupted(strategy))
    def check(value: Any) -> None:
        given_value = json.dumps(value).encode() if encoded else value
        assert accepts(parse, given_value) == harness.call("accepts", schema=schema, value=value), value

    check()


@pytest.mark.parametrize("schema", sorted(CASES))
def test_refuses_with_upstreams_message(harness: Harness, schema: str) -> None:
    strategy, _parse = CASES[schema]
    type_, mode = VALITA[schema]

    @given(corrupted(strategy))
    def check(value: Any) -> None:
        assert valita.parse_error(value, type_, mode) == harness.call("refusal", schema=schema, value=value), value

    check()


def _push(*mutations: Any) -> dict[str, Any]:
    return {"clientGroupID": "g", "mutations": list(mutations), "pushVersion": 1, "timestamp": 1, "requestID": "r"}


def _crud(*ops: Any) -> dict[str, Any]:
    return {
        "type": "crud",
        "id": 1,
        "clientID": "c",
        "name": "_zero_crud",
        "args": [{"ops": list(ops)}],
        "timestamp": 1,
    }


_delete = {"op": "delete", "tableName": "t", "primaryKey": ["id"], "value": {"id": "a"}}
# Messages a random draw seldom reaches: lists of types and keys, ranges of lengths, and unions.
PINNED_REFUSALS: list[tuple[str, Any]] = [
    ("pushBody", _push(_crud({**_delete, "value": {"id": None}}))),
    ("pushBody", _push(_crud({**_delete, "primaryKey": []}))),
    ("pushBody", _push(_crud({**_delete, "op": "nope"}))),
    ("pushBody", _push(_crud({k: v for k, v in _delete.items() if k != "op"}))),
    ("pushBody", _push({**_crud(), "args": []})),
    ("pushBody", _push({**_crud(), "type": 1})),
    ("pushBody", _push(_crud(_delete), "x")),
    # Parsed in strip mode, so keys it does not know are dropped rather than refused.
    ("transformRequest", ["transform", [{"id": "1", "name": "n", "args": [], "b": 1, "a": 2, "0": 3}]]),
    ("transformRequest", ["transform", [{"id": "1", "name": "n", "args": [], "x": 1}]]),
    ("transformRequest", ["transform"]),
    ("transformRequest", {"0": "transform"}),
    ("cleanupResultsArg", {"clientGroupID": "g"}),
    ("cleanupResultsArg", {"type": "bulk", "clientGroupID": "g", "clientIDs": []}),
    ("cleanupResultsArg", {"type": "single", "clientGroupID": "g", "clientID": 1, "upToMutationID": 1}),
    ("cleanupResultsArg", {"clientGroupID": "g", "clientID": "c", "upToMutationID": 1, "x": 1}),
    ("cleanupResultsArg", {"clientGroupID": "g", "clientID": "c", "upToMutationID": 1, "b": 1, "a": 2, "0": 3}),
    ("cleanupResultsArg", {"clientGroupID": "g", "clientID": "c", "upToMutationID": "1"}),
    ("cleanupResultsArg", "x"),
    ("mutateParams", {"schema": 1, "appID": 2}),
]


@pytest.mark.parametrize(("schema", "value"), PINNED_REFUSALS)
def test_pinned_refusals_match_upstream(harness: Harness, schema: str, value: Any) -> None:
    type_, mode = VALITA[schema]
    assert valita.parse_error(value, type_, mode) == harness.call("refusal", schema=schema, value=value)
