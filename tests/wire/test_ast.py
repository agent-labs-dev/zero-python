"""The AST models accept what upstream's `astSchema` accepts, and normalize as `normalizeAST` does."""

import re
from typing import Any

import pytest
from hypothesis import example, given
from pydantic import ValidationError

from tests.harness import Harness, HarnessError
from tests.strategies import as_js, asts, corrupted
from zero_server.protocol.ast import AST, map_ast, map_condition, normalize_ast
from zero_server.schema import DestNames, NameMapper


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


def _eq(left: dict[str, Any]) -> dict[str, Any]:
    return {"type": "simple", "op": "=", "left": left, "right": {"type": "literal", "value": 1}}


@given(asts() | asts(static=True) | asts(few=True) | asts(few=True))
@example(
    {
        "table": "t",
        "where": {
            "type": "and",
            "conditions": [_eq({"type": "column", "name": n}) for n in ("b\ud800", "a")]
            + [_eq({"type": "literal", "value": "a"})],
        },
    }
)
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


def _mapper(tables: dict[str, Any]) -> NameMapper:
    """The mapper the harness builds from the same `{table: {tableName, columns}}`."""
    return NameMapper(
        {
            name: DestNames(table_name=spec["tableName"], columns=spec["columns"], all_columns_same=False)
            for name, spec in tables.items()
        }
    )


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
    assert as_js(map_ast(AST.from_json(value), _mapper(tables)).to_json()) == as_js(expected)
    # Without the mapping for the query's own table, both refuse it alike.
    del tables[value["table"]]
    with pytest.raises(HarnessError) as upstream:
        harness.call("mapAST", ast=value, tables=tables)
    message = str(upstream.value).split("\n")[0].split(": ", 1)[1]
    with pytest.raises(Exception, match=f"^{re.escape(message)}$"):
        map_ast(AST.from_json(value), _mapper(tables))


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


@given(asts())
def test_maps_a_condition_like_upstream(harness: Harness, value: Any) -> None:
    if "where" not in value:
        return
    tables: dict[str, Any] = {}
    _mapping_for(value, tables)
    expected = harness.call("mapCondition", condition=value["where"], table=value["table"], tables=tables)
    ast = AST.from_json(value)
    assert ast.where is not None
    actual = map_condition(ast.where, value["table"], _mapper(tables))
    assert as_js(actual.to_json()) == as_js(expected)


def _col(name: str, op: str = "=") -> dict[str, Any]:
    return {
        "type": "simple",
        "op": op,
        "left": {"type": "column", "name": name},
        "right": {"type": "literal", "value": 1},
    }


def _exists(alias: str | None, **flags: Any) -> dict[str, Any]:
    subquery: dict[str, Any] = {"table": "u"} if alias is None else {"table": "u", "alias": alias}
    return {
        "type": "correlatedSubquery",
        "op": "EXISTS",
        "related": {"correlation": {"parentField": ["id"], "childField": ["id"]}, "subquery": subquery},
        **flags,
    }


def _static(field: str) -> dict[str, Any]:
    return {
        "type": "simple",
        "op": "=",
        "left": {"type": "static", "anchor": "authData", "field": field},
        "right": {"type": "literal", "value": 1},
    }


def _or(*conditions: Any) -> dict[str, Any]:
    return {"type": "or", "conditions": list(conditions)}


# Conditions whose order turns on a later field of the comparison.
PINNED_WHERES = [
    {"type": "and", "conditions": [_or(_col("a"), _col("c")), _or(_col("a"), _col("b"))]},
    {"type": "and", "conditions": [_or(_col("a"), _col("b"), _col("c")), _or(_col("a"), _col("b"))]},
    {"type": "and", "conditions": [_or(_col("a"), _col("b")), _exists("s"), _or(_col("a"), _col("c"))]},
    {"type": "and", "conditions": [_exists("s", flip=True), _exists("s", flip=False), _exists("s")]},
    {"type": "and", "conditions": [_exists("s", scalar=True), _exists("s", scalar=False)]},
    {"type": "and", "conditions": [_col("a", "<"), _col("a", "=")]},
    {"type": "and", "conditions": [_static("x"), _static("y")]},
    {"type": "and", "conditions": [_exists(None), _exists("s")]},
]


@pytest.mark.parametrize("where", PINNED_WHERES)
def test_normalizes_pinned_conditions_like_upstream(harness: Harness, where: Any) -> None:
    ast = {"table": "t", "where": where}
    try:
        expected: Any = harness.call("normalizeAST", ast=ast)
    except HarnessError as error:
        message = str(error).split("\n")[0].split(": ", 1)[1]
        with pytest.raises(Exception, match=re.escape(message)):
            normalize_ast(AST.from_json(ast))
        return
    assert as_js(normalize_ast(AST.from_json(ast)).to_json()) == as_js(expected)
