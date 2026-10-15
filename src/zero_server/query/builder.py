"""The ZQL query builder: a `Query` is an immutable AST under construction, in client names.

Ported from rocicorp/mono `packages/zql/src/query/query-impl.ts` and `create-builder.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.

The errors match upstream's message for message, because a query endpoint reports them to the client.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Generic

from typing_extensions import TypeVar

from zero_server.protocol.ast import (
    AST,
    SUBQ_PREFIX,
    Bound,
    Condition,
    CorrelatedSubquery,
    CorrelatedSubqueryCondition,
    Correlation,
    Direction,
    System,
)
from zero_server.query.expression import MISSING, ExpressionBuilder, and_, cmp, simplify_condition
from zero_server.schema import Connection, Schema
from zero_server.shared.asserts import check


@dataclass(frozen=True)
class Format:
    """The shape of a query's result: a row or a list, and the same for each relationship."""

    singular: bool = False
    relationships: dict[str, "Format"] = field(default_factory=dict)


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
        self._format = format or Format()
        self._strict = strict

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._ast.to_json()!r})"

    def _check_column(self, column: str) -> None:
        if self._strict and column not in self._schema.tables[self._table].columns:
            raise UnknownColumnError(f'unknown column "{column}" of "{self._table}" table')

    @property
    def format(self) -> Format:
        return self._format

    @property
    def ast(self) -> AST:
        """The AST in client names. The query endpoint maps it to server names."""
        return self._ast

    @property
    def table(self) -> str:
        return self._table

    def _new(self, table: str, ast: AST, junction: str | None, format: Format | None = None) -> AnyQuery:
        return Query(self._schema, table, ast, self._system, junction, format, strict=self._strict)

    def _with[Q: Query[Any, Any]](self: Q, format: Format | None = None, **update: Any) -> Q:
        return type(self)(
            self._schema,
            self._table,
            self._ast.model_copy(update=update),
            self._system,
            self._junction,
            format or self._format,
            strict=self._strict,
        )

    def _with_relationship[Q: Query[Any, Any]](self: Q, name: str, sub: AnyQuery, entry: CorrelatedSubquery) -> Q:
        format = replace(self._format, relationships={**self._format.relationships, name: sub.format})
        return self._with(format=format, related=(*(self._ast.related or ()), entry))

    def one(self) -> "OneQuery[R, C]":
        """At most one row: `tx.run` returns it, or `None`."""
        query = self._with(format=replace(self._format, singular=True), limit=1)
        return OneQuery(
            query._schema, query._table, query._ast, query._system, query._junction, query._format, strict=self._strict
        )

    def where[Q: Query[Any, Any]](
        self: Q, field_or_factory: C | Callable[[ExpressionBuilder], Condition], *args: Any
    ) -> Q:
        """`where(field, value)`, `where(field, op, value)`, or `where(lambda eb: ...)`."""
        cond: Condition
        if isinstance(field_or_factory, str):
            self._check_column(field_or_factory)
            check(len(args) >= 1, "Invalid condition. Too few arguments.")
            cond = cmp(field_or_factory, args[0]) if len(args) == 1 else cmp(field_or_factory, args[0], args[1])
        else:
            cond = field_or_factory(self.expression_builder())
        if self._ast.where is not None:
            cond = and_(self._ast.where, cond)
        return self._with(where=simplify_condition(cond))

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
        check(related, "Invalid relationship")
        assert related is not None
        return related

    def related[Q: Query[Any, Any]](self: Q, relationship: str, cb: SubqueryFn | None = None) -> Q:
        """Nest the rows of `relationship` under each row, shaped by `cb` if given."""
        if relationship.startswith(SUBQ_PREFIX):
            raise Exception(f'Relationship names may not start with "{SUBQ_PREFIX}". That is a reserved prefix.')
        cb = cb or (lambda q: q)
        related = self._relationship(relationship)
        if len(related) == 1:
            (hop,) = related
            q = self._new(
                hop.dest_schema,
                AST(table=hop.dest_schema, alias=relationship),
                None,
                Format(singular=hop.cardinality == "one"),
            )
            sub = _as_query(cb(q))
            check(len(hop.source_field) >= 1, "The source of a relationship must specify at last 1 field")
            check(len(hop.dest_field) >= 1, "The destination of a relationship must specify at last 1 field")
            check(
                len(hop.source_field) == len(hop.dest_field),
                "The source and destination of a relationship must have the same number of fields",
            )
            entry = CorrelatedSubquery(
                system=self._system,
                correlation=Correlation(parent_field=hop.source_field, child_field=hop.dest_field),
                subquery=sub.ast,
            )
            return self._with_relationship(relationship, sub, entry)

        first, second = related
        q = self._new(
            second.dest_schema,
            AST(table=second.dest_schema, alias=relationship),
            relationship,
            Format(singular=second.cardinality == "one"),
        )
        sub = _as_query(cb(q))
        for key in (first.source_field, first.dest_field, second.source_field, second.dest_field):
            check(len(key) >= 1, "Invalid relationship")
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
                        subquery=sub.ast,
                    ),
                ),
            ),
        )
        return self._with_relationship(relationship, sub, entry)

    def start[Q: Query[Any, Any]](self: Q, row: Mapping[str, Any], *, inclusive: bool = False) -> Q:
        for column in row:
            self._check_column(column)
        return self._with(start=Bound(row=dict(row), exclusive=not inclusive))

    def limit[Q: Query[Any, Any]](self: Q, limit: int | float) -> Q:
        if limit < 0:
            raise Exception("Limit must be non-negative")
        if not (isinstance(limit, int | float) and limit == int(limit) and -(2**31) <= limit < 2**31):
            raise Exception("Limit must be an integer")
        if self._junction:
            raise NotImplementedError(
                "Limit is not supported in junction relationships yet. Junction relationship being limited: "
                + self._junction
            )
        return self._with(limit=limit)

    def order_by[Q: Query[Any, Any]](self: Q, field: C, direction: Direction) -> Q:
        self._check_column(field)
        if self._junction:
            raise NotImplementedError(
                "Order by is not supported in junction relationships yet. Junction relationship being ordered: "
                + self._junction
            )
        return self._with(order_by=(*(self._ast.order_by or ()), (field, direction)))

    def _exists(
        self,
        relationship: str,
        cb: SubqueryFn | None,
        *,
        flip: bool | None = None,
        scalar: bool | None = None,
    ) -> Condition:
        cb = cb or (lambda q: q)
        related = self._relationship(relationship)
        if len(related) == 1:
            (hop,) = related
            check(len(hop.source_field) >= 1, "Invalid relationship")
            check(len(hop.dest_field) >= 1, "Invalid relationship")
            sub = _as_query(
                cb(self._new(hop.dest_schema, AST(table=hop.dest_schema, alias=f"{SUBQ_PREFIX}{relationship}"), None))
            )
            return CorrelatedSubqueryCondition(
                related=CorrelatedSubquery(
                    system=self._system,
                    correlation=Correlation(parent_field=hop.source_field, child_field=hop.dest_field),
                    subquery=sub.ast,
                ),
                op="EXISTS",
                flip=flip,
                scalar=scalar,
            )

        first, second = related
        for key in (first.source_field, first.dest_field, second.source_field, second.dest_field):
            check(len(key) >= 1, "Invalid relationship")
        to_dest = _as_query(
            cb(
                self._new(
                    second.dest_schema,
                    AST(table=second.dest_schema, alias=f"{SUBQ_PREFIX}zhidden_{relationship}"),
                    relationship,
                )
            )
        )
        return CorrelatedSubqueryCondition(
            related=CorrelatedSubquery(
                system=self._system,
                correlation=Correlation(parent_field=first.source_field, child_field=first.dest_field),
                subquery=AST(
                    table=first.dest_schema,
                    alias=f"{SUBQ_PREFIX}{relationship}",
                    where=CorrelatedSubqueryCondition(
                        related=CorrelatedSubquery(
                            system=self._system,
                            correlation=Correlation(parent_field=second.source_field, child_field=second.dest_field),
                            subquery=to_dest.ast,
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
        return ExpressionBuilder(self._exists, self._check_column)


class OneQuery(Query[R, C]):
    """A query `one()` narrowed to a single row. Its methods keep it singular."""


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
        if name not in self._schema.tables:
            raise AttributeError(name)
        return Query(self._schema, name, AST(table=name), strict=self._strict)


def create_builder(schema: Schema, *, strict: bool = False) -> Builder:
    return Builder(schema, strict=strict)


__all__ = ["MISSING", "Builder", "OneQuery", "Query", "UnknownColumnError", "create_builder"]
