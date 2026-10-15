"""Conditions for `Query.where`: `cmp`, `and_`, `or_`, `not_` and `exists`.

Ported from rocicorp/mono `packages/zql/src/query/expression.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

from zero_server.protocol.ast import (
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
from zero_server.shared.asserts import check

if TYPE_CHECKING:
    from zero_server.query.builder import Query

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
    if type(value) is Parameter:
        return value
    # Each literal gets its own array, as each JavaScript array literal is its own object: the SQL
    # compiler shares a placeholder between two literals only when they are the same object.
    return LiteralReference(value=list(value) if isinstance(value, tuple) else value)


def cmp(field: str, op_or_value: Any, value: Any = MISSING) -> Condition:
    """`cmp(field, value)` compares with `=`; `cmp(field, op, value)` with `op`."""
    if value is MISSING:
        return SimpleCondition(op="=", left=ColumnReference(name=field), right=_position(op_or_value))
    return SimpleCondition(op=op_or_value, left=ColumnReference(name=field), right=_position(value))


def _is_always_true(condition: Condition) -> bool:
    return isinstance(condition, Conjunction) and not condition.conditions


def _is_always_false(condition: Condition) -> bool:
    return isinstance(condition, Disjunction) and not condition.conditions


def and_(*conditions: Condition | None) -> Condition:
    expressions = [c for c in conditions if c is not None and not _is_always_true(c)]
    if len(expressions) == 1:
        return expressions[0]
    for c in expressions:
        if _is_always_false(c):
            return FALSE
    return Conjunction(conditions=tuple(expressions))


def or_(*conditions: Condition | None) -> Condition:
    expressions = [c for c in conditions if c is not None and not _is_always_false(c)]
    if len(expressions) == 1:
        return expressions[0]
    for c in expressions:
        if _is_always_true(c):
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
                op=_negate(expression.op),  # ty: ignore[invalid-argument-type]
                flip=expression.flip,
                scalar=expression.scalar,
            )
        case _:
            return expression.negated(_negate(expression.op))


def _negate(op: str) -> str:
    negated = _NEGATED.get(op)
    check(negated is not None, "Unexpected undefined value")
    return negated  # ty: ignore[invalid-return-type]


def simplify_condition(c: Condition) -> Condition:
    if not isinstance(c, Conjunction | Disjunction):
        return c
    conditions = c.conditions
    if len(conditions) == 1:
        return simplify_condition(conditions[0])
    kind = type(c)
    # The other kind's empty condition, `false` in an `and` and `true` in an `or`, decides it.
    absorbing = Disjunction if kind is Conjunction else Conjunction
    flat: list[Condition] = []
    for x in conditions:
        x = simplify_condition(x)
        if type(x) is kind:
            flat.extend(x.conditions)  # ty: ignore[unresolved-attribute]
        elif isinstance(x, absorbing) and not x.conditions:
            return FALSE if kind is Conjunction else TRUE
        else:
            flat.append(x)
    return kind(conditions=tuple(flat))


type ExistsFn = Callable[..., Condition]


class ExpressionBuilder:
    """What a `where` callback receives. `and`, `or` and `not` gain a trailing underscore in Python."""

    def __init__(self, exists: ExistsFn, check_column: Callable[[str], None] | None = None) -> None:
        self._exists = exists
        self._check_column = check_column

    @property
    def eb(self) -> "ExpressionBuilder":
        return self

    def cmp(self, field: str, op_or_value: Any, value: Any = MISSING) -> Condition:
        if self._check_column is not None:
            self._check_column(field)
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
