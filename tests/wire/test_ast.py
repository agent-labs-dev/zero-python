"""The AST models accept what upstream's `astSchema` accepts, and normalize as `normalizeAST` does."""

from typing import Any

import pytest
from hypothesis import given
from pydantic import ValidationError

from tests.harness import Harness, HarnessError
from tests.strategies import as_js, asts, corrupted
from zero_python.protocol.ast import AST, map_ast, normalize_ast


def python_accepts(value: Any) -> bool:
    try:
        AST.from_json(value)
    except ValidationError:
        return False
    return True


@given(corrupted(asts(static=True)))
def test_accepts_what_upstream_accepts(harness: Harness, value: Any) -> None:
    assert python_accepts(value) == harness.call("accepts", schema="ast", value=value)


@given(asts())
def test_round_trips(value: Any) -> None:
    assert AST.from_json(value).to_json() == value


@given(asts() | asts(static=True))
def test_normalizes_like_upstream(harness: Harness, value: Any) -> None:
    try:
        expected: Any = harness.call("normalizeAST", ast=value)
    except HarnessError as e:
        expected = e
    if isinstance(expected, HarnessError):
        assert "Static parameters should be resolved" in str(expected)
        with pytest.raises(ValueError, match="Static parameters should be resolved before normalization"):
            normalize_ast(AST.from_json(value))
    else:
        assert as_js(normalize_ast(AST.from_json(value)).to_json()) == as_js(expected)


class DictMapper:
    def __init__(self, tables: dict[str, Any]) -> None:
        self.tables = tables

    def table_name(self, table: str) -> str:
        return self.tables[table]["tableName"]

    def column_name(self, table: str, column: str) -> str:
        return self.tables[table]["columns"][column]


def _mapping_for(ast: Any, tables: dict[str, Any]) -> None:
    """Every table and column `ast` names, mapped to an upper-cased server name."""

    def table(name: str) -> dict[str, Any]:
        return tables.setdefault(name, {"tableName": f"S_{name}", "columns": {}})

    def column(t: str, c: str) -> None:
        table(t)["columns"][c] = f"S_{c}"

    def condition(t: str, cond: Any) -> None:
        match cond["type"]:
            case "simple":
                if cond["left"]["type"] == "column":
                    column(t, cond["left"]["name"])
            case "correlatedSubquery":
                related(t, cond["related"])
            case _:
                for c in cond["conditions"]:
                    condition(t, c)

    def related(t: str, r: Any) -> None:
        for c in r["correlation"]["parentField"]:
            column(t, c)
        for c in r["correlation"]["childField"]:
            column(r["subquery"]["table"], c)
        walk(r["subquery"])

    def walk(a: Any) -> None:
        t = a["table"]
        table(t)
        if "where" in a:
            condition(t, a["where"])
        for r in a.get("related", []):
            related(t, r)
        for c, _ in a.get("orderBy", []):
            column(t, c)
        for c in a.get("start", {}).get("row", {}):
            column(t, c)

    walk(ast)


@given(asts(static=True))
def test_maps_names_like_upstream(harness: Harness, value: Any) -> None:
    tables: dict[str, Any] = {}
    _mapping_for(value, tables)
    expected = harness.call("mapAST", ast=value, tables=tables)
    assert as_js(map_ast(AST.from_json(value), DictMapper(tables)).to_json()) == as_js(expected)


def test_normalizes_mixed_conditions_like_upstream(harness: Harness) -> None:
    def eq(column: str) -> dict[str, Any]:
        return {
            "type": "simple",
            "op": "=",
            "left": {"type": "column", "name": column},
            "right": {"type": "literal", "value": 1},
        }

    exists = {
        "type": "correlatedSubquery",
        "op": "EXISTS",
        "related": {
            "correlation": {"parentField": ["id"], "childField": ["id"]},
            "subquery": {"table": "u", "alias": "s"},
        },
    }
    compound = {"type": "or", "conditions": [eq("x"), eq("y")]}
    for order in ([compound, exists, eq("a")], [exists, compound, eq("a")], [compound, eq("a"), exists]):
        ast = {"table": "t", "where": {"type": "and", "conditions": order}}
        assert normalize_ast(AST.from_json(ast)).to_json() == harness.call("normalizeAST", ast=ast)
