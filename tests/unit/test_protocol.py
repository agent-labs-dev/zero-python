import pytest

from zero_python.protocol.application_error import (
    ApplicationError,
    is_application_error,
    wrap_with_application_error,
)
from zero_python.protocol.ast import (
    AST,
    ColumnReference,
    Conjunction,
    CorrelatedSubquery,
    Correlation,
    LiteralReference,
    SimpleCondition,
    map_condition,
    normalize_ast,
)
from zero_python.protocol.query_server import QuerySuccess
from zero_python.shared.asserts import AssertionFailedError


def test_application_error() -> None:
    cause = ValueError("why")
    error = ApplicationError("nope", details={"a": 1}, cause=cause)
    assert error.kind == "Application"
    assert error.__cause__ is cause
    assert is_application_error(error)
    assert not is_application_error(cause)
    assert wrap_with_application_error(error) is error


def test_wrapping_keeps_message_details_and_cause() -> None:
    cause = TypeError("bad")
    wrapped = wrap_with_application_error(cause)
    assert (wrapped.message, wrapped.details, wrapped.__cause__) == ("bad", {"name": "TypeError"}, cause)
    assert wrap_with_application_error("text").__cause__ is None


def test_user_id_is_reported_only_when_set() -> None:
    assert QuerySuccess(queries=[]).to_json() == {"kind": "QueryResponse", "queries": []}
    assert QuerySuccess(queries=[], user_id=None).to_json() == {"kind": "QueryResponse", "queries": [], "userID": None}


def _eq(column: str, value: int) -> SimpleCondition:
    return SimpleCondition(op="=", left=ColumnReference(name=column), right=LiteralReference(value=value))


def test_normalize_orders_sibling_conjunctions() -> None:
    ast = AST.from_json(
        {
            "table": "t",
            "where": {
                "type": "or",
                "conditions": [
                    {"type": "and", "conditions": [_eq("b", 1).to_json(), _eq("c", 1).to_json()]},
                    {"type": "and", "conditions": [_eq("a", 1).to_json(), _eq("c", 1).to_json()]},
                    {
                        "type": "and",
                        "conditions": [_eq("a", 1).to_json(), _eq("c", 1).to_json(), _eq("d", 1).to_json()],
                    },
                ],
            },
        }
    )
    where = normalize_ast(ast).where
    assert where is not None
    assert [[c.left.name for c in conj.conditions] for conj in where.conditions] == [  # ty: ignore[unresolved-attribute]
        ["a", "c"],
        ["a", "c", "d"],
        ["b", "c"],
    ]
    assert isinstance(where.conditions[0], Conjunction)  # ty: ignore[unresolved-attribute]


def test_normalize_requires_subquery_aliases() -> None:
    related = CorrelatedSubquery(
        correlation=Correlation(parent_field=("id",), child_field=("id",)), subquery=AST(table="u")
    )
    with pytest.raises(AssertionFailedError):
        normalize_ast(AST(table="t", related=(related, related)))


class Upper:
    def table_name(self, table: str) -> str:
        return table.upper()

    def column_name(self, table: str, column: str) -> str:
        return column.upper()


def test_map_condition() -> None:
    assert map_condition(_eq("a", 1), "t", Upper()) == _eq("A", 1)


def test_normalize_refuses_unresolved_parameters() -> None:
    param = {"type": "simple", "op": "=", "left": {"type": "static", "anchor": "authData", "field": "sub"}}
    ast = {
        "table": "t",
        "where": {
            "type": "and",
            "conditions": [
                {**param, "right": {"type": "literal", "value": 1}},
                {**param, "right": {"type": "literal", "value": 2}},
            ],
        },
    }
    with pytest.raises(ValueError, match="Static parameters"):
        normalize_ast(AST.from_json(ast))


def test_normalize_orders_and_before_or() -> None:
    pair = [_eq("a", 1).to_json(), _eq("b", 1).to_json()]
    ast = {
        "table": "t",
        "where": {
            "type": "or",
            "conditions": [
                {"type": "and", "conditions": [{"type": "or", "conditions": pair}, *pair]},
                {
                    "type": "and",
                    "conditions": [{"type": "or", "conditions": pair}, {"type": "and", "conditions": pair}],
                },
            ],
        },
    }
    assert normalize_ast(AST.from_json(ast)).where is not None


def test_condition_comparator_edges() -> None:
    from zero_python.protocol.ast import CorrelatedSubqueryCondition, Disjunction, _cmp_condition

    related = CorrelatedSubquery(
        correlation=Correlation(parent_field=("id",), child_field=("id",)), subquery=AST(table="u", alias="s")
    )
    plain = CorrelatedSubqueryCondition(related=related, op="EXISTS")
    flipped = CorrelatedSubqueryCondition(related=related, op="EXISTS", flip=True, scalar=False)
    assert _cmp_condition(plain, flipped) < 0
    assert _cmp_condition(flipped, flipped.model_copy(update={"scalar": True})) < 0
    pair = (_eq("a", 1), _eq("b", 1))
    assert _cmp_condition(Conjunction(conditions=pair), Disjunction(conditions=pair)) < 0
