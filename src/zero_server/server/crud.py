"""`tx.mutate.<table>.insert / upsert / update / delete` on the server, as SQL.

Ported from rocicorp/mono `packages/zero-server/src/custom.ts` (`makeServerTableCRUD`),
`packages/zql/src/mutate/crud.ts` and the column conversions of `packages/z2s/src/sql.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.

Values are bound as `zero_server.z2s.sql` binds them.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Generic

from typing_extensions import TypeVar

from zero_server.schema import Schema, TableSchema
from zero_server.server.server_schema import ServerSchema, ServerTableSchema
from zero_server.server.transaction import DBTransaction
from zero_server.shared.asserts import AssertionFailedError
from zero_server.shared.js import js_keys
from zero_server.z2s.sql import Binding, Params, binding, ident


def _as_row(value: object) -> dict[str, Any]:
    """The row a value stands for, by `Object.entries`: upstream reads every row that way first."""
    match value:
        case None:
            raise TypeError("Cannot convert undefined or null to object")
        case Mapping():
            return dict(value)
        case str() | list() | tuple():
            return {str(i): item for i, item in enumerate(value)}
        case _:
            return {}


class UndefinedValueError(Exception):
    """A primary-key column missing from the row. Upstream binds `undefined` for it, which postgres.js
    refuses with this message, so the mutation fails with the same application error."""

    error_name = "Error"

    def __init__(self) -> None:
        super().__init__("UNDEFINED_VALUE: Undefined values are not allowed")


def _key_value(row: Mapping[str, Any], name: str) -> Any:
    if name not in row:
        raise UndefinedValueError
    return row[name]


def _server_name(table: TableSchema) -> str:
    return table.server_name if table.server_name is not None else table.name


def _server_column(table: TableSchema, column: str) -> str:
    schema = table.columns.get(column)
    if schema is None:  # `check`, without formatting the message for every column that passes
        raise _unknown_column(table, column)
    return schema.server_name if schema.server_name is not None else column


@dataclass(slots=True)
class _Column:
    server: str
    ident: str
    # Looked up on first write, as the server schema need not have a column no row writes.
    write: Binding | None = None


_plans: dict[tuple[int, int], tuple[TableSchema, ServerTableSchema, dict[str, _Column]]] = {}


def _write_plan(table: TableSchema, server_table: ServerTableSchema) -> dict[str, _Column]:
    """Each client column of `table`, as an insert writes it. Built once per table and server schema."""
    key = (id(table), id(server_table))
    entry = _plans.get(key)
    # The entry holds both objects, so neither id is reused while it lives.
    if entry is not None and entry[0] is table and entry[1] is server_table:
        return entry[2]
    plan = {name: _Column(server, ident(server)) for name in table.columns for server in (_server_column(table, name),)}
    _plans[key] = (table, server_table, plan)
    return plan


def _unknown_column(table: TableSchema, column: str) -> AssertionFailedError:
    return AssertionFailedError(f"Column {column} was not found in the Zero schema for the table {table.name}")


I = TypeVar("I", default=Mapping[str, Any])  # noqa: E741 - the insert row type
U = TypeVar("U", default=Mapping[str, Any])
K = TypeVar("K", default=Mapping[str, Any])


class TableCRUD(Generic[I, U, K]):
    """Writes to one table. Generated schema types narrow the row types: `I` to insert or upsert, `U` to
    update (the key and any other columns), `K` to delete (the key)."""

    def __init__(self, table: TableSchema, tx: DBTransaction[Any], server_table: ServerTableSchema) -> None:
        self._table = table
        self._tx = tx
        self._server_table = server_table

    def _pk_clause(self, params: Params, row: Mapping[str, Any]) -> str:
        return " AND ".join(
            f"{ident(server)} = {params.column(self._server_table[server], _key_value(row, name), comparison=True)}"
            for name, server in ((n, _server_column(self._table, n)) for n in self._table.primary_key)
        )

    def _non_pk(self, value: dict[str, Any]) -> list[str]:
        return [k for k in js_keys(value) if k not in self._table.primary_key]

    async def insert(self, value: I) -> None:
        await self._insert(_as_row(value), upsert=False)

    async def upsert(self, value: I) -> None:
        await self._insert(_as_row(value), upsert=True)

    def _bind(self, params: Params, column: _Column, value: Any) -> str:
        write = column.write
        if write is None:
            write = column.write = binding(self._server_table[column.server], False)
        return params.bind(write, value)

    async def _insert(self, value: dict[str, Any], *, upsert: bool) -> None:
        keys = js_keys(value)
        plan = _write_plan(self._table, self._server_table)
        targeted: list[_Column] = []
        for k in keys:
            column = plan.get(k)
            if column is None:
                raise _unknown_column(self._table, k)
            targeted.append(column)
        pk = [_server_column(self._table, k) for k in self._table.primary_key]
        params = Params()
        values = ", ".join([self._bind(params, column, value[k]) for k, column in zip(keys, targeted, strict=True)])
        action = "DO NOTHING"
        if upsert:
            primary_key = self._table.primary_key
            sets = ", ".join(
                [
                    f"{column.ident} = {self._bind(params, column, value[k])}"
                    for k, column in zip(keys, targeted, strict=True)
                    if k not in primary_key
                ]
            )
            if sets:
                action = f"DO UPDATE SET {sets}"
        sql = (
            f"INSERT INTO {ident(_server_name(self._table))} ({','.join([c.ident for c in targeted])}) "
            f"VALUES ({values}) ON CONFLICT ({', '.join(ident(c) for c in pk)}) {action}"
        )
        await self._tx.query(sql, params.values)

    async def update(self, value: U) -> None:
        row = _as_row(value)
        targeted = [(k, _server_column(self._table, k)) for k in self._non_pk(row)]
        if not targeted:
            return
        params = Params()
        sets = ", ".join(
            f"{ident(server)} = {params.column(self._server_table[server], row[k], comparison=False)}"
            for k, server in targeted
        )
        where = self._pk_clause(params, row)
        await self._tx.query(f"UPDATE {ident(_server_name(self._table))} SET {sets} WHERE {where}", params.values)

    async def delete(self, value: K) -> None:
        params = Params()
        where = self._pk_clause(params, _as_row(value))
        await self._tx.query(f"DELETE FROM {ident(_server_name(self._table))} WHERE {where}", params.values)


class Mutate:
    """`tx.mutate.issue` (or `tx.mutate["issue"]`) is the CRUD for the `issue` table, in client names."""

    def __init__(self, schema: Schema, tx: DBTransaction[Any], server_schema: ServerSchema) -> None:
        self._schema = schema
        self._tx = tx
        self._server_schema = server_schema

    def __getitem__(self, table: str) -> TableCRUD[Any, Any, Any]:
        table_schema = self._schema.tables[table]
        return TableCRUD(table_schema, self._tx, self._server_schema[_server_name(table_schema)])

    def __getattr__(self, table: str) -> TableCRUD[Any, Any, Any]:
        try:
            return self[table]
        except KeyError:  # python-only: an unknown table is an AttributeError, where upstream has `undefined`
            raise AttributeError(table) from None
