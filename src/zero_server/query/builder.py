"""The ZQL query builder: a `Query` is an immutable AST under construction, in client names.

Ported from rocicorp/mono `packages/zql/src/query/query-impl.ts`, `create-builder.ts`, `isSyntheticColumn`
from `packages/zql/src/ivm/aggregate.ts` and `groupOrdering` from `group-by.ts` at zero/v1.12.0
(d489a3218ad603b3fd65e33cec76ae77cea7c7af). Apache-2.0, Copyright Rocicorp.

The errors match upstream's message for message, because a query endpoint reports them to the client.

A query's AST is always normalized, as upstream's is: each step keeps it so, sorting a new `related`
into place and normalizing the conditions of a `where`.

Queries are interned, as upstream's are, so that rebuilding the same chain hands back the same objects
rather than building them again. Each query holds the query it was derived from, and weakly the ones
derived from it, keyed by the step and its arguments. Python's keys are exact where upstream's are not:
upstream folds a `flip` of `'yes'` into `true`, and Python keeps them apart (see docs/compat.md).
"""

import dataclasses
import math
import re
import weakref
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic

from typing_extensions import TypeVar

from zero_server.protocol.ast import (
    AST,
    SUBQ_PREFIX,
    Aggregate,
    AggregateFunction,
    Bound,
    ColumnReference,
    Condition,
    CorrelatedSubquery,
    CorrelatedSubqueryCondition,
    Correlation,
    Direction,
    JsonPathReference,
    LiteralReference,
    NameMapper,
    Ordering,
    SimpleCondition,
    System,
    insert_related,
    map_ast,
    normalize_condition,
)
from zero_server.query.expression import (
    MISSING,
    ExpressionBuilder,
    _position,
    aggregate_from_selector,
    and_,
    cmp,
    simplify_condition,
)
from zero_server.schema import Connection, Schema
from zero_server.shared.asserts import AssertionFailedError, check
from zero_server.shared.js import js_check_in, js_string, js_stringify


@dataclass(frozen=True, slots=True)
class Format:
    """The shape of a query's result: a row or a list, and the same for each relationship. With `aggregate`,
    the result is that one value instead, and the query is singular."""

    singular: bool = False
    relationships: dict[str, "Format"] = field(default_factory=dict)
    aggregate: Aggregate | None = None


# The columns a grouped or aggregate row carries besides its own: `_0_key`, then the payload columns.
_SYNTHETIC_COLUMNS = frozenset({"_0_key", "_0_value", "_0_sum", "_0_count", "_0_rows"})


def is_synthetic_column(column: object) -> bool:
    return column in _SYNTHETIC_COLUMNS


def group_ordering(ast: AST) -> Ordering:
    """The order of a grouped query's rows: its `orderBy`, which names only grouped columns, then the rest
    of them ascending."""
    # Upstream falls back to a count's distinct columns, which z2s moves into `group_by` before it asks.
    columns = ast.group_by
    assert columns is not None
    ordering: list[tuple[str, Any]] = []
    for column, direction in ast.order_by or ():
        if column in columns and not any(c == column for c, _ in ordering):
            ordering.append((column, direction))
    for column in columns:
        if not any(c == column for c, _ in ordering):
            ordering.append((column, "asc"))
    return tuple(ordering)


# The queries built last, each holding the chain it came from. V8 keeps a query nothing holds until
# its next collection, long enough for upstream to find it when a request builds it again; CPython
# frees it at once. Holding the last few thousand gives a server that builds the same queries request
# after request the same reuse, in bounded memory.
_RECENT: deque[Any] = deque(maxlen=4096)

_MANY = Format()
_ONE = Format(singular=True)
_object_new = object.__new__

R = TypeVar("R", default=dict[str, Any])
"""A row of the query's table, as `tx.run` returns it. Generated schema types narrow it."""
C = TypeVar("C", bound=str, default=str)
"""The table's column names. Generated schema types narrow it to a `Literal`."""

type AnyQuery = "Query[Any, Any]"
type SubqueryFn = Callable[[Any], Any]


class UnknownColumnError(Exception):
    """A strict builder refused a column its table does not have."""


class Query(Generic[R, C]):
    """An immutable query on one table. Every method returns a new query."""

    __slots__ = (
        "__weakref__",
        "_ast",
        "_children",
        "_format",
        "_junction",
        "_mapped",
        "_parent",
        "_schema",
        "_strict",
        "_system",
        "_table",
    )

    def __init__(
        self,
        schema: Schema,
        table: str,
        ast: AST,
        system: System = "client",
        current_junction: str | None = None,
        format: Format | None = None,
        *,
        strict: bool = False,
    ) -> None:
        self._schema = schema
        self._table = table
        self._ast = ast
        self._system: System = system
        self._junction = current_junction
        self._format = format or _MANY
        self._strict = strict
        self._parent: Query[Any, Any] | None = None
        self._children: dict[Any, weakref.ref[Any]] | None = None
        self._mapped: tuple[NameMapper, AST] | None = None

    def __repr__(self) -> str:  # python-only: for debugging
        return f"{type(self).__name__}({self._ast.to_json()!r})"

    def _check_column(self, column: str) -> None:
        if self._strict and column not in self._schema.tables[self._table].columns:  # python-only: strict mode
            raise UnknownColumnError(f'unknown column "{column}" of "{self._table}" table')

    @property
    def format(self) -> Format:
        return self._format

    @property
    def ast(self) -> AST:
        """The AST in client names. The query endpoint maps it to server names."""
        return self._ast

    @property
    def table(self) -> str:  # python-only: upstream keeps the table name private
        return self._table

    def _new(self, table: str, ast: AST, junction: str | None, format: Format = _MANY) -> AnyQuery:
        query = self._copy(Query, ast, format)
        query._table = table
        query._junction = junction
        return query

    def _copy[Q: Query[Any, Any]](self, cls: type[Q], ast: AST, format: Format) -> Q:
        # Skips `__init__`, which every builder call would otherwise pay for.
        query = _object_new(cls)
        query._schema = self._schema
        query._table = self._table
        query._ast = ast
        query._system = self._system
        query._junction = self._junction
        query._format = format
        query._strict = self._strict
        query._parent = None
        query._children = None
        query._mapped = None
        return query

    def _derive[T](self, key: Any, build: Callable[[], T]) -> T:
        """The query `build` makes from this one, or the one it made before for the same `key`.

        `key` names the step and every argument it was given, exactly.
        """
        found = self._find(key)
        return self._keep(key, build()) if found is None else found

    def _find(self, key: Any) -> Any:
        """The query derived from this one for `key` before, while it lives, or `None`."""
        children = self._children
        if children is None:
            return None
        ref = children.get(key)
        return None if ref is None else ref()

    def _keep[T](self, key: Any, query: T) -> T:
        """Remember `query` as the one derived from this query for `key`."""
        children = self._children
        if children is None:
            children = self._children = {}
        query._parent = self  # ty: ignore[unresolved-attribute]
        # Dropped once the query is: a parent keeps no query alive that nothing else holds.
        children[key] = weakref.ref(query, lambda _, children=children, key=key: children.pop(key, None))
        _RECENT.append(query)
        return query

    def _with[Q: Query[Any, Any]](self: Q, field: str, value: Any, format: Format | None = None) -> Q:
        """This query with its AST's `field` set to `value`."""
        ast = self._ast._copy()
        setattr(ast, field, value)
        return self._copy(type(self), ast, format or self._format)

    def _with_relationship[Q: Query[Any, Any]](self: Q, name: str, sub: AnyQuery, entry: CorrelatedSubquery) -> Q:
        format = dataclasses.replace(self._format, relationships={**self._format.relationships, name: sub._format})
        query = self._with("related", insert_related(self._ast.related, entry), format)
        return self._keep(("related", name, id(sub._ast)), query)

    def one(self) -> "OneQuery[R, C]":
        """At most one row: `tx.run` returns it, or `None`."""
        self._assert_not_aggregated("one")
        self._assert_not_distinct("one")

        def build() -> OneQuery[R, C]:
            ast = self._ast._copy()
            ast.limit = 1
            return self._copy(OneQuery, ast, Format(True, self._format.relationships))

        return self._derive(("one",), build)

    def _assert_not_aggregated(self, method: str) -> None:
        """A bound applied after an aggregate would go in the same AST fields as one before it, where it bounds
        the rows the aggregate reduces, so it must come first."""
        aggregate = self._format.aggregate
        if aggregate is not None:
            raise Exception(f"{method}() must come before {aggregate.fn}(): it bounds the rows the aggregate reduces")

    def _assert_not_distinct(self, method: str) -> None:
        if self._ast.group_by:
            kind = "groupBy()" if self._ast.aggregate is not None else "distinct()"
            raise Exception(f"{method}() does not support {kind} yet")

    def _aggregate(self, fn: AggregateFunction, selector: Any = None) -> AnyQuery:
        if self._strict and isinstance(selector, str):  # python-only: strict mode
            self._check_column(selector)
        self._assert_not_aggregated(fn)
        # A count of a distinct() counts its rows, the distinct combinations; a groupBy() has its aggregate.
        distinct = self._ast.group_by
        if distinct and (fn != "count" or self._ast.aggregate is not None):
            self._assert_not_distinct(fn)
        aggregate = Aggregate(fn=fn, distinct=distinct) if distinct else aggregate_from_selector(fn, selector)

        def build() -> AnyQuery:
            ast = self._ast._copy()
            ast.group_by = None
            ast.aggregate = aggregate
            format = dataclasses.replace(self._format, singular=True, aggregate=aggregate)
            return self._copy(Query, ast, format)

        return self._derive(("aggregate", fn, _tag(aggregate.field), _tag(aggregate.path)), build)

    def count(self) -> AnyQuery:
        """The number of rows. `tx.run` returns the number."""
        return self._aggregate("count")

    def sum(self, selector: Any = None) -> AnyQuery:
        """The sum of a column, or of a JSON leaf: `sum(lambda b: b.json("meta", "points"))`. `None` for no rows."""
        return self._aggregate("sum", selector)

    def avg(self, selector: Any = None) -> AnyQuery:
        return self._aggregate("avg", selector)

    def min(self, selector: Any = None) -> AnyQuery:
        return self._aggregate("min", selector)

    def max(self, selector: Any = None) -> AnyQuery:
        return self._aggregate("max", selector)

    def distinct[Q: Query[Any, Any]](self: Q, *columns: C) -> Q:
        """One row of `columns` per distinct combination of their values."""
        self._assert_groupable("distinct", columns)
        return self._derive(("distinct", _tag(columns)), lambda: self._with("group_by", tuple(columns)))

    def group_by(self, *columns: C) -> "GroupedQuery":
        """The rows grouped by `columns`, for an aggregate per group: `group_by("owner_id").count()`."""
        self._assert_groupable("groupBy", columns)
        return GroupedQuery(self, tuple(columns))

    def _group_aggregate(self, columns: tuple[str, ...], fn: AggregateFunction, selector: Any) -> AnyQuery:
        if self._strict and isinstance(selector, str):  # python-only: strict mode
            self._check_column(selector)
        if fn in columns:
            raise Exception(f'groupBy(): the column "{fn}" has the same name as the {fn}() value')
        aggregate = aggregate_from_selector(fn, selector)

        def build() -> AnyQuery:
            ast = self._ast._copy()
            ast.group_by = columns
            ast.aggregate = aggregate
            return self._copy(type(self), ast, self._format)

        return self._derive(("group_by", fn, _tag(columns), _tag(aggregate.field), _tag(aggregate.path)), build)

    def _assert_groupable(self, method: str, columns: Sequence[Any]) -> None:
        self._assert_not_aggregated(method)
        self._assert_not_distinct(method)
        if not columns:
            raise Exception(f"{method}() needs at least one column")
        table_columns = self._schema.tables[self._table].columns
        for i, column in enumerate(columns):
            schema = table_columns.get(column) if isinstance(column, str) else None
            if schema is None:
                raise Exception(f'{method}(): "{js_string(column)}" is not a column of "{self._table}"')
            if schema.type == "json":
                raise Exception(f'{method}() does not support the json column "{column}"')
            if columns.index(column) != i:
                raise Exception(f'{method}(): "{column}" is named twice')
            # The rows carry the synthetic columns too.
            if is_synthetic_column(column):
                raise Exception(
                    f'{method}(): "{column}" is the name of a synthetic column, which a grouped row has too'
                )
        ast = self._ast
        if ast.limit is not None or ast.start is not None:
            raise Exception(f"{method}() does not support limit(), start() or one() yet")
        if ast.related is not None:
            raise Exception(f"{method}() does not support related() yet")
        for column, _ in ast.order_by or ():
            _assert_group_column(column, columns)

    def where[Q: Query[Any, Any]](
        self: Q, field_or_factory: C | Callable[[ExpressionBuilder], Condition], *args: Any
    ) -> Q:
        """`where(field, value)`, `where(field, op, value)`, or `where(lambda eb: ...)`."""
        cond: Condition
        key: Any = None
        # Anything but a function is a field, as upstream has it: a number fails once the column is looked up.
        if isinstance(field_or_factory, str) or not callable(field_or_factory):
            if self._strict:  # python-only: strict mode
                self._check_column(field_or_factory)
            if len(args) == 1:
                op, value = "=", args[0]
            else:
                check(len(args) >= 2, "Invalid condition. Too few arguments.")
                op, value = args[0], args[1]
            # A column compared with a scalar, by far the usual condition, is looked up before it is built.
            kind = type(value)
            if kind in _SCALAR_TYPES and type(field_or_factory) is str and type(op) is str:
                key = ("where", field_or_factory, op, kind, value)
                found = self._find(key)
                if found is not None:
                    return found
            if len(args) == 1:
                cond = SimpleCondition(op="=", left=ColumnReference(name=field_or_factory), right=_position(value))
            else:
                cond = cmp(field_or_factory, op, value)
        else:
            cond = field_or_factory(self.expression_builder())
        if key is None:
            key = ("where", _condition_key(cond))
            found = self._find(key)
            if found is not None:
                return found
        existing = self._ast.where
        merged = cond if existing is None else and_(existing, cond)
        return self._keep(key, self._with("where", normalize_condition(simplify_condition(merged))))

    def where_exists[Q: Query[Any, Any]](
        self: Q,
        relationship: str,
        cb: SubqueryFn | None = None,
        *,
        flip: bool | None = None,
        scalar: bool | None = None,
    ) -> Q:
        return self.where(lambda eb: eb.exists(relationship, cb, flip=flip, scalar=scalar))

    def _relationship(self, relationship: str) -> tuple[Connection, ...]:
        by_table = self._schema.relationships.get(self._table)
        if by_table is None:
            raise TypeError(f"Cannot read properties of undefined (reading '{relationship}')")
        related = by_table.get(relationship)
        if not related:
            raise AssertionFailedError("Invalid relationship")
        return related

    def related[Q: Query[Any, Any]](self: Q, relationship: str, cb: SubqueryFn | None = None) -> Q:
        """Nest the rows of `relationship` under each row, shaped by `cb` if given."""
        if relationship.startswith(SUBQ_PREFIX):
            raise Exception(f'Relationship names may not start with "{SUBQ_PREFIX}". That is a reserved prefix.')
        self._assert_not_distinct("related")
        related = self._relationship(relationship)
        base_key = ("related_base", relationship)
        q = self._find(base_key)
        if len(related) == 1:
            (hop,) = related
            if q is None:
                q = self._keep(
                    base_key,
                    self._new(
                        hop.dest_schema,
                        AST(table=hop.dest_schema, alias=relationship),
                        None,
                        _ONE if hop.cardinality == "one" else _MANY,
                    ),
                )
            sub = _as_query(cb(q) if cb else q)
            _assert_subquery_not_grouped(sub, "related")
            check(len(hop.source_field) >= 1, "The source of a relationship must specify at last 1 field")
            check(len(hop.dest_field) >= 1, "The destination of a relationship must specify at last 1 field")
            check(
                len(hop.source_field) == len(hop.dest_field),
                "The source and destination of a relationship must have the same number of fields",
            )
            # A `related` under an aggregate reads as a junction's destination, so a direct one cannot nest one.
            aggregate = sub._format.aggregate
            check(
                aggregate is None or sub._ast.related is None,
                "an aggregate relationship does not support related() on its subquery",
            )
            found = self._find(("related", relationship, id(sub._ast)))
            if found is not None:
                return found
            entry = CorrelatedSubquery(
                system=self._system,
                correlation=Correlation(parent_field=hop.source_field, child_field=hop.dest_field),
                # The aggregate lives on this entry, so the subquery does not reduce itself too.
                subquery=sub._ast if aggregate is None else _without_aggregate(sub._ast),
                source=sub,
                aggregate=aggregate,
            )
            return self._with_relationship(relationship, sub, entry)

        first, second = related
        if q is None:
            q = self._keep(
                base_key,
                self._new(
                    second.dest_schema,
                    AST(table=second.dest_schema, alias=relationship),
                    relationship,
                    _ONE if second.cardinality == "one" else _MANY,
                ),
            )
        sub = _as_query(cb(q) if cb else q)
        _assert_subquery_not_grouped(sub, "related")
        _check_keys(first, second)
        found = self._find(("related", relationship, id(sub._ast)))
        if found is not None:
            return found
        aggregate = sub._format.aggregate
        if aggregate is not None:
            return self._with_relationship(relationship, sub, self._junction_aggregate(relationship, sub, aggregate))
        entry = CorrelatedSubquery(
            system=self._system,
            correlation=Correlation(parent_field=first.source_field, child_field=first.dest_field),
            hidden=True,
            source=sub,
            subquery=AST(
                table=first.dest_schema,
                alias=relationship,
                related=(
                    CorrelatedSubquery(
                        system=self._system,
                        correlation=Correlation(parent_field=second.source_field, child_field=second.dest_field),
                        subquery=sub._ast,
                        source=sub,
                    ),
                ),
            ),
        )
        return self._with_relationship(relationship, sub, entry)

    def _junction_aggregate(self, relationship: str, sub: AnyQuery, aggregate: Aggregate) -> CorrelatedSubquery:
        """The entry of an aggregate over a junction relationship. A `where` on the destination applies;
        nesting and bounding it do not yet."""
        first, second = self._relationship(relationship)
        dest = sub._ast
        check(
            dest.related is None and dest.limit is None and dest.start is None,
            "an aggregate over a junction relationship does not yet support related/limit/start on the destination",
        )
        correlation = Correlation(parent_field=first.source_field, child_field=first.dest_field)
        to_dest = Correlation(parent_field=second.source_field, child_field=second.dest_field)
        dest = _without_aggregate(dest)
        if aggregate.fn == "count":
            # A count of the junction rows whose destination exists and passes its `where`, which is also where
            # the server applies the destination's read permissions. The destination is never materialized.
            exists = CorrelatedSubqueryCondition(
                related=CorrelatedSubquery(correlation=to_dest, subquery=dest, system=self._system, source=sub),
                op="EXISTS",
            )
            return CorrelatedSubquery(
                correlation=correlation,
                subquery=AST(table=first.dest_schema, alias=relationship, where=exists),
                system=self._system,
                aggregate=Aggregate(fn="count"),
                source=sub,
            )
        # The other functions need the destination's column, one hop past the junction.
        return CorrelatedSubquery(
            correlation=correlation,
            subquery=AST(
                table=first.dest_schema,
                alias=relationship,
                related=(CorrelatedSubquery(correlation=to_dest, subquery=dest, system=self._system, source=sub),),
            ),
            system=self._system,
            aggregate=aggregate,
            source=sub,
        )

    def start[Q: Query[Any, Any]](self: Q, row: Mapping[str, Any], *, inclusive: bool = False) -> Q:
        self._assert_not_aggregated("start")
        self._assert_not_distinct("start")
        # Upstream stores any row as given: one that is not a mapping only fails once it is used.
        if isinstance(row, Mapping):
            for column in row:
                self._check_column(column)
            row = dict(row)
        return self._derive(
            ("start", _tag(inclusive), _tag(row)), lambda: self._with("start", Bound(row=row, exclusive=not inclusive))
        )

    def limit[Q: Query[Any, Any]](self: Q, limit: int | float) -> Q:
        self._assert_not_aggregated("limit")
        self._assert_not_distinct("limit")
        if not (type(limit) is int and 0 <= limit < 2**31):  # The usual limit passes both checks.
            if _js_negative(limit):
                raise Exception("Limit must be non-negative")
            # `(limit | 0) !== limit`: only a number that is already a 32-bit integer survives.
            if not (_is_number(limit) and math.isfinite(limit) and limit == int(limit) and -(2**31) <= limit < 2**31):
                raise Exception("Limit must be an integer")
        if self._junction:
            raise NotImplementedError(
                "Limit is not supported in junction relationships yet. Junction relationship being limited: "
                + self._junction
            )
        return self._derive(("limit", _tag(limit)), lambda: self._with("limit", limit))

    def order_by[Q: Query[Any, Any]](self: Q, field: C, direction: Direction) -> Q:
        if self._strict:  # python-only: strict mode
            self._check_column(field)
        if self._junction:
            raise NotImplementedError(
                "Order by is not supported in junction relationships yet. Junction relationship being ordered: "
                + self._junction
            )
        if self._ast.group_by:
            _assert_group_column(field, self._ast.group_by)
        order_by = self._ast.order_by
        return self._derive(
            ("order_by", _tag(field), _tag(direction)),
            lambda: self._with("order_by", (*order_by, (field, direction)) if order_by else ((field, direction),)),
        )

    def _exists(
        self,
        relationship: str,
        cb: SubqueryFn | None,
        *,
        flip: bool | None = None,
        scalar: bool | None = None,
    ) -> Condition:
        related = self._relationship(relationship)
        base_key = ("exists_base", relationship)
        q = self._find(base_key)
        if len(related) == 1:
            (hop,) = related
            check(hop.source_field and hop.dest_field, "Invalid relationship")
            if q is None:
                q = self._keep(
                    base_key,
                    self._new(hop.dest_schema, AST(table=hop.dest_schema, alias=f"{SUBQ_PREFIX}{relationship}"), None),
                )
            sub = _as_query(cb(q) if cb else q)
            _assert_exists_subquery(sub)
            return CorrelatedSubqueryCondition(
                related=CorrelatedSubquery(
                    system=self._system,
                    correlation=Correlation(parent_field=hop.source_field, child_field=hop.dest_field),
                    subquery=sub._ast,
                    source=sub,
                ),
                op="EXISTS",
                flip=flip,
                scalar=scalar,
            )

        first, second = related
        _check_keys(first, second)
        if q is None:
            alias = f"{SUBQ_PREFIX}zhidden_{relationship}"
            q = self._keep(
                base_key, self._new(second.dest_schema, AST(table=second.dest_schema, alias=alias), relationship)
            )
        to_dest = _as_query(cb(q) if cb else q)
        _assert_exists_subquery(to_dest)
        return CorrelatedSubqueryCondition(
            related=CorrelatedSubquery(
                system=self._system,
                correlation=Correlation(parent_field=first.source_field, child_field=first.dest_field),
                # The junction AST is built here, new every time; the query it leads to stands for it.
                source=to_dest,
                subquery=AST(
                    table=first.dest_schema,
                    alias=f"{SUBQ_PREFIX}{relationship}",
                    # A single condition, which needs no normalizing.
                    where=CorrelatedSubqueryCondition(
                        related=CorrelatedSubquery(
                            system=self._system,
                            correlation=Correlation(parent_field=second.source_field, child_field=second.dest_field),
                            subquery=to_dest._ast,
                            source=to_dest,
                        ),
                        op="EXISTS",
                        flip=flip,
                        scalar=scalar,
                    ),
                ),
            ),
            op="EXISTS",
            flip=flip,
        )

    def expression_builder(self) -> ExpressionBuilder:
        return ExpressionBuilder(self._exists, self._check_column if self._strict else None)


class OneQuery(Query[R, C]):
    """A query `one()` narrowed to a single row. Its methods keep it singular."""


class GroupedQuery:
    """`query.group_by(*columns)`, waiting for its aggregate. Each is a query derived from `query`."""

    __slots__ = ("_columns", "_query")

    def __init__(self, query: AnyQuery, columns: tuple[str, ...]) -> None:
        self._query = query
        self._columns = columns

    def count(self) -> AnyQuery:
        """One row per group: its columns and `count`, the number of rows in it."""
        return self._query._group_aggregate(self._columns, "count", None)

    def sum(self, selector: Any = None) -> AnyQuery:
        return self._query._group_aggregate(self._columns, "sum", selector)

    def avg(self, selector: Any = None) -> AnyQuery:
        return self._query._group_aggregate(self._columns, "avg", selector)

    def min(self, selector: Any = None) -> AnyQuery:
        return self._query._group_aggregate(self._columns, "min", selector)

    def max(self, selector: Any = None) -> AnyQuery:
        return self._query._group_aggregate(self._columns, "max", selector)


def _without_aggregate(ast: AST) -> AST:
    out = ast._copy()
    out.aggregate = None
    return out


def _assert_group_column(column: Any, columns: Sequence[Any]) -> None:
    """The rows of a `distinct()` or `groupBy()` can only be ordered by its columns."""
    if column not in columns:
        names = ", ".join(js_stringify(c) for c in columns)
        raise Exception(
            f'orderBy("{js_string(column)}"): the rows can only be ordered by the grouped columns ({names})'
        )


def _assert_subquery_not_grouped(sub: AnyQuery, method: str) -> None:
    group_by, aggregate = sub._ast.group_by, sub._ast.aggregate
    if group_by or (aggregate is not None and aggregate.distinct):
        kind = "groupBy()" if group_by and aggregate is not None else "distinct()"
        raise Exception(f"{method}() does not support a {kind} subquery yet")


def _assert_exists_subquery(sub: AnyQuery) -> None:
    check(sub._ast.aggregate is None, "exists() does not support an aggregate subquery")
    _assert_subquery_not_grouped(sub, "exists")


_SCALAR_TYPES = (str, int, bool, type(None))


def _tag(value: object) -> Any:
    """`value` as an exact, hashable key.

    Each part carries its type, so `1`, `1.0`, `True` and `"1"` stay apart, and a float is spelled
    exactly, so `0.0` and `-0.0` do too. A dict keeps its order, which its JSON has.
    """
    kind = type(value)
    if kind in _SCALAR_TYPES:
        return kind, value
    if kind is float:
        return kind, value.hex()  # ty: ignore[unresolved-attribute]
    if kind is list or kind is tuple:
        return kind, tuple([_tag(v) for v in value])  # ty: ignore[not-iterable]
    if kind is dict:
        return kind, tuple([(_tag(k), _tag(v)) for k, v in value.items()])  # ty: ignore[unresolved-attribute]
    # The query keyed by it holds the value in its AST, so the identity is not reused while the key is.
    return object, id(value)  # python-only: a value of no JSON type, which only Python code passes


def _position_key(position: object) -> Any:
    kind = type(position)
    if kind is ColumnReference:
        return kind, _tag(position.name)  # ty: ignore[unresolved-attribute]
    if kind is JsonPathReference:
        return kind, _tag(position.value.name), _tag(position.path)  # ty: ignore[unresolved-attribute]
    if kind is LiteralReference:
        return kind, _tag(position.value)  # ty: ignore[unresolved-attribute]
    return kind, _tag(position.anchor), _tag(position.field)  # ty: ignore[unresolved-attribute]


def _condition_key(condition: Condition) -> Any:
    """`condition` as an exact key. A subquery is named by the identity of its AST, which the condition
    keeps alive (`CorrelatedSubquery.source`)."""
    kind = type(condition)
    if kind is SimpleCondition:
        left, right, op = condition.left, condition.right, condition.op  # ty: ignore[unresolved-attribute]
        # A column compared with a scalar, by far the usual condition, keyed flat: one tuple, not five.
        if type(left) is ColumnReference and type(right) is LiteralReference and type(op) is str:
            name, value = left.name, right.value
            if type(name) is str and type(value) in _SCALAR_TYPES:
                return name, op, type(value), value
        return (
            kind,
            _position_key(condition.left),  # ty: ignore[unresolved-attribute]
            _tag(condition.op),  # ty: ignore[unresolved-attribute]
            _position_key(condition.right),  # ty: ignore[unresolved-attribute]
        )
    if kind is CorrelatedSubqueryCondition:
        related: CorrelatedSubquery = condition.related  # ty: ignore[unresolved-attribute]
        correlation = related.correlation
        return (
            kind,
            _tag(correlation.parent_field),
            _tag(correlation.child_field),
            _tag(related.hidden),
            _tag(related.system),
            _subquery_key(related),
            _tag(condition.op),  # ty: ignore[unresolved-attribute]
            _tag(condition.flip),  # ty: ignore[unresolved-attribute]
            _tag(condition.scalar),  # ty: ignore[unresolved-attribute]
        )
    return kind, tuple([_condition_key(c) for c in condition.conditions])  # ty: ignore[unresolved-attribute]


def _subquery_key(related: CorrelatedSubquery) -> Any:
    subquery = related.subquery
    # A condition the builder did not make has no source, and its subquery stands for itself.
    source = getattr(related.source, "_ast", subquery)
    # A two-hop `exists` builds its junction AST new every time, so it is keyed by its condition,
    # which names the query it leads to.
    return id(subquery) if source is subquery else _condition_key(subquery.where)  # ty: ignore[invalid-argument-type]


def _check_keys(first: Connection, second: Connection) -> None:
    check(first.source_field and first.dest_field and second.source_field and second.dest_field, "Invalid relationship")


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


_JS_NEGATIVE = re.compile(r"-(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?|-Infinity")


def _js_negative(value: object) -> bool:
    """JavaScript's `value < 0`, which first turns a string, or an array by way of its string, into a number."""
    if isinstance(value, int | float):
        return value < 0
    if isinstance(value, str | list):
        text = js_string(value).strip()
        return _JS_NEGATIVE.fullmatch(text) is not None and float(text.replace("Infinity", "inf")) < 0
    return False


def as_query_internals(q: object) -> AnyQuery:
    """`q` as a query, or the error upstream's `asQueryInternals` throws for anything else.

    Ported from `packages/zql/src/query/query-internals.ts`.
    """
    js_check_in("Symbol()", q)
    check(
        isinstance(q, Query),
        "Query does not implement QueryInternals. This likely means there are two copies of Zero in your runtime.",
    )
    assert isinstance(q, Query)
    return q


def mapped_ast(query: AnyQuery, mapper: NameMapper) -> AST:
    """`map_ast(query.ast, mapper)`, kept on the query: an interned query comes back request after request."""
    mapped = query._mapped
    if mapped is not None and mapped[0] is mapper:
        return mapped[1]
    ast = map_ast(query._ast, mapper)
    query._mapped = (mapper, ast)
    return ast


def _as_query(q: object) -> AnyQuery:
    check(isinstance(q, Query), "Expected QueryImpl instance")
    assert isinstance(q, Query)
    return q


class Builder:
    """`create_builder(schema).issue` starts a query on the `issue` table.

    With `strict=True`, a column the table does not have is refused when the query is built, as
    `UnknownColumnError`. Upstream refuses it only when the query endpoint maps the AST, failing the
    whole batch, so strict mode is for catching mistakes in your own tests rather than for production
    parity.
    """

    def __init__(self, schema: Schema, *, strict: bool = False) -> None:
        self._schema = schema
        self._strict = strict
        # Every query this builder makes descends from one of these, so they hold the interned queries.
        self._roots: dict[str, Query[Any, Any]] = {}

    def __getattr__(self, table: str) -> Query[Any, Any]:
        return self.table(table)

    def table(self, name: str) -> Query[Any, Any]:
        root = self._roots.get(name)
        if root is None:
            if name not in self._schema.tables:  # python-only: upstream's builder has `undefined` for it instead
                raise AttributeError(name)
            root = self._roots[name] = Query(self._schema, name, AST(table=name), strict=self._strict)
        return root


def create_builder(schema: Schema, *, strict: bool = False) -> Builder:
    return Builder(schema, strict=strict)


__all__ = ["MISSING", "Builder", "GroupedQuery", "OneQuery", "Query", "UnknownColumnError", "create_builder"]
