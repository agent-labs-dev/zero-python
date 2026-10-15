"""`js_string` and `js_stringify` stringify as JavaScript's `String()` and `JSON.stringify` do."""

from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from tests.harness import Harness
from tests.strategies import json_values, scalars
from zero_server.shared.js import js_sort, js_string, js_stringify


@given(
    st.none() | scalars | st.lists(st.none() | scalars, max_size=3) | st.floats(allow_nan=False, allow_infinity=False)
)
def test_matches_string(harness: Harness, value: Any) -> None:
    assert js_string(value) == harness.call("jsString", value=value)


@given(json_values)
def test_matches_json_stringify(harness: Harness, value: Any) -> None:
    assert js_stringify(value) == harness.call("jsonStringify", value=value)


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
