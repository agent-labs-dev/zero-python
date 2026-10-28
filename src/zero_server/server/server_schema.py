"""The Postgres types behind each column of the Zero schema, and the check that they agree.

Ported from rocicorp/mono `packages/zero-server/src/schema.ts` and `packages/zero-types/src/server-schema.ts`
at zero/v1.11.3 (e91e55f05b7a2c631f77238e1dd6fadf1029a538). Apache-2.0, Copyright Rocicorp.
"""

from dataclasses import dataclass

from zero_server.schema import Schema
from zero_server.server.pg_types import zql_value_type
from zero_server.server.transaction import Queryable
from zero_server.shared.asserts import check


@dataclass(frozen=True)
class ServerColumnSchema:
    type: str
    is_enum: bool
    is_array: bool


type ServerTableSchema = dict[str, ServerColumnSchema]
type ServerSchema = dict[str, ServerTableSchema]

# Reads pg_catalog rather than information_schema, as upstream does, for the same columns.
_QUERY = """
      SELECT
          n.nspname::text AS schema,
          cl.relname::text AS table,
          a.attname::text AS column,
          CASE
              WHEN ut.typelem <> 0 AND ut.typlen = -1 THEN 'ARRAY'
              WHEN nut.nspname = 'pg_catalog' THEN pg_catalog.format_type(ut.oid, NULL)
              ELSE 'USER-DEFINED'
          END::text AS "dataType",
          CASE
              WHEN tm.typmod = -1 THEN NULL
              WHEN ut.oid IN ('pg_catalog.bpchar'::regtype, 'pg_catalog.varchar'::regtype) THEN tm.typmod - 4
              WHEN ut.oid IN ('pg_catalog.bit'::regtype, 'pg_catalog.varbit'::regtype) THEN tm.typmod
          END AS length,
          CASE ut.oid
              WHEN 'pg_catalog.int2'::regtype THEN 16
              WHEN 'pg_catalog.int4'::regtype THEN 32
              WHEN 'pg_catalog.int8'::regtype THEN 64
              WHEN 'pg_catalog.float4'::regtype THEN 24
              WHEN 'pg_catalog.float8'::regtype THEN 53
              WHEN 'pg_catalog.numeric'::regtype THEN
                CASE WHEN tm.typmod = -1 THEN NULL ELSE ((tm.typmod - 4) >> 16) & 65535 END
          END AS precision,
          CASE
              WHEN ut.oid IN ('pg_catalog.int2'::regtype, 'pg_catalog.int4'::regtype, 'pg_catalog.int8'::regtype)
                THEN 0
              WHEN ut.oid = 'pg_catalog.numeric'::regtype AND tm.typmod <> -1 THEN (tm.typmod - 4) & 65535
          END AS scale,
          ut.typtype::text AS typtype,
          ut.typname::text AS typename,
          CASE WHEN ut.typelem <> 0 THEN et.typtype::text ELSE NULL END AS "elemTyptype",
          CASE WHEN ut.typelem <> 0 THEN et.typname::text ELSE NULL END AS "elemTypname"
      FROM
          pg_catalog.pg_attribute a
      JOIN pg_catalog.pg_class cl ON cl.oid = a.attrelid
      JOIN pg_catalog.pg_namespace n ON n.oid = cl.relnamespace
      JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
      JOIN pg_catalog.pg_type ut ON ut.oid = CASE WHEN t.typtype = 'd' THEN t.typbasetype ELSE t.oid END
      JOIN pg_catalog.pg_namespace nut ON nut.oid = ut.typnamespace
      LEFT JOIN pg_catalog.pg_type et ON et.oid = ut.typelem
      CROSS JOIN LATERAL
          (SELECT CASE WHEN t.typtype = 'd' THEN t.typtypmod ELSE a.atttypmod END AS typmod) tm
      WHERE
          a.attnum > 0
          AND NOT a.attisdropped
          AND cl.relkind IN ('r', 'v', 'f', 'p')
          AND NOT pg_catalog.pg_is_other_temp_schema(n.oid)
          AND (pg_catalog.pg_has_role(cl.relowner, 'USAGE')
               OR pg_catalog.has_column_privilege(cl.oid, a.attnum, 'SELECT, INSERT, UPDATE, REFERENCES'))
          AND (n.nspname, cl.relname) IN ({pairs})
"""

_ENUM = "e"


def _schema_and_table(name: str, server_name: str | None) -> tuple[str, str]:
    if server_name and "." in server_name:
        schema, table = server_name.split(".", 1)
        return schema, table
    return "public", server_name if server_name is not None else name


async def get_server_schema(tx: Queryable, schema: Schema) -> ServerSchema:
    pairs = [_schema_and_table(t.name, t.server_name) for t in schema.tables.values()]
    if not pairs:
        return {}
    placeholders = ",".join(f"(${2 * i + 1}::text, ${2 * i + 2}::text)" for i in range(len(pairs)))
    rows = await tx.query(_QUERY.format(pairs=placeholders), [part for pair in pairs for part in pair])

    server_schema: ServerSchema = {}
    for row in rows:
        table_name = row["table"] if row["schema"] == "public" else f"{row['schema']}.{row['table']}"
        table = server_schema.setdefault(table_name, {})
        is_array = row["elemTyptype"] is not None
        is_enum = (row["elemTyptype"] if row["elemTyptype"] is not None else row["typtype"]) == _ENUM
        type_ = row["dataType"].lower()
        if is_array:
            check(
                row["elemTypname"],
                f'Array column "{row["column"]}" in table "{table_name}" is missing element type name',
            )
            type_ = row["elemTypname"]
        elif is_enum or (type_ == "user-defined" and zql_value_type(row["typename"], False, False) is not None):
            type_ = row["typename"]
        if type_ in ("bpchar", "character", "character varying", "varchar") and row["length"]:
            type_ = f"{type_}({row['length']})"
        elif type_ == "character":
            # Without a length `character` means `character(1)`, so an unbounded `bpchar` is `bpchar`.
            type_ = "bpchar"
        if type_ in ("numeric", "decimal") and row["precision"] is not None and row["scale"] is not None:
            type_ = f"{type_}({row['precision']}, {row['scale']})"
        table[row["column"]] = ServerColumnSchema(type=type_, is_enum=is_enum, is_array=is_array)

    errors = check_schemas_are_compatible(schema, server_schema)
    check(not errors, _incompatible_message(errors))
    return server_schema


def check_schemas_are_compatible(schema: Schema, server_schema: ServerSchema) -> list[str]:
    """One message per table, column or type in `schema` that the database does not match."""
    errors: list[str] = []
    for table in schema.tables.values():
        server_table_name = table.server_name if table.server_name is not None else table.name
        server_table = server_schema.get(server_table_name)
        if server_table is None:
            errors.append(
                f'Table "{server_table_name}" is defined in your zero schema but does not exist in the database.'
            )
            continue
        for column_name, column in table.columns.items():
            server_column_name = column.server_name if column.server_name is not None else column_name
            server_column = server_table.get(server_column_name)
            if server_column is None:
                errors.append(
                    f'Column "{server_column_name}" in table "{server_table_name}" is defined in your zero schema '
                    "but does not exist in the database."
                )
                continue
            required = zql_value_type(server_column.type, server_column.is_enum, server_column.is_array)
            if required != column.type:
                reason = (
                    f"{server_column.type} is currently unsupported in Zero. Please file a bug at "
                    "https://bugs.rocicorp.dev/"
                    if required is None
                    else f"{server_column.type} should be mapped to {required} in Zero not {column.type}."
                )
                errors.append(
                    f'Type mismatch for column "{server_column_name}" in table "{server_table_name}": {reason}'
                )
    return errors


def _incompatible_message(errors: list[str]) -> str:
    return "\n".join(
        [
            "Schema incompatibility detected between your zero schema definition and the database:",
            "",
            *(f"  - {e}" for e in errors),
            "",
            "Please update your schema definition to match the database or migrate your database to match the schema.",
        ]
    )
