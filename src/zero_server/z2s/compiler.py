"""A ZQL AST compiled to one Postgres statement that returns the whole result as JSON.

Ported from rocicorp/mono `packages/z2s/src/compiler.ts` and `packages/zql/src/query/complete-ordering.ts`
at zero/v1.10.0 (905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.

Relationships nest as `json_agg` subqueries, so the statement returns one row whose `zql_result` column
holds the rows the client-side query would have produced, in client names.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from zero_server.protocol.ast import (
    AST,
    Bound,
    ColumnReference,
    Condition,
    Conjunction,
    CorrelatedSubquery,
    CorrelatedSubqueryCondition,
    Correlation,
    Disjunction,
    LiteralReference,
    Ordering,
    Parameter,
    Row,
    SimpleCondition,
    Value,
)
from zero_server.query.builder import Format
from zero_server.schema import NameMapper, Schema, TableSchema, client_to_server
from zero_server.server.server_schema import ServerColumnSchema, ServerSchema
from zero_server.shared.asserts import AssertionFailedError, check
from zero_server.shared.js import js_string
from zero_server.z2s.sql import Params, ident, literal_type

ZQL_RESULT_KEY = "zql_result"
_ROOT = "zql_root"
_STATIC = "Static parameters must be bound to a value before compiling to SQL"
_NOT_AN_ARG = "JsonPackedFormat can only take JsonPackArgs."


def complete_ordering(ast: AST, primary_key: Callable[[str], Sequence[str]]) -> AST:
    """Append each table's primary key to its ordering, so every result has one total order."""
    pk = primary_key(ast.table)
    check(pk is not None, "Unexpected undefined value")
    update: dict[str, object] = {"order_by": _add_primary_keys(pk, ast.order_by)}
    if ast.related is not None:
        update["related"] = tuple(
            r.model_copy(update={"subquery": complete_ordering(r.subquery, primary_key)}) for r in ast.related
        )
    if ast.where is not None:
        update["where"] = _complete_condition(ast.where, primary_key)
    return ast.model_copy(update=update)


def _complete_condition(condition: Condition, primary_key: Callable[[str], Sequence[str]]) -> Condition:
    match condition:
        case SimpleCondition():
            return condition
        case CorrelatedSubqueryCondition():
            related = condition.related.model_copy(
                update={"subquery": complete_ordering(condition.related.subquery, primary_key)}
            )
            return condition.model_copy(update={"related": related})
        case _:
            return condition.model_copy(
                update={"conditions": tuple(_complete_condition(c, primary_key) for c in condition.conditions)}
            )


def _add_primary_keys(primary_key: Sequence[str], order_by: Ordering | None) -> Ordering:
    order_by = order_by or ()
    ordered = {field for field, _ in order_by}
    missing = [key for key in dict.fromkeys(primary_key) if key not in ordered]
    return (*order_by, *((key, "asc") for key in missing))


@dataclass(frozen=True)
class _Table:
    zql: str
    alias: str


@dataclass(frozen=True)
class _Column:
    table: _Table
    zql: str


@dataclass(frozen=True)
class _JunctionTable:
    table: _Table
    correlation: Correlation
    limit: int | float | None


type _Correlate = Callable[[_Table], str]


def compile_query(
    server_schema: ServerSchema, schema: Schema, ast: AST, format: Format | None = None
) -> tuple[str, list[str | None]]:
    """The SQL text and its bound values. The statement returns the result in column `zql_result`."""
    compiler = _Compiler(server_schema, schema)
    ast = complete_ordering(ast, lambda table: schema.tables[table].primary_key)
    body = compiler.select(ast, format)
    text = (
        f"SELECT {_to_json(_ROOT, format.singular if format else False)}::text AS {ident(ZQL_RESULT_KEY)} "
        f"FROM ({body}) {ident(_ROOT)}"
    )
    if compiler.params.failure is not None:
        raise compiler.params.failure
    return text, compiler.params.values


def _to_json(table: str, singular: bool) -> str:
    if singular:
        return f"(row_to_json({ident(table)}))"
    return f"COALESCE(json_agg(row_to_json({ident(table)})), '[]'::json)"


def limit_clause(params: Params, limit: int | float | None, singular: bool) -> str:
    if limit == 0:
        return "LIMIT 0"
    if singular:
        return "LIMIT 1"
    if limit is None:
        return ""
    return f"LIMIT {params.literal(limit)}"


class _Compiler:
    def __init__(self, server_schema: ServerSchema, schema: Schema) -> None:
        self.server_schema = server_schema
        self.mapper: NameMapper = client_to_server(schema.tables)
        self.tables: dict[str, TableSchema] = schema.tables
        self.alias_count = 0
        self.params = Params()

    def _table(self, zql: str) -> _Table:
        alias = f"{zql}_{self.alias_count}"
        self.alias_count += 1
        return _Table(zql=zql, alias=alias)

    def _col(self, column: _Column) -> str:
        return ident(column.table.alias, self.mapper.column_name(column.table.zql, column.zql))

    def _from(self, table: _Table) -> str:
        return f"{ident(self.mapper.table_name(table.zql))} AS {ident(table.alias)}"

    def _server_column(self, table: _Table, zql_column: str) -> ServerColumnSchema:
        # The schema check on the first transaction guarantees the column exists.
        return self.server_schema[self.mapper.table_name(table.zql)][self.mapper.column_name(table.zql, zql_column)]

    def _select_ident(self, column: _Column) -> str:
        server = self._server_column(column.table, column.zql)
        if not server.is_enum and server.type in _DATE_TIME_TYPES:
            normalize = server.type in ("timestamptz", "timetz")

            def to_ms(epoch: str) -> str:
                # EXTRACT(EPOCH FROM timetz) is negative when the offset is positive; wrap to 0..86400000.
                return f"(({epoch})::bigint + 86400000) % 86400000" if normalize else epoch

            col = self._col(column)
            if server.is_array:
                return (
                    f"CASE WHEN {col} IS NULL THEN NULL ELSE "
                    f"ARRAY(SELECT {to_ms(f'EXTRACT(EPOCH FROM unnest({col})) * 1000')}) END as {ident(column.zql)}"
                )
            return f"{to_ms(f'EXTRACT(EPOCH FROM {col}) * 1000')} as {ident(column.zql)}"
        return f"{self._col(column)} as {ident(column.zql)}"

    def select(self, ast: AST, format: Format | None, correlate: _Correlate | None = None) -> str:
        table = self._table(ast.table)
        selection = self._related(ast.related or (), format, table)
        used = {r.subquery.alias or "" for r in ast.related or ()}
        selection += [
            self._select_ident(_Column(table, column))
            for column in self.tables[ast.table].columns
            if column not in used
        ]

        clauses: list[str] = []
        if ast.where is not None:
            clauses.append(self._where(ast.where, table))
        if ast.start is not None:
            clauses.append(self._start(ast.start, ast.order_by, table))
        if correlate is not None:
            clauses.append(correlate(table))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return (
            f"SELECT {','.join(selection)} FROM {self._from(table)} {where} "
            f"{self._order_by(ast.order_by, table)} "
            f"{limit_clause(self.params, ast.limit, format.singular if format else False)}"
        )

    def _order_by(self, order_by: Ordering | None, table: _Table) -> str:
        # Never empty: `complete_ordering` has appended the primary key.
        assert order_by
        return "ORDER BY " + ", ".join(
            f"{self._col(_Column(table, col))} ASC NULLS FIRST"
            if direction == "asc"
            else f"{self._col(_Column(table, col))} DESC NULLS LAST"
            for col, direction in order_by
        )

    def _start(self, bound: Bound, order_by: Ordering | None, table: _Table) -> str:
        check(order_by is not None and len(order_by) > 0, "start requires ordering")
        assert order_by is not None
        constraints: list[str] = []
        for i, (i_field, i_direction) in enumerate(order_by):
            group = [self._start_equality(table, field, _row_value(bound.row, field)) for field, _ in order_by[:i]]
            group.append(
                self._start_range(table, i_field, _row_value(bound.row, i_field), ">" if i_direction == "asc" else "<")
            )
            constraints.append(f"({' AND '.join(group)})")
        if not bound.exclusive:
            equal = [self._start_equality(table, field, _row_value(bound.row, field)) for field, _ in order_by]
            constraints.append(f"({' AND '.join(equal)})")
        return f"({' OR '.join(constraints)})"

    def _start_value(self, table: _Table, field: str, value: object) -> str:
        return self.params.column(self._server_column(table, field), value, comparison=True)

    def _start_equality(self, table: _Table, field: str, value: object) -> str:
        return f"{self._col(_Column(table, field))} IS NOT DISTINCT FROM {self._start_value(table, field, value)}"

    def _start_range(self, table: _Table, field: str, value: object, operator: str) -> str:
        column = self._col(_Column(table, field))
        if value is None:
            return f"{column} IS NOT NULL" if operator == ">" else "FALSE"
        typed = self._start_value(table, field, value)
        return f"{column} > {typed}" if operator == ">" else f"({column} IS NULL OR {column} < {typed})"

    def _related(self, relationships: Sequence[CorrelatedSubquery], format: Format | None, parent: _Table) -> list[str]:
        return [
            self._relationship(r, format.relationships.get(_must(r.subquery.alias)) if format else None, parent)
            for r in relationships
        ]

    def _correlator(self, parent_fields: Sequence[_Column], child_fields: Sequence[str]) -> _Correlate:
        def correlate(child: _Table) -> str:
            return " AND ".join(
                f"{self._col(parent)} = {self._col(_Column(child, field))}"
                for parent, field in zip(parent_fields, child_fields, strict=False)
            )

        return correlate

    def _parent_correlator(self, relationship: CorrelatedSubquery, parent: _Table) -> _Correlate:
        return self._correlator(
            [_Column(parent, f) for f in relationship.correlation.parent_field],
            relationship.correlation.child_field,
        )

    def _relationship(self, relationship: CorrelatedSubquery, format: Format | None, parent: _Table) -> str:
        alias = _must(relationship.subquery.alias)
        inner = f"inner_{relationship.subquery.alias}"
        singular = format.singular if format else False
        if relationship.hidden:
            join, tables = self._junction_join(relationship)
            last = tables[-1].table
            check(relationship.subquery.related, "hidden relationship must be a junction")
            assert relationship.subquery.related is not None
            nested = relationship.subquery.related[0].subquery
            selection = self._related(nested.related or (), format, last)
            selection += [self._select_ident(_Column(last, column)) for column in self.tables[nested.table].columns]
            correlate = self._parent_correlator(relationship, parent)(tables[0].table)
            where = f" AND {self._where(nested.where, last)}" if nested.where is not None else ""
            start = f" AND {self._start(nested.start, nested.order_by, last)}" if nested.start is not None else ""
            return (
                f"(SELECT {_to_json(inner, singular)} FROM (SELECT {','.join(selection)} FROM {join} "
                f"WHERE ({correlate}){where}{start} {self._order_by(nested.order_by, last)} "
                f"{limit_clause(self.params, tables[-1].limit, singular)} ) {ident(inner)}) as {ident(alias)}"
            )
        body = self.select(relationship.subquery, format, self._parent_correlator(relationship, parent))
        return f"(SELECT {_to_json(inner, singular)} FROM ({body}) {ident(inner)}) as {ident(alias)}"

    def _junction_join(self, relationship: CorrelatedSubquery) -> tuple[str, list[_JunctionTable]]:
        related = relationship.subquery.related
        check(related is not None and len(related) == 1, "Too many related tables for a junction edge")
        assert related is not None
        other = related[0]
        check(not other.hidden, "Expected junction edge relationship to not be hidden")
        tables = [
            _JunctionTable(
                self._table(relationship.subquery.table), relationship.correlation, relationship.subquery.limit
            ),
            _JunctionTable(self._table(other.subquery.table), other.correlation, other.subquery.limit),
        ]
        first, second = tables
        on = self._correlator(
            [_Column(first.table, f) for f in second.correlation.parent_field], second.correlation.child_field
        )(second.table)
        return f"{self._from(first.table)} JOIN {self._from(second.table)} ON {on}", tables

    def _where(self, condition: Condition, table: _Table) -> str:
        sql = self._condition(condition, table)
        # Upstream interpolates the `undefined` an unknown operator compiles to; its formatter refuses it.
        return sql if sql is not None else self.params.fail(AssertionFailedError(_NOT_AN_ARG))

    def _condition(self, condition: Condition, table: _Table) -> str | None:
        """The SQL for `condition`, or `None` where upstream's is `undefined`: under an unknown operator."""
        match condition:
            case Conjunction() | Disjunction():
                parts = [self._condition(c, table) for c in condition.conditions]
                if None in parts:
                    # `sql.join` reads each part's items, once every part is built.
                    raise TypeError("Cannot read properties of undefined (reading '_items')")
                return f"({f' {condition.type.upper()} '.join(p for p in parts if p is not None)})"
            case CorrelatedSubqueryCondition():
                # `scalar` is a planner hint for the IVM engines; Postgres decorrelates EXISTS on its own.
                body = self.select(condition.related.subquery, None, self._parent_correlator(condition.related, table))
                return f"{'NOT ' if condition.op == 'NOT EXISTS' else ''}EXISTS ({body})"
            case _:
                return self._simple(condition, table)

    def _simple(self, condition: SimpleCondition, table: _Table) -> str | None:
        left, right = condition.left, condition.right
        match condition.op:
            case "IN" | "NOT IN":
                lhs = self._value(left, table, right, plural=False)
                rhs = self._value(right, table, left, plural=True)
                return f"{'NOT' if condition.op == 'NOT IN' else ''} ({lhs} = ANY ({rhs}))"
            case "IS" | "IS NOT":
                lhs = self._value(left, table, right, plural=False)
                rhs = self._value(right, table, left, plural=False)
                distinct = "IS NOT DISTINCT FROM" if condition.op == "IS" else "IS DISTINCT FROM"
                return f"{lhs} {distinct} {rhs}"
            case "=" | "!=" | "<" | ">" | "<=" | ">=" | "LIKE" | "NOT LIKE" | "ILIKE" | "NOT ILIKE":
                lhs = self._value(left, table, right, plural=False)
                rhs = self._value(right, table, left, plural=False)
                return f"{lhs} {condition.op} {rhs}"
            case _:
                # Only the builder makes such an operator. Upstream's `switch` falls through.
                return None

    def _value(
        self,
        position: LiteralReference | ColumnReference | Parameter,
        table: _Table,
        other: LiteralReference | ColumnReference | Parameter,
        *,
        plural: bool,
    ) -> str:
        match position:
            case ColumnReference():
                return self._col(_Column(table, position.name))
            case Parameter():
                raise Exception(_STATIC)
            case _:
                return self._literal(position, table, other, plural=plural)

    def _literal(
        self,
        position: LiteralReference,
        table: _Table,
        other: LiteralReference | ColumnReference | Parameter,
        *,
        plural: bool,
    ) -> str:
        value = position.value
        match other:
            case ColumnReference():
                return self.params.column(self._server_column(table, other.name), value, plural=plural, comparison=True)
            case Parameter():
                raise Exception(_STATIC)
            case _:
                is_array = isinstance(value, list | tuple)
                check(plural == is_array, "Expected plural flag to match whether value is an array")
                if is_array:
                    # Only the right side of IN is plural, and the left side was compiled first, so it is
                    # a non-null scalar here. An empty list takes its element type from it.
                    return self.params.plural_literal(literal_type(value[0] if value else other.value), value)
                if not isinstance(value, str | int | float):
                    raise Exception(f"Literal of unexpected type. {js_string(value)} of type object")
                return self.params.literal(value)


_DATE_TIME_TYPES = frozenset(
    {
        "timestamptz", "timetz", "date", "time", "time without time zone", "time with time zone", "timestamp",
        "timestamp without time zone", "timestamp with time zone",
    }
)  # fmt: skip


def _row_value(row: Row, field: str) -> Value:
    """`row[field] ?? null`. The builder takes any row; an array has no column's value."""
    return row.get(field) if isinstance(row, dict) else None


def _must(alias: str | None) -> str:
    check(alias is not None, "Unexpected undefined value")
    assert alias is not None
    return alias
