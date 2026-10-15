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
from functools import cached_property
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from zero_server.shared.js import js_string

type ValueType = Literal["string", "number", "boolean", "null", "json"]
type Cardinality = Literal["one", "many"]


class _SchemaModel(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="ignore", defer_build=True)


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

    @cached_property
    def client_to_server_mapper(self) -> "NameMapper":
        """`client_to_server(self.tables)`, built once: the query endpoint and z2s map names on every call."""
        return client_to_server(self.tables)


def load_schema(source: str | Path | Mapping[str, Any]) -> Schema:
    """A schema from `JSON.stringify(schema)`: the JSON text as a `str`, a `Path` to it, or the parsed object."""
    if isinstance(source, Path):  # python-only: reading the emitted schema from a file
        source = json.loads(source.read_text())
    elif isinstance(source, str):
        try:
            source = json.loads(source)
        except json.JSONDecodeError as e:  # python-only: a hint for a path passed as a str
            raise json.JSONDecodeError(
                f"{e.msg} (a str is JSON text; pass a pathlib.Path to read a file)", e.doc, e.pos
            ) from e
    return Schema.model_validate(source)


@dataclass(frozen=True)
class DestNames:
    table_name: str
    columns: dict[str, str]
    all_columns_same: bool


class NameMapper:
    def __init__(self, tables: dict[str, DestNames]) -> None:
        self._tables = tables
        self.correlations: dict[tuple[Any, ...], Any] = {}
        """What `map_ast` has mapped through this mapper, which it reuses."""

    def _table(self, src: str) -> DestNames:
        table = self._tables.get(src)
        if table is None:
            raise Exception(f'unknown table "{src}" ')
        return table

    def table_name(self, src: str) -> str:
        table = self._tables.get(src)
        return table.table_name if table is not None else self._table(src).table_name

    def column_name(self, table: str, src: str) -> str:
        dest_names = self._tables.get(table)
        if dest_names is not None and type(src) is str:
            dest = dest_names.columns.get(src)
            if dest:
                return dest
        return self._column_name(table, src)

    def _column_name(self, table: str, src: str) -> str:
        # Upstream looks the column up as an object key, which a builder handed a number turns into a string.
        src = src if isinstance(src, str) else js_string(src)
        dest = self._table(table).columns.get(src)
        if not dest:
            raise Exception(f'unknown column "{src}" of "{table}" table ')
        return dest


def client_to_server(tables: Mapping[str, TableSchema]) -> NameMapper:
    mapping: dict[str, DestNames] = {}
    for table_name, table in tables.items():
        all_same = True
        names: dict[str, str] = {}
        for name, column in table.columns.items():
            if column.server_name and column.server_name != name:
                all_same = False
            names[name] = column.server_name if column.server_name is not None else name
        server_table = table.server_name if table.server_name is not None else table_name
        mapping[table_name] = DestNames(table_name=server_table, columns=names, all_columns_same=all_same)
    return NameMapper(mapping)
