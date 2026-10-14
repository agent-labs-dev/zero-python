"""Conditions for `Query.where`: `cmp`, `and_`, `or_`, `not_` and `exists`.

Ported from rocicorp/mono `packages/zql/src/query/expression.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.
"""

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Final

from zero_python.protocol.ast import (
    ColumnReference,
    Condition,
    Conjunction,
    CorrelatedSubqueryCondition,
    Disjunction,
    LiteralReference,
    Parameter,
    SimpleCondition,
    SimpleOperator,
)

if TYPE_CHECKING:
    from zero_python.query.builder import Query

MISSING: Final[Any] = object()
"""Marks an argument the caller left out, which upstream tells apart from an explicit `undefined`."""

TRUE: Final[Condition] = Conjunction(conditions=())
FALSE: Final[Condition] = Disjunction(conditions=())

_NEGATED: Final[dict[str, str]] = {
    "=": "!=",
    "!=": "=",
    "<": ">=",
    ">": "<=",
    ">=": "<",
    "<=": ">",
    "IN": "NOT IN",
    "NOT IN": "IN",
    "LIKE": "NOT LIKE",
    "NOT LIKE": "LIKE",
    "ILIKE": "NOT ILIKE",
    "NOT ILIKE": "ILIKE",
    "IS": "IS NOT",
    "IS NOT": "IS",
    "EXISTS": "NOT EXISTS",
    "NOT EXISTS": "EXISTS",
}


def _position(value: Any) -> LiteralReference | Parameter:
    return value if isinstance(value, Parameter) else LiteralReference(value=value)


def cmp(field: str, op_or_value: Any, value: Any = MISSING) -> Condition:
    """`cmp(field, value)` compares with `=`; `cmp(field, op, value)` with `op`."""
    if value is MISSING:
        op, actual = "=", op_or_value
    else:
        op, actual = op_or_value, value
    return SimpleCondition(op=op, left=ColumnReference(name=field), right=_position(actual))


def _is_always_true(condition: Condition) -> bool:
    return isinstance(condition, Conjunction) and not condition.conditions


def _is_always_false(condition: Condition) -> bool:
    return isinstance(condition, Disjunction) and not condition.conditions


def and_(*conditions: Condition | None) -> Condition:
    expressions = [c for c in conditions if c is not None and not _is_always_true(c)]
    if len(expressions) == 1:
        return expressions[0]
    if any(_is_always_false(c) for c in expressions):
        return FALSE
    return Conjunction(conditions=tuple(expressions))


def or_(*conditions: Condition | None) -> Condition:
    expressions = [c for c in conditions if c is not None and not _is_always_false(c)]
    if len(expressions) == 1:
        return expressions[0]
    if any(_is_always_true(c) for c in expressions):
        return TRUE
    return Disjunction(conditions=tuple(expressions))


def not_(expression: Condition) -> Condition:
    match expression:
        case Conjunction():
            return Disjunction(conditions=tuple(not_(c) for c in expression.conditions))
        case Disjunction():
            return Conjunction(conditions=tuple(not_(c) for c in expression.conditions))
        case CorrelatedSubqueryCondition():
            return CorrelatedSubqueryCondition(
                related=expression.related,
                op=_NEGATED[expression.op],  # ty: ignore[invalid-argument-type]
                flip=expression.flip,
                scalar=expression.scalar,
            )
        case _:
            return SimpleCondition(
                op=_NEGATED[expression.op],  # ty: ignore[invalid-argument-type]
                left=expression.left,
                right=expression.right,
            )


def _flatten(kind: str, conditions: Sequence[Condition]) -> list[Condition]:
    out: list[Condition] = []
    for c in conditions:
        if isinstance(c, Conjunction | Disjunction) and c.type == kind:
            out.extend(c.conditions)
        else:
            out.append(c)
    return out


def simplify_condition(c: Condition) -> Condition:
    if isinstance(c, SimpleCondition | CorrelatedSubqueryCondition):
        return c
    if len(c.conditions) == 1:
        return simplify_condition(c.conditions[0])
    conditions = _flatten(c.type, [simplify_condition(x) for x in c.conditions])
    if c.type == "and" and any(_is_always_false(x) for x in conditions):
        return FALSE
    if c.type == "or" and any(_is_always_true(x) for x in conditions):
        return TRUE
    return type(c)(conditions=tuple(conditions))


type ExistsFn = Callable[..., Condition]


class ExpressionBuilder:
    """What a `where` callback receives. `and`, `or` and `not` gain a trailing underscore in Python."""

    def __init__(self, exists: ExistsFn) -> None:
        self._exists = exists

    @property
    def eb(self) -> "ExpressionBuilder":
        return self

    def cmp(self, field: str, op_or_value: Any, value: Any = MISSING) -> Condition:
        return cmp(field, op_or_value, value)

    def cmp_lit(self, left: Any, op: SimpleOperator, right: Any) -> Condition:
        return SimpleCondition(op=op, left=_position(left), right=_position(right))

    and_ = staticmethod(and_)
    or_ = staticmethod(or_)
    not_ = staticmethod(not_)

    def exists(
        self,
        relationship: str,
        cb: "Callable[[Query], Query] | None" = None,
        *,
        flip: bool | None = None,
        scalar: bool | None = None,
    ) -> Condition:
        return self._exists(relationship, cb, flip=flip, scalar=scalar)
