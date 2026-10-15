"""The Zero schema, loaded from the JSON of the TypeScript `schema.ts`, and its name mapping.

The schema stays authored in TypeScript because the client needs it. Emit it with
`JSON.stringify(schema)` and load that here; this module has no schema DSL of its own.

Ported from rocicorp/mono `packages/zero-types/src/schema.ts`, `schema-value.ts`, `name-mapper.ts` and
`packages/zero-schema/src/name-mapper.ts` at zero/v1.10.0 (905d08516ba0caaf314cd28e93a5a47e8acdd482).
Apache-2.0, Copyright Rocicorp.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from zero_server.shared.js import js_stringify

type ValueType = Literal["string", "number", "boolean", "null", "json"]
type Cardinality = Literal["one", "many"]


class _SchemaModel(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="ignore")


class ColumnSchema(_SchemaModel):
    type: ValueType
    optional: bool = False
    server_name: str | None = Field(default=None, alias="serverName")


class TableSchema(_SchemaModel):
    name: str
    server_name: str | None = Field(default=None, alias="serverName")
    columns: dict[str, ColumnSchema]
    primary_key: tuple[str, ...] = Field(alias="primaryKey", min_length=1)


class Connection(_SchemaModel):
    source_field: tuple[str, ...] = Field(alias="sourceField")
    dest_field: tuple[str, ...] = Field(alias="destField")
    dest_schema: str = Field(alias="destSchema")
    cardinality: Cardinality


type Relationship = tuple[Connection] | tuple[Connection, Connection]


class Schema(_SchemaModel):
    tables: dict[str, TableSchema]
    # Upstream only has entries for tables that declare relationships.
    relationships: dict[str, dict[str, Relationship]] = Field(default_factory=dict)
    enable_legacy_queries: bool | None = Field(default=None, alias="enableLegacyQueries")
    enable_legacy_mutators: bool | None = Field(default=None, alias="enableLegacyMutators")


def load_schema(source: str | Path | Mapping[str, Any]) -> Schema:
    """A schema from `JSON.stringify(schema)`: the JSON text, a path to it, or the parsed object."""
    if isinstance(source, Path):
        source = source.read_text()
    if isinstance(source, str):
        source = json.loads(source)
    return Schema.model_validate(source)


@dataclass(frozen=True)
class DestNames:
    table_name: str
    columns: dict[str, str]
    all_columns_same: bool


def _in_context(context: Any) -> str:
    return f"in {js_stringify(context)}" if context else ""


class NameMapper:
    def __init__(self, tables: dict[str, DestNames]) -> None:
        self._tables = tables

    def _table(self, src: str, context: Any = None) -> DestNames:
        table = self._tables.get(src)
        if table is None:
            raise Exception(f'unknown table "{src}" {_in_context(context)}')
        return table

    def table_name(self, src: str, context: Any = None) -> str:
        return self._table(src, context).table_name

    def table_name_if_known(self, src: str) -> str | None:
        table = self._tables.get(src)
        return table.table_name if table is not None else None

    def column_name(self, table: str, src: str, context: Any = None) -> str:
        dest = self._table(table, context).columns.get(src)
        if not dest:
            raise Exception(f'unknown column "{src}" of "{table}" table {_in_context(context)}')
        return dest

    def row[V](self, table: str, row: Mapping[str, V]) -> dict[str, V]:
        """Rename a row's columns. Columns the schema does not know pass through."""
        dest = self._table(table)
        if dest.all_columns_same:
            return dict(row)
        return {dest.columns.get(column, column): value for column, value in row.items()}

    def columns(self, table: str, columns: tuple[str, ...]) -> tuple[str, ...]:
        dest = self._table(table)
        if dest.all_columns_same:
            return columns
        return tuple(dest.columns.get(column, column) for column in columns)


def client_to_server(tables: Mapping[str, TableSchema]) -> NameMapper:
    return _mapper("client", tables)


def server_to_client(tables: Mapping[str, TableSchema]) -> NameMapper:
    return _mapper("server", tables)


def _mapper(src: Literal["client", "server"], tables: Mapping[str, TableSchema]) -> NameMapper:
    mapping: dict[str, DestNames] = {}
    for table_name, table in tables.items():
        all_same = True
        names: dict[str, str] = {}
        for name, column in table.columns.items():
            if column.server_name and column.server_name != name:
                all_same = False
            server_column = column.server_name if column.server_name is not None else name
            if src == "client":
                names[name] = server_column
            else:
                names[server_column] = name
        server_table = table.server_name if table.server_name is not None else table_name
        key = table_name if src == "client" else server_table
        dest = server_table if src == "client" else table_name
        mapping[key] = DestNames(table_name=dest, columns=names, all_columns_same=all_same)
    return NameMapper(mapping)
