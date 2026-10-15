"""The AST's wire schema: what `AST.from_json` accepts, mirroring upstream's `astSchema`.

Ported from rocicorp/mono `packages/zero-protocol/src/ast.ts`, `data.ts` and `primary-key.ts` at
zero/v1.10.0 (905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.

These models are only ever parsed. Each turns into the plain node class of `zero_server.protocol.ast`
it mirrors, which the builder, the query endpoint and z2s work with.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictStr

from zero_server.protocol import ast
from zero_server.protocol.wire import Number, Omittable, PlainJSON, StrictWireModel

type _Direction = Literal["asc", "desc"]
type _Ordering = tuple[tuple[StrictStr, _Direction], ...]
type _Scalar = StrictStr | Number | StrictBool
type _LiteralValue = _Scalar | list[_Scalar] | None
type _CompoundKey = Annotated[tuple[StrictStr, ...], Field(min_length=1)]


class LiteralReference(StrictWireModel):
    type: Literal["literal"] = "literal"
    value: _LiteralValue

    def node(self) -> ast.LiteralReference:
        return ast.LiteralReference(value=self.value)


class ColumnReference(StrictWireModel):
    type: Literal["column"] = "column"
    name: StrictStr

    def node(self) -> ast.ColumnReference:
        return ast.ColumnReference(name=self.name)


class Parameter(StrictWireModel):
    type: Literal["static"] = "static"
    anchor: Literal["authData", "preMutationRow"]
    field: StrictStr | tuple[StrictStr, ...]

    def node(self) -> ast.Parameter:
        return ast.Parameter(anchor=self.anchor, field=self.field)


type _ValuePosition = Annotated[LiteralReference | ColumnReference | Parameter, Field(discriminator="type")]


class SimpleCondition(StrictWireModel):
    type: Literal["simple"] = "simple"
    left: _ValuePosition
    right: Annotated[Parameter | LiteralReference, Field(discriminator="type")]
    op: ast.SimpleOperator

    def node(self) -> ast.SimpleCondition:
        return ast.SimpleCondition(op=self.op, left=self.left.node(), right=self.right.node())


class Conjunction(StrictWireModel):
    type: Literal["and"] = "and"
    conditions: tuple[_Condition, ...]

    def node(self) -> ast.Conjunction:
        return ast.Conjunction(conditions=tuple(c.node() for c in self.conditions))


class Disjunction(StrictWireModel):
    type: Literal["or"] = "or"
    conditions: tuple[_Condition, ...]

    def node(self) -> ast.Disjunction:
        return ast.Disjunction(conditions=tuple(c.node() for c in self.conditions))


class CorrelatedSubqueryCondition(StrictWireModel):
    type: Literal["correlatedSubquery"] = "correlatedSubquery"
    related: CorrelatedSubquery
    op: ast.CorrelatedSubqueryConditionOperator
    flip: Omittable[StrictBool] = None
    scalar: Omittable[StrictBool] = None

    def node(self) -> ast.CorrelatedSubqueryCondition:
        return ast.CorrelatedSubqueryCondition(
            related=self.related.node(), op=self.op, flip=self.flip, scalar=self.scalar
        )


type _Condition = Annotated[
    SimpleCondition | Conjunction | Disjunction | CorrelatedSubqueryCondition, Field(discriminator="type")
]


class Correlation(StrictWireModel):
    parent_field: _CompoundKey = Field(alias="parentField")
    child_field: _CompoundKey = Field(alias="childField")


class CorrelatedSubquery(StrictWireModel):
    correlation: Correlation
    hidden: Omittable[StrictBool] = None
    subquery: AST
    system: Omittable[ast.System] = None

    def node(self) -> ast.CorrelatedSubquery:
        return ast.CorrelatedSubquery(
            correlation=ast.Correlation(
                parent_field=self.correlation.parent_field, child_field=self.correlation.child_field
            ),
            hidden=self.hidden,
            subquery=self.subquery.node(),
            system=self.system,
        )


class Bound(StrictWireModel):
    row: PlainJSON[dict[str, ast.Value]]
    exclusive: StrictBool


class AST(StrictWireModel):
    schema_: Omittable[StrictStr] = Field(default=None, alias="schema")
    table: StrictStr
    alias: Omittable[StrictStr] = None
    where: Omittable[_Condition] = None
    related: Omittable[tuple[CorrelatedSubquery, ...]] = None
    start: Omittable[Bound] = None
    limit: Omittable[Number] = None
    order_by: Omittable[_Ordering] = Field(default=None, alias="orderBy")

    def node(self) -> ast.AST:
        return ast.AST(
            schema_=self.schema_,
            table=self.table,
            alias=self.alias,
            where=self.where.node() if self.where is not None else None,
            related=tuple(r.node() for r in self.related) if self.related is not None else None,
            start=ast.Bound(row=self.start.row, exclusive=self.start.exclusive) if self.start is not None else None,
            limit=self.limit,
            order_by=self.order_by,
        )


for _model in (Conjunction, Disjunction, CorrelatedSubqueryCondition, CorrelatedSubquery, AST):
    _model.model_rebuild()
