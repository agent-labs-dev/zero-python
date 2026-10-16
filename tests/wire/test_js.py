"""`js_string` and `js_stringify` stringify as JavaScript's `String()` and `JSON.stringify` do."""

from typing import Any

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from tests.harness import Harness
from tests.strategies import json_values, numbers, scalars
from zero_server.protocol.application_error import ApplicationError, is_application_error
from zero_server.shared.error import error_name
from zero_server.shared.js import js_number, js_sort, js_string, js_stringify, js_stringify_bytes


@given(
    st.none() | scalars | st.lists(st.none() | scalars, max_size=3) | st.floats(allow_nan=False, allow_infinity=False)
)
def test_matches_string(harness: Harness, value: Any) -> None:
    assert js_string(value) == harness.call("jsString", value=value)


@pytest.mark.parametrize("text", ["Infinity", "-Infinity", "NaN", "1e400", "-0"])
def test_writes_numbers_json_cannot_carry_as_string_does(harness: Harness, text: str) -> None:
    assert js_number(float(text)) == harness.call("numberString", text=text)


# Every code point, and often a surrogate, lone or paired: JSON.parse makes those of a `\\ud800` escape.
any_text = st.lists(
    st.sampled_from(["\ud800", "\udfff", "\ud83d", "\ude00"]) | st.characters(codec=None, exclude_categories=()),
    max_size=6,
).map("".join)
json_with_any_text = st.recursive(
    st.none() | any_text | numbers | st.booleans(),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(any_text, inner, max_size=3),
    max_leaves=6,
)


# The cases where orjson's text could differ from V8's: array-index keys, keys that only end in a digit,
# floats, integers past 2**53 and escaped quotes.
@example({"b": 1, "1": [2], "0": {"10": 3}})
@example({"id1": 1, "s": "1.5e3"})
@example([1.0, -0.0, 1e20, 1e-6, 2**53])
@example({"x\\": '"'})
@given(json_with_any_text | json_values)
def test_matches_json_stringify(harness: Harness, value: Any) -> None:
    expected = harness.call("jsonStringify", value=value)
    assert js_stringify(value) == expected
    assert js_stringify_bytes(value) == expected.encode()


@example(list(range(70, 0, -1)))
@given(st.lists(st.integers(0, 70), max_size=80))
def test_sort_of_a_long_array_matches_v8(harness: Harness, items: list[int]) -> None:
    consistent = {f"{a},{b}": (a > b) - (a < b) for a in set(items) for b in set(items)}
    assert js_sort(items, lambda a, b: consistent[f"{a},{b}"]) == harness.call(
        "sortWith", items=items, table=consistent
    )


@given(st.data())
def test_sort_makes_v8s_comparisons(harness: Harness, data: st.DataObject) -> None:
    items = data.draw(st.lists(st.integers(0, 70), max_size=80))
    table = {f"{a},{b}": data.draw(st.sampled_from([-1, 0, 1])) for a in set(items) for b in set(items)}
    expected = harness.call("sortWith", items=items, table=table)
    if len(items) < 64:
        assert js_sort(items, lambda a, b: table[f"{a},{b}"]) == expected
    consistent = {f"{a},{b}": (a > b) - (a < b) for a in set(items) for b in set(items)}
    assert js_sort(items, lambda a, b: consistent[f"{a},{b}"]) == harness.call(
        "sortWith", items=items, table=consistent
    )


@pytest.mark.parametrize("details", [None, {"a": [1]}, 0])
def test_application_errors_describe_themselves_as_upstreams_do(harness: Harness, details: Any) -> None:
    error = ApplicationError("m", details=details)
    expected = harness.call("applicationError", message="m", details=details)
    assert {
        "kind": error.kind,
        "name": error_name(error),
        "message": error.message,
        "details": error.details,
        "isApplicationError": is_application_error(error),
        "plainIsApplicationError": is_application_error(Exception("m")),
    } == expected
