"""Identifiers and bound values for the SQL that z2s and `tx.mutate` write.

Ported from rocicorp/mono `packages/z2s/src/sql.ts` (`formatPgInternalConvert` and its argument
conversions) at zero/v1.10.0 (905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.

Every value is bound as text and cast in SQL, so the driver never coerces it: a number is its JSON
text, a timestamp epoch milliseconds, a JSON value its `JSON.stringify` output.
"""

from typing import Any, Literal

from zero_server.server.pg_types import is_native_string, is_number, is_string, is_text_represented
from zero_server.server.server_schema import ServerColumnSchema
from zero_server.shared.js import js_string, js_stringify

type LiteralType = Literal["boolean", "number", "string"]

_LITERAL_PG_TYPE: dict[str, str] = {"boolean": "boolean", "number": "double precision", "string": "text"}


def ident(*names: str) -> str:
    """Quote an identifier as upstream's `sql.ident` does. A single dotted name is split into its parts."""
    parts = names[0].split(".") if len(names) == 1 else list(names)
    return ".".join('"' + part.replace('"', '""') + '"' for part in parts)


def literal_type(value: object) -> LiteralType:
    """`typeof value` for a scalar literal."""
    match value:
        case bool():
            return "boolean"
        case int() | float():
            return "number"
        case _:
            return "string"


def _cast(placeholder: str, column: ServerColumnSchema, comparison: bool) -> str:
    match column.type:
        case "timestamptz" | "timestamp with time zone":
            return f"to_timestamp({placeholder}::text::numeric / 1000.0)"
        case "date" | "timestamp" | "timestamp without time zone":
            return f"to_timestamp({placeholder}::text::numeric / 1000.0) AT TIME ZONE 'UTC'"
        case "timetz" | "time with time zone":
            return f"({placeholder}::text::int * interval'1ms')::time"
        case "time" | "time without time zone":
            return f"({placeholder}::text::int * interval'1ms')::time AT TIME ZONE 'UTC'"
        case "uuid":
            return f"{placeholder}::text::uuid"
    if column.is_enum:
        return f'{placeholder}::text::"{column.type}"'
    if is_native_string(column.type):
        # Compared as plain text, so a value longer than a varchar column still compares.
        return f"{placeholder}::text" if comparison else f"{placeholder}::text::{column.type}"
    if is_text_represented(column.type):
        return f"{placeholder}::text::{column.type}"
    if is_number(column.type):
        # Compared as a double, the representation every ZQL number has.
        return f"{placeholder}::text::double precision" if comparison else f"{placeholder}::text::{column.type}"
    return f"{placeholder}::text::{column.type}"


def _plural(index: int, select: str) -> str:
    return f"ARRAY(SELECT {select} FROM jsonb_array_elements_text(${index}::text::jsonb))"


def _same_value_key(value: object) -> object:
    """A key equal for values a JavaScript `Map` treats as one key: equal primitives, the same object."""
    match value:
        case None:
            return ("null",)
        case bool():
            return ("boolean", value)
        case int() | float():
            return ("number", float(value))
        case str():
            return ("string", value)
        case _:
            return ("object", id(value))


class Params:
    """The values bound to one statement, in placeholder order.

    Like upstream's formatter, a value already bound with the same type reuses its placeholder. That is
    not only cosmetic: Postgres infers a reused placeholder's type from its first use, so whether a
    bare `$n` works can depend on it. Values must be bound in the order they appear in the SQL text.
    """

    def __init__(self) -> None:
        self.values: list[str | None] = []
        self._seen: dict[tuple[object, str], int] = {}
        # Objects keyed by identity are kept alive so their ids stay unique for the statement.
        self._held: list[object] = []

    def _bind(self, value: object, type_key: str, bound: str | None) -> int:
        key = (_same_value_key(value), type_key)
        if key in self._seen:
            return self._seen[key]
        self._held.append(value)
        self.values.append(bound)
        self._seen[key] = len(self.values)
        return len(self.values)

    def column(self, column: ServerColumnSchema, value: Any, *, plural: bool = False, comparison: bool) -> str:
        """A value compared with or written to `column`, cast to the column's type."""
        plural = plural or column.is_array
        if value is None:
            bound = None
        elif plural:
            bound = js_stringify(value)
        elif column.is_enum or is_string(column.type):
            bound = value if isinstance(value, str) else js_string(value)
        else:
            bound = js_stringify(value)
        index = self._bind(value, column.type, bound)
        if value is None and plural:
            return f"${index}"
        common = _cast("value" if plural else f"${index}", column, comparison)
        return _plural(index, common) if plural else common

    def literal(self, value: str | int | float | bool) -> str:
        """A scalar compared with another literal, typed by its own JSON type."""
        kind = literal_type(value)
        index = self._bind(value, kind, value if isinstance(value, str) else js_stringify(value))
        return f"${index}::text::{_LITERAL_PG_TYPE[kind]}"

    def plural_literal(self, kind: LiteralType, values: list[Any]) -> str:
        index = self._bind(values, kind, js_stringify(values))
        return _plural(index, f"value::{_LITERAL_PG_TYPE[kind]}")
