import dataclasses
from enum import StrEnum
from typing import Any

import pytest
from pydantic import ValidationError

from zero_server.protocol.application_error import (
    ApplicationError,
    is_application_error,
    wrap_with_application_error,
)
from zero_server.protocol.ast import (
    AST,
    ColumnReference,
    Conjunction,
    CorrelatedSubquery,
    Correlation,
    LiteralReference,
    SimpleCondition,
    normalize_ast,
)
from zero_server.protocol.custom_queries import AppErroredQuery
from zero_server.protocol.mutate_server import MutateSuccess
from zero_server.protocol.mutation import CustomMutation, MutationAppError, MutationOk, MutationResponse
from zero_server.protocol.mutation_id import MutationID
from zero_server.protocol.query_server import QuerySuccess
from zero_server.protocol.wire import WireModel
from zero_server.shared.asserts import AssertionFailedError


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


def test_user_id_is_reported_only_when_set() -> None:
    assert QuerySuccess(queries=[]).to_json() == {"kind": "QueryResponse", "queries": []}
    assert QuerySuccess(queries=[], user_id=None).to_json() == {"kind": "QueryResponse", "queries": [], "userID": None}
    assert QuerySuccess(queries=[]).user_id is None
    assert QuerySuccess(queries=[], user_id="u").user_id == "u"


@pytest.mark.parametrize("user", [{}, {"userID": None}, {"userID": "u1"}])
@pytest.mark.parametrize("mutation_id", [1, 2.5])
@pytest.mark.parametrize(
    "result", [MutationOk(), MutationOk(data={"a": [1]}), MutationAppError(message="no"), MutationAppError()]
)
def test_mutate_response_serializes_as_pydantic_does(user: dict[str, Any], mutation_id: float, result: Any) -> None:
    response = MutationResponse(id=MutationID(id=mutation_id, clientID="c"), result=result)
    success = MutateSuccess(mutations=[response], **user)
    assert success.to_json() == success.model_dump(mode="json")
    assert list(success.to_json()) == list(success.model_dump(mode="json"))


def test_model_dump_leaves_out_excluded_fields() -> None:
    assert MutationID(id=1, clientID="c").model_dump(exclude={"id"}) == {"clientID": "c"}


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
    from zero_server.protocol.ast import CorrelatedSubqueryCondition, _cmp_condition

    related = CorrelatedSubquery(
        correlation=Correlation(parent_field=("id",), child_field=("id",)), subquery=AST(table="u", alias="s")
    )
    plain = CorrelatedSubqueryCondition(related=related, op="EXISTS")
    flipped = CorrelatedSubqueryCondition(related=related, op="EXISTS", flip=True, scalar=False)
    assert _cmp_condition(plain, flipped) < 0
    assert _cmp_condition(flipped, dataclasses.replace(flipped, scalar=True)) < 0


class Mood(StrEnum):
    HAPPY = "happy"


@dataclasses.dataclass
class Point:
    xy: tuple[int, int]
    mood: Mood


def test_a_literal_built_in_python_serializes_as_pydantic_does() -> None:
    def literal(value: Any) -> Any:
        return LiteralReference(value=value).to_json()["value"]

    assert literal((1, Mood.HAPPY)) == [1, "happy"]
    assert literal({"p": Point(xy=(1, 2), mood=Mood.HAPPY)}) == {"p": {"xy": [1, 2], "mood": "happy"}}


def _custom(args: Any) -> CustomMutation:
    return CustomMutation(type="custom", id=1, clientID="c", name="m", args=args, timestamp=0)


def test_json_args_other_than_plain_json_get_pydantics_check() -> None:
    plain = [{"a": [1, 2.5, None, True]}]
    assert _custom(plain).args is plain
    assert _custom((1,)).args == [1]
    assert type(_custom([Mood.HAPPY]).args[0]) is str
    for args in ([(1,)], [{1: 2}], [{"a": {1}}], None):
        with pytest.raises(ValidationError):
            _custom(args)


@pytest.mark.parametrize("body", [{"table": "t", "limit": None}, b'{"table": "t", "limit": null}'])
def test_an_ast_off_the_wire_refuses_null_for_an_optional_field(body: object) -> None:
    with pytest.raises(ValidationError, match=r"limit.int\n.*input_value=None"):
        AST.from_json(body)
    assert AST(table="t", limit=None).to_json() == {"table": "t"}


def test_a_model_built_in_python_refuses_null_off_the_wire() -> None:
    with pytest.raises(ValidationError, match="message may be absent but not null"):
        AppErroredQuery.from_json({"error": "app", "id": "1", "name": "q", "message": None})
    assert AppErroredQuery.from_json({"error": "app", "id": "1", "name": "q", "details": 1}).details == 1
    assert AppErroredQuery(id="1", name="q", message=None).to_json() == {"error": "app", "id": "1", "name": "q"}


def test_an_optional_field_must_say_how_it_refuses_null() -> None:
    with pytest.raises(TypeError, match="Omittable or extend RefusesNullOffWire"):

        class Loose(WireModel):
            note: str | None = None
