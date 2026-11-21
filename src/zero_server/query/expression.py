"""Conditions for `Query.where`: `cmp`, `and_`, `or_`, `not_`, `exists` and `json`, and the selector an
aggregate takes.

Ported from rocicorp/mono `packages/zql/src/query/expression.ts` at zero/v1.12.0
(d489a3218ad603b3fd65e33cec76ae77cea7c7af). Apache-2.0, Copyright Rocicorp.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

from zero_server.protocol.ast import (
    MAX_JSON_PATH_INDEX,
    Aggregate,
    AggregateFunction,
    ColumnReference,
    Condition,
    Conjunction,
    CorrelatedSubqueryCondition,
    Disjunction,
    JsonPathReference,
    LiteralReference,
    Parameter,
    SimpleCondition,
    SimpleOperator,
    is_homogeneous_list,
    is_valid_json_path_index,
)
from zero_server.shared.asserts import check
from zero_server.shared.js import js_string

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


class ColumnRef:
    """A value inside a `json()` column, from `eb.json(column, *path)`: the left operand of `cmp`."""

    __slots__ = ("_ref",)

    def __init__(self, ref: JsonPathReference) -> None:
        self._ref = ref

    def reference(self) -> JsonPathReference:
        return self._ref


def make_column_ref(name: str, path: tuple[Any, ...]) -> ColumnRef:
    # A whole JSON column is never a comparison operand: only its scalar leaves are.
    if not path:
        raise Exception(f"json('{js_string(name)}') requires at least one path segment")
    for seg in path:
        if isinstance(seg, int | float) and not isinstance(seg, bool) and not is_valid_json_path_index(seg):
            raise Exception(
                f"Invalid JSON path segment {js_string(seg)} in json('{js_string(name)}', ...): a numeric "
                "segment is an array index and must be a non-negative integer "
                f"no larger than {MAX_JSON_PATH_INDEX}"
            )
    return ColumnRef(JsonPathReference(value=ColumnReference(name=name), path=path))


class _JsonAggregateBuilder:
    """What an aggregate's selector callback receives: `q.sum(lambda b: b.json("meta", "points"))`."""

    @staticmethod
    def json(column: str, *path: Any) -> ColumnRef:
        return make_column_ref(column, path)


_JSON_AGGREGATE_BUILDER = _JsonAggregateBuilder()


def aggregate_from_selector(fn: AggregateFunction, selector: Any) -> Aggregate:
    """The aggregate `fn` of `selector`: nothing for `count`, a column name, or a callback that picks a leaf
    inside a `json()` column."""
    if not callable(selector):
        return Aggregate(fn=fn, field=selector)
    ref = selector(_JSON_AGGREGATE_BUILDER)
    if not isinstance(ref, ColumnRef):
        raise Exception(f"{fn}() expects the callback to return json(...)")
    reference = ref.reference()
    return Aggregate(fn=fn, field=reference.value.name, path=reference.path)


def cmp(field: str | ColumnRef, op_or_value: Any, value: Any = MISSING) -> Condition:
    """`cmp(field, value)` compares with `=`; `cmp(field, op, value)` with `op`."""
    if value is MISSING:
        op, value = "=", op_or_value
    else:
        op = op_or_value
    if isinstance(value, ColumnRef):
        raise Exception("A json() reference is only valid as the left operand of cmp()")
    if isinstance(field, ColumnRef):
        # The engines compare a JSON leaf with the type of the list's first element.
        if op in ("IN", "NOT IN") and isinstance(value, list | tuple) and not is_homogeneous_list(value):
            raise Exception(f"{op} against a JSON path requires a list of values of one type")
        return SimpleCondition(op=op, left=field.reference(), right=_position(value))
    return SimpleCondition(op=op, left=ColumnReference(name=field), right=_position(value))


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

    def cmp(self, field: str | ColumnRef, op_or_value: Any, value: Any = MISSING) -> Condition:
        if self._check_column is not None and not isinstance(field, ColumnRef):  # python-only: strict mode
            self._check_column(field)
        return cmp(field, op_or_value, value)

    def json(self, column: str, *path: str | int) -> ColumnRef:
        """A value inside a `json()` column for `cmp`: `cmp(json("meta", "tags", 0), "=", "x")`. Each segment
        is an object key or an array index, and at least one is required."""
        return make_column_ref(column, path)

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
