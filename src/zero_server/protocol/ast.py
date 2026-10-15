"""The ZQL AST: what a query endpoint returns for each query zero-cache asks it to transform.

Ported from rocicorp/mono `packages/zero-protocol/src/ast.ts`, `data.ts` and `primary-key.ts` at
zero/v1.10.0 (905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.

The AST carries server names: `map_ast` maps a client-named AST through the schema's name mapper before
it goes on the wire, as `handleQueryRequest` does upstream.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol

from pydantic import Field, PrivateAttr, StrictBool, StrictStr

from zero_server.protocol.wire import Number, StrictWireModel, WireChecked
from zero_server.shared.asserts import AssertionFailedError, check
from zero_server.shared.js import js_keys, js_sort, js_string
from zero_server.shared.json import JSONValue

SUBQ_PREFIX = "zsubq_"

type System = Literal["permissions", "client", "test"]
type Direction = Literal["asc", "desc"]
type OrderPart = tuple[StrictStr, Direction]
type Ordering = tuple[OrderPart, ...]

type EqualityOp = Literal["=", "!=", "IS", "IS NOT"]
type OrderOp = Literal["<", ">", "<=", ">="]
type LikeOp = Literal["LIKE", "NOT LIKE", "ILIKE", "NOT ILIKE"]
type InOp = Literal["IN", "NOT IN"]
type SimpleOperator = Literal[
    "=", "!=", "IS", "IS NOT", "<", ">", "<=", ">=", "LIKE", "NOT LIKE", "ILIKE", "NOT ILIKE", "IN", "NOT IN"
]
type CorrelatedSubqueryConditionOperator = Literal["EXISTS", "NOT EXISTS"]

type Scalar = StrictStr | Number | StrictBool
# A list, not a tuple: the SQL compiler tells literals apart by identity, and Python shares `()`.
type LiteralValue = Scalar | list[Scalar] | None

type Value = JSONValue
"""A row value. Upstream also allows `undefined`, which it treats as `null`."""
type Row = dict[str, Value]

type PrimaryKey = tuple[StrictStr, ...]
type CompoundKey = Annotated[tuple[StrictStr, ...], Field(min_length=1)]


class LiteralReference(StrictWireModel):
    nullable = frozenset({"value"})

    type: Literal["literal"] = "literal"
    value: WireChecked[LiteralValue]


class ColumnReference(StrictWireModel):
    type: Literal["column"] = "column"
    name: WireChecked[StrictStr]


class Parameter(StrictWireModel):
    """A value resolved at runtime: the caller's auth data, or a row before a mutation touched it."""

    type: Literal["static"] = "static"
    anchor: Literal["authData", "preMutationRow"]
    field: StrictStr | tuple[StrictStr, ...]


type ValuePosition = Annotated[LiteralReference | ColumnReference | Parameter, Field(discriminator="type")]


class SimpleCondition(StrictWireModel):
    type: Literal["simple"] = "simple"
    left: ValuePosition
    right: Annotated[Parameter | LiteralReference, Field(discriminator="type")]
    op: WireChecked[SimpleOperator]
    # Upstream's `not` writes the negated condition with `op` second.
    _op_first: bool = PrivateAttr(default=False)

    def negated(self, op: str) -> SimpleCondition:
        condition = SimpleCondition(op=op, left=self.left, right=self.right)  # ty: ignore[invalid-argument-type]
        condition._op_first = True
        return condition

    def _ordered(self, dumped: dict[str, Any]) -> dict[str, Any]:
        if not self._op_first:
            return dumped
        return {"type": dumped["type"], "op": dumped["op"], "left": dumped["left"], "right": dumped["right"]}


class Conjunction(StrictWireModel):
    type: Literal["and"] = "and"
    conditions: tuple[Condition, ...]


class Disjunction(StrictWireModel):
    type: Literal["or"] = "or"
    conditions: tuple[Condition, ...]


class CorrelatedSubqueryCondition(StrictWireModel):
    type: Literal["correlatedSubquery"] = "correlatedSubquery"
    related: CorrelatedSubquery
    op: CorrelatedSubqueryConditionOperator
    flip: WireChecked[StrictBool | None] = None
    scalar: WireChecked[StrictBool | None] = None

    def _ordered(self, dumped: dict[str, Any]) -> dict[str, Any]:
        # The builder writes a condition's subquery `system` first, where `related` on the AST has it last.
        related = dumped["related"]
        keys = [k for k in ("system", "correlation", "hidden", "subquery") if k in related]
        return {**dumped, "related": {k: related[k] for k in keys}}


type Condition = Annotated[
    SimpleCondition | Conjunction | Disjunction | CorrelatedSubqueryCondition, Field(discriminator="type")
]


class Correlation(StrictWireModel):
    parent_field: CompoundKey = Field(alias="parentField")
    child_field: CompoundKey = Field(alias="childField")


class CorrelatedSubquery(StrictWireModel):
    correlation: Correlation
    # A hop the client never sees, such as the junction table of a many-to-many relationship.
    hidden: StrictBool | None = None
    subquery: AST
    system: System | None = None


class Bound(StrictWireModel):
    row: WireChecked[Row]
    exclusive: StrictBool


class AST(StrictWireModel):
    schema_: StrictStr | None = Field(default=None, alias="schema")
    table: StrictStr
    # Set on every subquery, to tell apart two subqueries over the same table.
    alias: StrictStr | None = None
    where: Condition | None = None
    related: tuple[CorrelatedSubquery, ...] | None = None
    start: Bound | None = None
    limit: Number | None = None
    order_by: Ordering | None = Field(default=None, alias="orderBy")


for _model in (Conjunction, Disjunction, CorrelatedSubqueryCondition, CorrelatedSubquery, AST):
    _model.model_rebuild()


class NameMapper(Protocol):
    def table_name(self, table: str, /) -> str: ...

    def column_name(self, table: str, column: str, /) -> str: ...


@dataclass(frozen=True)
class _Transform:
    table_name: Callable[[str], str]
    column_name: Callable[[str, str], str]
    related: Callable[[list[CorrelatedSubquery]], Sequence[CorrelatedSubquery]]
    where: Callable[[Condition], Condition | None]
    conditions: Callable[[list[Condition]], Sequence[Condition]]


def _key(transform: _Transform, table: str, key: Sequence[str]) -> tuple[str, ...]:
    mapped = tuple(transform.column_name(table, column) for column in key)
    check(len(mapped) >= 1, "Expected non-empty array for compound key")
    return mapped


def _transform_ast(ast: AST, transform: _Transform) -> AST:
    def col(column: str) -> str:
        return transform.column_name(ast.table, column)

    # Mapped in upstream's order, so a batch with two unknown names fails on the same one.
    where = transform.where(ast.where) if ast.where is not None else None
    table = transform.table_name(ast.table)
    where = _transform_where(where, ast.table, transform) if where is not None else None
    related = (
        tuple(
            transform.related(
                [
                    CorrelatedSubquery(
                        correlation=Correlation(
                            parent_field=_key(transform, ast.table, r.correlation.parent_field),
                            child_field=_key(transform, r.subquery.table, r.correlation.child_field),
                        ),
                        hidden=r.hidden,
                        subquery=_transform_ast(r.subquery, transform),
                        system=r.system,
                    )
                    for r in ast.related
                ]
            )
        )
        if ast.related is not None
        else None
    )
    return AST(
        schema=ast.schema_,
        table=table,
        alias=ast.alias,
        where=where,
        related=related,
        start=(
            Bound(row={col(name): value for name, value in _entries(ast.start.row)}, exclusive=ast.start.exclusive)
            if ast.start is not None
            else None
        ),
        limit=ast.limit,
        orderBy=tuple((col(name), direction) for name, direction in ast.order_by) if ast.order_by is not None else None,
    )


def _entries(row: Row) -> list[tuple[str, Value]]:
    """`Object.entries(row)`, index-like keys first. The builder takes any row, and an array's keys are its indexes."""
    if isinstance(row, list):
        return [(str(i), value) for i, value in enumerate(row)]
    return [(key, row[key]) for key in js_keys(row)]


def _transform_where(where: Condition, table: str, transform: _Transform) -> Condition:
    match where:
        case SimpleCondition(left=ColumnReference(name=name)):
            return where.model_copy(update={"left": ColumnReference(name=transform.column_name(table, name))})
        case SimpleCondition():
            return where
        case CorrelatedSubqueryCondition(related=related):
            mapped = related.model_copy(
                update={
                    "correlation": Correlation(
                        parent_field=_key(transform, table, related.correlation.parent_field),
                        child_field=_key(transform, related.subquery.table, related.correlation.child_field),
                    ),
                    "subquery": _transform_ast(related.subquery, transform),
                }
            )
            return where.model_copy(update={"related": mapped})
        case _:
            conditions = tuple(transform.conditions([_transform_where(c, table, transform) for c in where.conditions]))
            return type(where)(conditions=conditions)


def map_ast(ast: AST, mapper: NameMapper) -> AST:
    """Rename every table and column in `ast` through `mapper`, client names to server names upstream."""
    return _transform_ast(ast, _mapping(mapper))


def map_condition(condition: Condition, table: str, mapper: NameMapper) -> Condition:
    return _transform_where(condition, table, _mapping(mapper))


def _mapping(mapper: NameMapper) -> _Transform:
    return _Transform(
        table_name=mapper.table_name,
        column_name=mapper.column_name,
        related=lambda r: r,
        where=lambda w: w,
        conditions=lambda c: c,
    )


def normalize_ast(ast: AST) -> AST:
    """The canonical form zero-cache hashes: flattened conditions, sorted conditions and subqueries."""
    return _transform_ast(
        ast,
        _Transform(
            table_name=lambda t: t,
            column_name=lambda _t, c: c,
            related=lambda r: js_sort(r, _cmp_related),
            where=_flattened,
            conditions=lambda c: js_sort(c, _cmp_condition),
        ),
    )


def _utf8(value: str) -> bytes:
    return value.encode("utf-8", "surrogatepass")


def _cmp(a: object, b: object) -> int:
    return (a > b) - (a < b)  # ty: ignore[unsupported-operator]


def _cmp_utf8(a: str, b: str) -> int:
    return _cmp(_utf8(a), _utf8(b))


def _cmp_value_position(
    a: LiteralReference | ColumnReference | Parameter, b: LiteralReference | ColumnReference | Parameter
) -> int:
    if a.type != b.type:
        return _cmp_utf8(a.type, b.type)
    match a, b:
        case LiteralReference(), LiteralReference():
            return _cmp_utf8(js_string(a.value), js_string(b.value))
        case ColumnReference(), ColumnReference():
            return _cmp_utf8(a.name, b.name)
        case _:
            raise ValueError("Static parameters should be resolved before normalization")


def _cmp_related(a: CorrelatedSubquery, b: CorrelatedSubquery) -> int:
    return _cmp_utf8(_must(a.subquery.alias), _must(b.subquery.alias))


def _must(alias: str | None) -> str:
    if alias is None:
        raise AssertionFailedError("Unexpected undefined value")
    return alias


def _cmp_optional_bool(a: bool | None, b: bool | None) -> int:
    def rank(v: bool | None) -> int:
        return 0 if v is None else 2 if v else 1

    return rank(a) - rank(b)


def _cmp_condition(a: Condition, b: Condition) -> int:
    if isinstance(a, SimpleCondition):
        if not isinstance(b, SimpleCondition):
            return -1
        return _cmp_value_position(a.left, b.left) or _cmp_utf8(a.op, b.op) or _cmp_value_position(a.right, b.right)
    if isinstance(b, SimpleCondition):
        return 1
    if isinstance(a, CorrelatedSubqueryCondition):
        if not isinstance(b, CorrelatedSubqueryCondition):
            return -1
        return (
            _cmp_related(a.related, b.related)
            or _cmp_utf8(a.op, b.op)
            or _cmp_optional_bool(a.flip, b.flip)
            or _cmp_optional_bool(a.scalar, b.scalar)
        )
    if isinstance(b, CorrelatedSubqueryCondition):
        # Upstream returns -1 here too, so a subquery and a compound condition each sort first.
        return -1
    by_type = _cmp_utf8(a.type, b.type)
    if by_type:
        return by_type
    for left, right in zip(a.conditions, b.conditions, strict=False):
        by_condition = _cmp_condition(left, right)
        if by_condition:
            return by_condition
    return len(a.conditions) - len(b.conditions)


def _flattened(condition: Condition) -> Condition | None:
    if isinstance(condition, SimpleCondition | CorrelatedSubqueryCondition):
        return condition
    flat: list[Condition | None] = []
    for c in condition.conditions:
        if isinstance(c, Conjunction | Disjunction) and c.type == condition.type:
            flat.extend(_flattened(inner) for inner in c.conditions)
        else:
            flat.append(_flattened(c))
    conditions = [c for c in flat if c is not None]
    match len(conditions):
        case 0:
            return None
        case 1:
            return conditions[0]
        case _:
            return type(condition)(conditions=tuple(conditions))
