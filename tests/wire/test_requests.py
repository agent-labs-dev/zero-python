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
from zero_server.protocol.wire import parse_wire

transform_request: TypeAdapter[Any] = TypeAdapter(TransformRequestMessage)
cleanup_results_arg: TypeAdapter[Any] = TypeAdapter(CleanupResultsArg)

CASES: dict[str, tuple[st.SearchStrategy[Any], Callable[[Any], object]]] = {
    "pushBody": (push_bodies(), PushBody.from_json),
    "transformRequest": (transform_requests(), lambda v: parse_wire(transform_request, v)),
    "cleanupResultsArg": (cleanup_args(), lambda v: parse_wire(cleanup_results_arg, v)),
    "mutateParams": (mutate_params(), MutateParams.from_json),
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
