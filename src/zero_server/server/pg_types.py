"""Postgres column types as Zero sees them.

Ported from rocicorp/mono `packages/zero-cache/src/types/pg-data-type.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.
"""

from typing import Final

from zero_server.schema import ValueType

NUMERIC: Final = frozenset(
    {
        "smallint", "integer", "int", "int2", "int4", "int8", "bigint", "smallserial", "serial", "serial2",
        "serial4", "serial8", "bigserial", "decimal", "numeric", "real", "double precision", "float", "float4",
        "float8",
    }
)  # fmt: skip
NATIVE_STRING: Final = frozenset({"bpchar", "character", "character varying", "text", "varchar"})
TEXT_REPRESENTED: Final = frozenset(
    {
        "cidr", "ean13", "inet", "isbn", "isbn13", "ismn", "ismn13", "issn", "issn13", "macaddr", "macaddr8",
        "pg_lsn", "upc", "uuid",
    }
)  # fmt: skip
DATE_TIME: Final = frozenset(
    {
        "date", "time", "timetz", "time with time zone", "time without time zone", "timestamp", "timestamptz",
        "timestamp with time zone", "timestamp without time zone",
    }
)  # fmt: skip


def _lookup(pg_type: str) -> str:
    """Lowercased, with any arguments (the `(32)` of `char(32)`) removed."""
    lowered = pg_type.lower()
    start = lowered.find("(")
    return lowered if start == -1 else lowered[:start]


def is_number(pg_type: str) -> bool:
    return _lookup(pg_type) in NUMERIC


def is_native_string(pg_type: str) -> bool:
    return _lookup(pg_type) in NATIVE_STRING


def is_text_represented(pg_type: str) -> bool:
    return _lookup(pg_type) in TEXT_REPRESENTED


def is_string(pg_type: str) -> bool:
    return is_native_string(pg_type) or is_text_represented(pg_type)


def zql_value_type(pg_type: str, is_enum: bool, is_array: bool) -> ValueType | None:
    """The Zero type a Postgres column must be declared as, or `None` if Zero does not support it."""
    if is_array:
        return "json"
    key = _lookup(pg_type)
    if key in NUMERIC or key in DATE_TIME:
        return "number"
    if key in NATIVE_STRING or key in TEXT_REPRESENTED:
        return "string"
    if key in ("bool", "boolean"):
        return "boolean"
    if key in ("json", "jsonb"):
        return "json"
    return "string" if is_enum else None
