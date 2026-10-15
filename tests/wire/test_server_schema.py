"""A schema the database does not match is refused on the first transaction, in upstream's words."""

import copy
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.harness import Harness
from tests.strategies import as_js
from tests.wire.conftest import ADAPTERS, WireDB
from tests.wire.mutators import handler
from zero_server.schema import load_schema
from zero_server.server.process_mutations import handle_mutate_request

QUERY = {"schema": "zero_0", "appID": "zero"}
BODY = {
    "clientGroupID": "cg1",
    "mutations": [{"type": "custom", "id": 1, "clientID": "c1", "name": "item.put", "args": [{}], "timestamp": 1}],
    "pushVersion": 1,
    "timestamp": 1,
    "requestID": "r",
}


@st.composite
def perturbed(draw: st.DrawFn, base: dict[str, Any]) -> dict[str, Any]:
    """The wire schema with a few columns retyped or renamed, or a table renamed."""
    schema = copy.deepcopy(base)
    for _ in range(draw(st.integers(0, 3))):
        table = draw(st.sampled_from(sorted(schema["tables"])))
        spec = schema["tables"][table]
        match draw(st.sampled_from(["type", "column", "table"])):
            case "type":
                column = draw(st.sampled_from(sorted(spec["columns"])))
                spec["columns"][column]["type"] = draw(st.sampled_from(["string", "number", "boolean", "json"]))
            case "column":
                column = draw(st.sampled_from(sorted(spec["columns"])))
                spec["columns"][column]["serverName"] = draw(st.sampled_from(["missing_column", ""]))
            case _:
                spec["serverName"] = draw(st.sampled_from(["missing_table", "other.things", ""]))
    return schema


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_incompatible_schemas_fail_like_upstream(harness: Harness, wire_db: WireDB, adapter: str) -> None:
    base = harness.call("wireSchema")

    @given(perturbed(base))
    def check(schema: dict[str, Any]) -> None:
        wire_db.reset()
        expected = harness.call("mutateWithSchema", url=wire_db.ts_url, schema=schema, query=QUERY, body=BODY)
        py_db = wire_db.py[adapter](load_schema(schema))
        actual = wire_db.run(
            handle_mutate_request(db_provider=py_db, handler=handler, query=QUERY, body=BODY, user_id=None)
        )
        assert as_js(actual.to_json()) == as_js(expected)

    check()
