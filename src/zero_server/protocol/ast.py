"""The ZQL AST: what a query endpoint returns for each query zero-cache asks it to transform.

Ported from rocicorp/mono `packages/zero-protocol/src/ast.ts`, `data.ts` and `primary-key.ts` at
zero/v1.10.0 (905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.

The AST carries server names: `map_ast` maps a client-named AST through the schema's name mapper before
it goes on the wire, as `handleQueryRequest` does upstream.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol

from zero_server.protocol.wire import dump_json, json_model, parse_wire
from zero_server.schema import NameMapper as SchemaNameMapper
from zero_server.shared.asserts import AssertionFailedError
from zero_server.shared.js import js_keys, js_sort, js_string
from zero_server.shared.json import JSONValue

if TYPE_CHECKING:
    from pydantic import TypeAdapter

    from zero_server.protocol import ast_wire

SUBQ_PREFIX = "zsubq_"

type System = Literal["permissions", "client", "test"]
type Direction = Literal["asc", "desc"]
type OrderPart = tuple[str, Direction]
type Ordering = tuple[OrderPart, ...]

type EqualityOp = Literal["=", "!=", "IS", "IS NOT"]
type OrderOp = Literal["<", ">", "<=", ">="]
type LikeOp = Literal["LIKE", "NOT LIKE", "ILIKE", "NOT ILIKE"]
type InOp = Literal["IN", "NOT IN"]
type SimpleOperator = Literal[
    "=", "!=", "IS", "IS NOT", "<", ">", "<=", ">=", "LIKE", "NOT LIKE", "ILIKE", "NOT ILIKE", "IN", "NOT IN"
]
type CorrelatedSubqueryConditionOperator = Literal["EXISTS", "NOT EXISTS"]

type Scalar = str | int | float | bool
# A list, not a tuple: the SQL compiler tells literals apart by identity, and Python shares `()`.
type LiteralValue = Scalar | list[Scalar] | None

type Value = JSONValue
"""A row value. Upstream also allows `undefined`, which it treats as `null`."""
type Row = dict[str, Value]

type PrimaryKey = tuple[str, ...]
type CompoundKey = tuple[str, ...]

_SCALARS = frozenset({str, int, float, bool, type(None)})
_new = object.__new__


def _json(value: Any) -> Any:
    """`value` as JSON. A node holds whatever the builder was given, as upstream's does."""
    return value if type(value) is str else dump_json(value)


def _names(names: Sequence[Any]) -> list[Any]:
    """A sequence of names as a JSON list, without a call per name."""
    return [name if type(name) is str else dump_json(name) for name in names]


# The nodes are plain slotted classes: the builder makes dozens per query, and a Pydantic model costs
# several times as much to build. They are not frozen, which would cost as much again, but no code
# changes a node once built: every transformation builds new ones. `to_json` writes upstream's keys in
# upstream's order and leaves out a field that is `None`. `AST.from_json` parses through the wire
# schema in `ast_wire`, which accepts exactly what upstream's `astSchema` accepts.


@json_model
@dataclass(slots=True, kw_only=True)
class LiteralReference:
    type: ClassVar[Literal["literal"]] = "literal"
    value: LiteralValue

    def to_json(self) -> dict[str, Any]:
        return {"type": "literal", "value": self.value if type(self.value) in _SCALARS else dump_json(self.value)}


@json_model
@dataclass(slots=True, kw_only=True)
class ColumnReference:
    type: ClassVar[Literal["column"]] = "column"
    name: str

    def to_json(self) -> dict[str, Any]:
        return {"type": "column", "name": _json(self.name)}


@json_model
@dataclass(slots=True, kw_only=True)
class Parameter:
    """A value resolved at runtime: the caller's auth data, or a row before a mutation touched it."""

    type: ClassVar[Literal["static"]] = "static"
    anchor: Literal["authData", "preMutationRow"]
    field: str | tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {"type": "static", "anchor": _json(self.anchor), "field": _json(self.field)}


type ValuePosition = LiteralReference | ColumnReference | Parameter


@json_model
@dataclass(slots=True, kw_only=True)
class SimpleCondition:
    type: ClassVar[Literal["simple"]] = "simple"
    left: ValuePosition
    right: Parameter | LiteralReference
    op: SimpleOperator

    def to_json(self) -> dict[str, Any]:
        return {"type": "simple", "left": self.left.to_json(), "right": self.right.to_json(), "op": _json(self.op)}

    def negated(self, op: str) -> SimpleCondition:
        return _NegatedSimpleCondition(op=op, left=self.left, right=self.right)  # ty: ignore[invalid-argument-type]

    def _with_left(self, left: ValuePosition) -> SimpleCondition:
        """This condition with `left` in place of its own, of the same class."""
        copy = _new(type(self))
        copy.left = left
        copy.right = self.right
        copy.op = self.op
        return copy


@json_model
@dataclass(slots=True, kw_only=True)
class _NegatedSimpleCondition(SimpleCondition):
    """Upstream's `not` writes the negated condition with `op` second."""

    def to_json(self) -> dict[str, Any]:
        return {"type": "simple", "op": _json(self.op), "left": self.left.to_json(), "right": self.right.to_json()}


@json_model
@dataclass(slots=True, kw_only=True)
class Conjunction:
    type: ClassVar[Literal["and"]] = "and"
    conditions: tuple[Condition, ...]

    def to_json(self) -> dict[str, Any]:
        return {"type": "and", "conditions": [c.to_json() for c in self.conditions]}


@json_model
@dataclass(slots=True, kw_only=True)
class Disjunction:
    type: ClassVar[Literal["or"]] = "or"
    conditions: tuple[Condition, ...]

    def to_json(self) -> dict[str, Any]:
        return {"type": "or", "conditions": [c.to_json() for c in self.conditions]}


@json_model
@dataclass(slots=True, kw_only=True)
class CorrelatedSubqueryCondition:
    type: ClassVar[Literal["correlatedSubquery"]] = "correlatedSubquery"
    related: CorrelatedSubquery
    op: CorrelatedSubqueryConditionOperator
    flip: bool | None = None
    scalar: bool | None = None

    def to_json(self) -> dict[str, Any]:
        # The builder writes a condition's subquery `system` first, where `related` on the AST has it last.
        related = self.related
        r: dict[str, Any] = {} if related.system is None else {"system": _json(related.system)}
        r["correlation"] = related.correlation.to_json()
        if related.hidden is not None:
            r["hidden"] = related.hidden if type(related.hidden) in _SCALARS else dump_json(related.hidden)
        r["subquery"] = related.subquery.to_json()
        out = {"type": "correlatedSubquery", "related": r, "op": _json(self.op)}
        if self.flip is not None:
            out["flip"] = self.flip if type(self.flip) in _SCALARS else dump_json(self.flip)
        if self.scalar is not None:
            out["scalar"] = self.scalar if type(self.scalar) in _SCALARS else dump_json(self.scalar)
        return out


type Condition = SimpleCondition | Conjunction | Disjunction | CorrelatedSubqueryCondition


@json_model
@dataclass(slots=True, kw_only=True)
class Correlation:
    parent_field: CompoundKey
    child_field: CompoundKey

    def to_json(self) -> dict[str, Any]:
        return {"parentField": _names(self.parent_field), "childField": _names(self.child_field)}


@json_model
@dataclass(slots=True, kw_only=True)
class CorrelatedSubquery:
    correlation: Correlation
    # A hop the client never sees, such as the junction table of a many-to-many relationship.
    hidden: bool | None = None
    subquery: AST
    system: System | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"correlation": self.correlation.to_json()}
        if self.hidden is not None:
            out["hidden"] = self.hidden if type(self.hidden) in _SCALARS else dump_json(self.hidden)
        out["subquery"] = self.subquery.to_json()
        if self.system is not None:
            out["system"] = _json(self.system)
        return out


@json_model
@dataclass(slots=True, kw_only=True)
class Bound:
    row: Row
    exclusive: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "row": dump_json(self.row),
            "exclusive": self.exclusive if type(self.exclusive) in _SCALARS else dump_json(self.exclusive),
        }


@json_model
@dataclass(slots=True, kw_only=True)
class AST:
    schema_: str | None = None
    table: str
    # Set on every subquery, to tell apart two subqueries over the same table.
    alias: str | None = None
    where: Condition | None = None
    related: tuple[CorrelatedSubquery, ...] | None = None
    start: Bound | None = None
    limit: int | float | None = None
    order_by: Ordering | None = None

    def _copy(self) -> AST:
        """A shallow copy, for a caller to change one field of. Twice as fast as `AST(...)` with every field."""
        copy = _new(AST)
        copy.schema_ = self.schema_
        copy.table = self.table
        copy.alias = self.alias
        copy.where = self.where
        copy.related = self.related
        copy.start = self.start
        copy.limit = self.limit
        copy.order_by = self.order_by
        return copy

    def to_json(self) -> dict[str, Any]:
        """The JSON object upstream would send for this AST."""
        out: dict[str, Any] = {} if self.schema_ is None else {"schema": _json(self.schema_)}
        out["table"] = _json(self.table)
        if self.alias is not None:
            out["alias"] = _json(self.alias)
        if self.where is not None:
            out["where"] = self.where.to_json()
        if self.related is not None:
            out["related"] = [r.to_json() for r in self.related]
        if self.start is not None:
            out["start"] = self.start.to_json()
        if self.limit is not None:
            out["limit"] = self.limit if type(self.limit) in _SCALARS else dump_json(self.limit)
        if self.order_by is not None:
            out["orderBy"] = [_names(part) for part in self.order_by]
        return out

    @classmethod
    def from_json(cls, value: object) -> AST:
        """Parse `value` off the wire, raw bytes or decoded JSON, as upstream's `astSchema` does.

        Raises Pydantic's `ValidationError` for a value upstream refuses.
        """
        return parse_wire(_wire_adapter(), value).node()


@functools.cache
def _wire_adapter() -> TypeAdapter[ast_wire.AST]:
    from pydantic import TypeAdapter

    from zero_server.protocol import ast_wire

    return TypeAdapter(ast_wire.AST)


class NameMapper(Protocol):
    def table_name(self, table: str, /) -> str: ...

    def column_name(self, table: str, column: str, /) -> str: ...


@dataclass(frozen=True, slots=True)
class _Transform:
    """How `_transform_ast` rewrites an AST. `None` leaves that part as it is."""

    table_name: Callable[[str], str] | None
    column_name: Callable[[str, str], str]
    related: Callable[[list[CorrelatedSubquery]], Sequence[CorrelatedSubquery]] | None = None
    where: Callable[[Condition], Condition | None] | None = None
    conditions: Callable[[list[Condition]], Sequence[Condition]] | None = None
    # Mapped correlations by parent table, child table and both keys: the same few on every query.
    correlations: dict[tuple[Any, ...], Correlation] | None = None


_MAX_CORRELATIONS = 4096


def _key(column_name: Callable[[str, str], str], table: str, key: Sequence[str]) -> tuple[str, ...]:
    mapped = tuple([column_name(table, column) for column in key])
    if not mapped:
        raise AssertionFailedError("Expected non-empty array for compound key")
    return mapped


def _correlation(transform: _Transform, parent: str, related: CorrelatedSubquery) -> Correlation:
    correlation = related.correlation
    child = related.subquery.table
    cache = transform.correlations
    key: tuple[Any, ...] | None = None
    if cache is not None:
        key = (parent, child, correlation.parent_field, correlation.child_field)
        try:
            mapped = cache.get(key)
        except TypeError:  # A key the builder was handed as a list.
            key = None
        else:
            if mapped is not None:
                return mapped
    column_name = transform.column_name
    mapped = Correlation(
        parent_field=_key(column_name, parent, correlation.parent_field),
        child_field=_key(column_name, child, correlation.child_field),
    )
    if cache is not None and key is not None:
        if len(cache) >= _MAX_CORRELATIONS:
            cache.clear()
        cache[key] = mapped
    return mapped


def _transform_related(transform: _Transform, parent: str, related: CorrelatedSubquery) -> CorrelatedSubquery:
    return CorrelatedSubquery(
        correlation=_correlation(transform, parent, related),
        hidden=related.hidden,
        subquery=_transform_ast(related.subquery, transform),
        system=related.system,
    )


def _transform_ast(ast: AST, transform: _Transform) -> AST:
    # Mapped in upstream's order, so a batch with two unknown names fails on the same one.
    table = ast.table
    where = ast.where
    if where is not None and transform.where is not None:
        where = transform.where(where)
    mapped_table = table if transform.table_name is None else transform.table_name(table)
    if where is not None:
        where = _transform_where(where, table, transform)
    related = ast.related
    if related is not None:
        mapped = [_transform_related(transform, table, r) for r in related]
        related = tuple(mapped if transform.related is None else transform.related(mapped))
    start = ast.start
    order_by = ast.order_by
    column_name = transform.column_name
    if start is not None:
        start = Bound(
            row={column_name(table, name): value for name, value in _entries(start.row)}, exclusive=start.exclusive
        )
    if order_by is not None:
        order_by = tuple([(column_name(table, name), direction) for name, direction in order_by])
    out = ast._copy()
    out.table = mapped_table
    out.where = where
    out.related = related
    out.start = start
    out.order_by = order_by
    return out


def _entries(row: Row) -> list[tuple[str, Value]]:
    """`Object.entries(row)`, index-like keys first. The builder takes any row, and an array's keys are its indexes."""
    if isinstance(row, list):
        return [(str(i), value) for i, value in enumerate(row)]
    return [(key, row[key]) for key in js_keys(row)]


def _transform_where(where: Condition, table: str, transform: _Transform) -> Condition:
    if isinstance(where, SimpleCondition):
        left = where.left
        if not isinstance(left, ColumnReference):
            return where
        return where._with_left(ColumnReference(name=transform.column_name(table, left.name)))
    if isinstance(where, CorrelatedSubqueryCondition):
        related = where.related
        return CorrelatedSubqueryCondition(
            related=_transform_related(transform, table, related),
            op=where.op,
            flip=where.flip,
            scalar=where.scalar,
        )
    conditions = [_transform_where(c, table, transform) for c in where.conditions]
    if transform.conditions is not None:
        return type(where)(conditions=tuple(transform.conditions(conditions)))
    return type(where)(conditions=tuple(conditions))


def map_ast(ast: AST, mapper: NameMapper) -> AST:
    """Rename every table and column in `ast` through `mapper`, client names to server names upstream."""
    return _transform_ast(ast, _mapping(mapper))


def map_condition(condition: Condition, table: str, mapper: NameMapper) -> Condition:
    return _transform_where(condition, table, _mapping(mapper))


def _mapping(mapper: NameMapper) -> _Transform:
    # A schema's mapper keeps what it mapped; another `NameMapper` may not always map a name the same way.
    correlations = mapper.correlations if isinstance(mapper, SchemaNameMapper) else None
    return _Transform(mapper.table_name, mapper.column_name, correlations=correlations)


_NORMALIZE = _Transform(
    table_name=None,
    column_name=lambda _t, c: c,
    related=lambda r: js_sort(r, _cmp_related),
    where=lambda w: _flattened(w),
    conditions=lambda c: js_sort(c, _cmp_condition),
)


def normalize_ast(ast: AST) -> AST:
    """The canonical form zero-cache hashes: flattened conditions, sorted conditions and subqueries."""
    return _transform_ast(ast, _NORMALIZE)


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
