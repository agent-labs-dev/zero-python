"""The ZQL query builder: a `Query` is an immutable AST under construction, in client names.

Ported from rocicorp/mono `packages/zql/src/query/query-impl.ts` and `create-builder.ts` at zero/v1.11.3
(e91e55f05b7a2c631f77238e1dd6fadf1029a538). Apache-2.0, Copyright Rocicorp.

The errors match upstream's message for message, because a query endpoint reports them to the client.

A query's AST is always normalized, as upstream's is: each step keeps it so, sorting a new `related`
into place and normalizing the conditions of a `where`. Upstream also interns queries, so that
rebuilding the same chain hands back the same objects; that only saves work, and is not ported.
"""

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Generic

from typing_extensions import TypeVar

from zero_server.protocol.ast import (
    AST,
    SUBQ_PREFIX,
    Bound,
    ColumnReference,
    Condition,
    CorrelatedSubquery,
    CorrelatedSubqueryCondition,
    Correlation,
    Direction,
    SimpleCondition,
    System,
    insert_related,
    normalize_condition,
)
from zero_server.query.expression import MISSING, ExpressionBuilder, _position, and_, cmp, simplify_condition
from zero_server.schema import Connection, Schema
from zero_server.shared.asserts import AssertionFailedError, check
from zero_server.shared.js import js_check_in, js_string


@dataclass(frozen=True, slots=True)
class Format:
    """The shape of a query's result: a row or a list, and the same for each relationship."""

    singular: bool = False
    relationships: dict[str, "Format"] = field(default_factory=dict)


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

    __slots__ = ("_ast", "_format", "_junction", "_schema", "_strict", "_system", "_table")

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
        return query

    def _with[Q: Query[Any, Any]](self: Q, field: str, value: Any, format: Format | None = None) -> Q:
        """This query with its AST's `field` set to `value`."""
        ast = self._ast._copy()
        setattr(ast, field, value)
        return self._copy(type(self), ast, format or self._format)

    def _with_relationship[Q: Query[Any, Any]](self: Q, name: str, sub: AnyQuery, entry: CorrelatedSubquery) -> Q:
        format = Format(self._format.singular, {**self._format.relationships, name: sub._format})
        return self._with("related", insert_related(self._ast.related, entry), format)

    def one(self) -> "OneQuery[R, C]":
        """At most one row: `tx.run` returns it, or `None`."""
        ast = self._ast._copy()
        ast.limit = 1
        return self._copy(OneQuery, ast, Format(True, self._format.relationships))

    def where[Q: Query[Any, Any]](
        self: Q, field_or_factory: C | Callable[[ExpressionBuilder], Condition], *args: Any
    ) -> Q:
        """`where(field, value)`, `where(field, op, value)`, or `where(lambda eb: ...)`."""
        cond: Condition
        # Anything but a function is a field, as upstream has it: a number fails once the column is looked up.
        if isinstance(field_or_factory, str) or not callable(field_or_factory):
            if self._strict:  # python-only: strict mode
                self._check_column(field_or_factory)
            if len(args) == 1:
                cond = SimpleCondition(op="=", left=ColumnReference(name=field_or_factory), right=_position(args[0]))
            else:
                check(len(args) >= 2, "Invalid condition. Too few arguments.")
                cond = cmp(field_or_factory, args[0], args[1])
        else:
            cond = field_or_factory(self.expression_builder())
        if self._ast.where is not None:
            cond = and_(self._ast.where, cond)
        return self._with("where", normalize_condition(simplify_condition(cond)))

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
        related = self._relationship(relationship)
        if len(related) == 1:
            (hop,) = related
            q = self._new(
                hop.dest_schema,
                AST(table=hop.dest_schema, alias=relationship),
                None,
                _ONE if hop.cardinality == "one" else _MANY,
            )
            sub = _as_query(cb(q) if cb else q)
            check(len(hop.source_field) >= 1, "The source of a relationship must specify at last 1 field")
            check(len(hop.dest_field) >= 1, "The destination of a relationship must specify at last 1 field")
            check(
                len(hop.source_field) == len(hop.dest_field),
                "The source and destination of a relationship must have the same number of fields",
            )
            entry = CorrelatedSubquery(
                system=self._system,
                correlation=Correlation(parent_field=hop.source_field, child_field=hop.dest_field),
                subquery=sub._ast,
            )
            return self._with_relationship(relationship, sub, entry)

        first, second = related
        q = self._new(
            second.dest_schema,
            AST(table=second.dest_schema, alias=relationship),
            relationship,
            _ONE if second.cardinality == "one" else _MANY,
        )
        sub = _as_query(cb(q) if cb else q)
        _check_keys(first, second)
        entry = CorrelatedSubquery(
            system=self._system,
            correlation=Correlation(parent_field=first.source_field, child_field=first.dest_field),
            hidden=True,
            subquery=AST(
                table=first.dest_schema,
                alias=relationship,
                related=(
                    CorrelatedSubquery(
                        system=self._system,
                        correlation=Correlation(parent_field=second.source_field, child_field=second.dest_field),
                        subquery=sub._ast,
                    ),
                ),
            ),
        )
        return self._with_relationship(relationship, sub, entry)

    def start[Q: Query[Any, Any]](self: Q, row: Mapping[str, Any], *, inclusive: bool = False) -> Q:
        # Upstream stores any row as given: one that is not a mapping only fails once it is used.
        if isinstance(row, Mapping):
            for column in row:
                self._check_column(column)
            row = dict(row)
        return self._with("start", Bound(row=row, exclusive=not inclusive))

    def limit[Q: Query[Any, Any]](self: Q, limit: int | float) -> Q:
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
        return self._with("limit", limit)

    def order_by[Q: Query[Any, Any]](self: Q, field: C, direction: Direction) -> Q:
        if self._strict:  # python-only: strict mode
            self._check_column(field)
        if self._junction:
            raise NotImplementedError(
                "Order by is not supported in junction relationships yet. Junction relationship being ordered: "
                + self._junction
            )
        order_by = self._ast.order_by
        return self._with("order_by", (*order_by, (field, direction)) if order_by else ((field, direction),))

    def _exists(
        self,
        relationship: str,
        cb: SubqueryFn | None,
        *,
        flip: bool | None = None,
        scalar: bool | None = None,
    ) -> Condition:
        related = self._relationship(relationship)
        if len(related) == 1:
            (hop,) = related
            check(len(hop.source_field) >= 1, "Invalid relationship")
            check(len(hop.dest_field) >= 1, "Invalid relationship")
            q = self._new(hop.dest_schema, AST(table=hop.dest_schema, alias=f"{SUBQ_PREFIX}{relationship}"), None)
            sub = _as_query(cb(q) if cb else q)
            return CorrelatedSubqueryCondition(
                related=CorrelatedSubquery(
                    system=self._system,
                    correlation=Correlation(parent_field=hop.source_field, child_field=hop.dest_field),
                    subquery=sub._ast,
                ),
                op="EXISTS",
                flip=flip,
                scalar=scalar,
            )

        first, second = related
        _check_keys(first, second)
        alias = f"{SUBQ_PREFIX}zhidden_{relationship}"
        q = self._new(second.dest_schema, AST(table=second.dest_schema, alias=alias), relationship)
        to_dest = _as_query(cb(q) if cb else q)
        return CorrelatedSubqueryCondition(
            related=CorrelatedSubquery(
                system=self._system,
                correlation=Correlation(parent_field=first.source_field, child_field=first.dest_field),
                subquery=AST(
                    table=first.dest_schema,
                    alias=f"{SUBQ_PREFIX}{relationship}",
                    # A single condition, which needs no normalizing.
                    where=CorrelatedSubqueryCondition(
                        related=CorrelatedSubquery(
                            system=self._system,
                            correlation=Correlation(parent_field=second.source_field, child_field=second.dest_field),
                            subquery=to_dest._ast,
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


def _check_keys(first: Connection, second: Connection) -> None:
    for key in (first.source_field, first.dest_field, second.source_field, second.dest_field):
        check(len(key) >= 1, "Invalid relationship")


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

    def __getattr__(self, table: str) -> Query[Any, Any]:
        return self.table(table)

    def table(self, name: str) -> Query[Any, Any]:
        if name not in self._schema.tables:  # python-only: upstream's builder has `undefined` for it instead
            raise AttributeError(name)
        return Query(self._schema, name, AST(table=name), strict=self._strict)


def create_builder(schema: Schema, *, strict: bool = False) -> Builder:
    return Builder(schema, strict=strict)


__all__ = ["MISSING", "Builder", "OneQuery", "Query", "UnknownColumnError", "create_builder"]
