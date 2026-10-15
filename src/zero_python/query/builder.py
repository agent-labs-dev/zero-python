"""The ZQL query builder: a `Query` is an immutable AST under construction, in client names.

Ported from rocicorp/mono `packages/zql/src/query/query-impl.ts` and `create-builder.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.

The errors match upstream's message for message, because a query endpoint reports them to the client.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from zero_python.protocol.ast import (
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
from zero_python.query.expression import MISSING, ExpressionBuilder, and_, cmp, simplify_condition
from zero_python.schema import Connection, Schema
from zero_python.shared.asserts import check


@dataclass(frozen=True)
class Format:
    """The shape of a query's result: a row or a list, and the same for each relationship."""

    singular: bool = False
    relationships: dict[str, "Format"] = field(default_factory=dict)


class Query:
    def __init__(
        self,
        schema: Schema,
        table: str,
        ast: AST,
        system: System = "client",
        current_junction: str | None = None,
        format: Format | None = None,
    ) -> None:
        self._schema = schema
        self._table = table
        self._ast = ast
        self._system: System = system
        self._junction = current_junction
        self._format = format or Format()

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

    def _new(self, table: str, ast: AST, junction: str | None, format: Format | None = None) -> "Query":
        return Query(self._schema, table, ast, self._system, junction, format)

    def _with(self, format: Format | None = None, **update: Any) -> "Query":
        return self._new(self._table, self._ast.model_copy(update=update), self._junction, format or self._format)

    def _with_relationship(self, name: str, sub: "Query", entry: CorrelatedSubquery) -> "Query":
        format = replace(self._format, relationships={**self._format.relationships, name: sub.format})
        return self._with(format=format, related=(*(self._ast.related or ()), entry))

    def one(self) -> "Query":
        return self._with(format=replace(self._format, singular=True), limit=1)

    def where(self, field_or_factory: str | Callable[[ExpressionBuilder], Condition], *args: Any) -> "Query":
        """`where(field, value)`, `where(field, op, value)`, or `where(lambda eb: ...)`."""
        cond: Condition
        if isinstance(field_or_factory, str):
            check(len(args) >= 1, "Invalid condition. Too few arguments.")
            cond = cmp(field_or_factory, args[0]) if len(args) == 1 else cmp(field_or_factory, args[0], args[1])
        else:
            cond = field_or_factory(self.expression_builder())
        if self._ast.where is not None:
            cond = and_(self._ast.where, cond)
        return self._with(where=simplify_condition(cond))

    def where_exists(
        self,
        relationship: str,
        cb: Callable[["Query"], "Query"] | None = None,
        *,
        flip: bool | None = None,
        scalar: bool | None = None,
    ) -> "Query":
        return self.where(lambda eb: eb.exists(relationship, cb, flip=flip, scalar=scalar))

    def _relationship(self, relationship: str) -> tuple[Connection, ...]:
        by_table = self._schema.relationships.get(self._table)
        if by_table is None:
            raise TypeError(f"Cannot read properties of undefined (reading '{relationship}')")
        related = by_table.get(relationship)
        check(related, "Invalid relationship")
        assert related is not None
        return related

    def related(self, relationship: str, cb: Callable[["Query"], "Query"] | None = None) -> "Query":
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

    def start(self, row: Mapping[str, Any], *, inclusive: bool = False) -> "Query":
        return self._with(start=Bound(row=dict(row), exclusive=not inclusive))

    def limit(self, limit: int | float) -> "Query":
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

    def order_by(self, field: str, direction: Direction) -> "Query":
        if self._junction:
            raise NotImplementedError(
                "Order by is not supported in junction relationships yet. Junction relationship being ordered: "
                + self._junction
            )
        return self._with(order_by=(*(self._ast.order_by or ()), (field, direction)))

    def _exists(
        self,
        relationship: str,
        cb: Callable[["Query"], "Query"] | None,
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
        return ExpressionBuilder(self._exists)


def _as_query(q: object) -> Query:
    check(isinstance(q, Query), "Expected QueryImpl instance")
    assert isinstance(q, Query)
    return q


class Builder:
    """`create_builder(schema).issue` starts a query on the `issue` table."""

    def __init__(self, schema: Schema) -> None:
        self._schema = schema

    def __getattr__(self, table: str) -> Query:
        return self.table(table)

    def table(self, name: str) -> Query:
        if name not in self._schema.tables:
            raise AttributeError(name)
        return Query(self._schema, name, AST(table=name))


def create_builder(schema: Schema) -> Builder:
    return Builder(schema)


__all__ = ["MISSING", "Builder", "Query", "create_builder"]
