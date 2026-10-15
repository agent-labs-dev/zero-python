"""The SQL tier: the same ZQL run by upstream's `ZQLDatabase.run` and by `SQLAlchemyDatabase.run`, against
the same Postgres. Compares result rows, never SQL text."""

from typing import Any

import pytest
from hypothesis import given

from tests.harness import Harness
from tests.strategies import as_js
from tests.wire.conftest import ADAPTERS, SqlDB
from tests.wire.programs import apply, programs
from zero_server.query.builder import create_builder
from zero_server.schema import Schema, load_schema


@pytest.fixture(scope="session")
def schema(harness: Harness) -> Schema:
    return load_schema(harness.call("schema"))


def run_python(sql_db: SqlDB, schema: Schema, program: dict[str, Any], adapter: str = "sqlalchemy") -> dict[str, Any]:
    db = sql_db.py[adapter](schema)
    try:
        query = apply(create_builder(schema).table(program["table"]), program["ops"])
        return {"rows": sql_db.run(db.run(query))}
    except Exception as error:
        return {"error": str(error)}


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_queries_return_upstreams_rows(harness: Harness, sql_db: SqlDB, schema: Schema, adapter: str) -> None:
    @given(programs(schema))
    def check(program: dict[str, Any]) -> None:
        expected = harness.call("runQuery", url=sql_db.url, program=program)
        assert as_js(run_python(sql_db, schema, program, adapter)) == as_js(expected)

    check()


def _q(table: str, *ops: list[Any]) -> dict[str, Any]:
    return {"table": table, "ops": list(ops)}


exists = ["exists", "comments", {"ops": [["where", "body", "a"]]}, None]
# Paths a random draw can miss: each column conversion, each operator family, and each refusal.
PINNED = [
    _q("audit"),
    _q("audit", ["where", "at", ">", 0], ["orderBy", "day", "desc"], ["start", {"day": None, "id": "a2"}, True]),
    _q("audit", ["where", "mood", "IS NOT", None], ["where", "tags", "IS", None]),
    _q("audit", ["start", {"at": 1, "id": "a1"}, False], ["orderBy", "at", "asc"]),
    _q("counter", ["where", "n", "<", 10]),
    _q("counter"),
    _q("issue", ["whereFn", ["not", exists]], ["orderBy", "priority", "desc"], ["limit", 3]),
    _q("issue", ["whereFn", ["cmpLit", [1, 2], "IN", [1]]]),
    _q("issue", ["whereFn", ["cmpLit", 1, "IN", [1, 2]]]),
    _q("issue", ["whereFn", ["cmpLit", "a", "LIKE", "a%"]]),
    _q("issue", ["whereFn", ["cmpLit", 1, "=", {"$param": ["authData", "sub"]}]]),
    _q("issue", ["whereFn", ["cmpLit", 1, "IN", []]]),
    _q("issue", ["whereFn", ["cmpLit", None, "IN", []]]),
    _q("issue", ["whereFn", ["cmpLit", [], "IN", "a"]]),
    _q("issue", ["whereFn", ["cmpLit", True, "=", True]]),
    _q("issue", ["where", "title", "IN", []], ["where", "open", "IS", True]),
    _q("issue", ["related", "labels", {"ops": [["where", "name", "!=", "a"], ["start", {"id": "l1"}, None]]}]),
    _q("issue", ["related", "owner", None], ["one"]),
    _q("user", ["where", "id", "nobody"], ["one"]),
    _q("issue", ["limit", 0]),
    # The second null reuses the first's placeholder, which Postgres has already typed as text.
    _q("comment", ["where", "authorID", None], ["where", "authorID", "IN", None]),
    # Two empty lists are two objects, so two placeholders: one binds "", the other "[]".
    _q("comment", ["where", "authorID", []], ["where", "authorID", "IN", []]),
    # Literals upstream's builder takes though its wire schema would not.
    _q("issue", ["where", "meta", {"a": 1}]),
    _q("issue", ["where", "title", {"a": 1}], ["where", "title", "IN", [{"a": 1}]]),
    _q("issue", ["where", "title", "IN", [None, "a"]]),
    _q("issue", ["where", "title", "IN", [["a"], "b"]]),
    _q("audit", ["where", "tags", "IN", [["a"]]]),
    _q("issue", ["whereFn", ["cmpLit", 1, "IN", [None, 1]]]),
    _q("issue", ["whereFn", ["cmpLit", "a", "IN", [{"a": 1}]]]),
    _q("issue", ["whereFn", ["cmpLit", {"a": 1}, "=", {"a": 1}]]),
    # Builder arguments upstream refuses late or never.
    _q("issue", ["limit", None]),
    _q("issue", ["limit", "3"]),
    _q("issue", ["limit", "-1"]),
    _q("issue", ["limit", True]),
    _q("issue", ["where", 1, "a"]),
    _q("issue", ["start", ["a"], None]),
    _q("issue", ["whereExists", "owner", None, {"flip": "yes"}]),
    _q("issue", ["where", "title", "==", "a"]),
    _q("issue", ["whereFn", ["cmpLit", 1, "<>", 1]]),
    _q("issue", ["whereFn", ["not", ["cmp", "title", "==", "a"]]]),
    # An error building the SQL beats one formatting it, wherever each is; between two errors formatting
    # it, the first in the text wins.
    _q("issue", ["where", "title", "==", "a"], ["whereFn", ["cmpLit", {"a": 1}, "=", 1]]),
    _q("issue", ["whereFn", ["cmpLit", 1, "IN", [None]]], ["where", "title", "==", "a"]),
]


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("program", PINNED)
def test_pinned_queries_return_upstreams_rows(
    harness: Harness, sql_db: SqlDB, schema: Schema, adapter: str, program: dict[str, Any]
) -> None:
    expected = harness.call("runQuery", url=sql_db.url, program=program)
    assert as_js(run_python(sql_db, schema, program, adapter)) == as_js(expected)
