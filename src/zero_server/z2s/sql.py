"""Identifiers and bound values for the SQL that z2s and `tx.mutate` write.

Ported from rocicorp/mono `packages/z2s/src/sql.ts` (`formatPgInternalConvert` and its argument
conversions) at zero/v1.10.0 (905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.

Every value is bound as text and cast in SQL, so the driver never coerces it: a number is its JSON
text, a timestamp epoch milliseconds, a JSON value its `JSON.stringify` output.

An identifier is checked as `escapePostgresIdentifier` checks it, ported from `@databases/escape-identifier`
1.0.3 (`lib/index.js`) and `@databases/validate-unicode` 1.0.0 (`lib/index.js`), the versions
`@rocicorp/zero` 1.10.0 installs. MIT, Copyright (c) Forbes Lindesay.
"""

import functools
import re
from collections.abc import Sequence
from typing import Any, Literal

from zero_server.server.pg_types import is_native_string, is_number, is_string, is_text_represented
from zero_server.server.server_schema import ServerColumnSchema
from zero_server.shared.js import js_string, js_stringify

type LiteralType = Literal["boolean", "number", "string", "object"]

_LITERAL_PG_TYPE: dict[str, str] = {"boolean": "boolean", "number": "double precision", "string": "text"}


@functools.lru_cache(maxsize=4096)
def ident(*names: str) -> str:
    """Quote an identifier as upstream's `sql.ident` does. A single dotted name is split into its parts."""
    parts = names[0].split(".") if len(names) == 1 else list(names)
    return ".".join(_escape_identifier(part) for part in parts)


_LONE_SURROGATE = re.compile("[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]")
_NON_ASCII = re.compile("[^\u0001-\u007f]")


def _escape_identifier(name: str) -> str:
    """`escapePostgresIdentifier`, which refuses what Postgres would take quoted, in its own words."""
    if _LONE_SURROGATE.search(name):
        raise Exception(f"This string contains unmatched surrogate pairs: {js_stringify(name)}")
    if not name:
        raise Exception("Postgres identifiers must be at least 1 character long.")
    if len(name.encode("utf-16-le", "surrogatepass")) // 2 > 60:
        raise Exception(f"Postgres identifiers must not be longer than 60 characters. {name}")
    if _NON_ASCII.search(name):
        raise Exception(
            "Postgres identifiers must only contain ASCII characters (to use unicode, pass {extended: true} "
            "when escaping the identifier)"
        )
    return '"' + name.replace('"', '""') + '"'


def literal_type(value: object) -> LiteralType:
    """`typeof value` for a JSON value."""
    match value:
        case bool():
            return "boolean"
        case int() | float():
            return "number"
        case str():
            return "string"
        case _:
            return "object"


_PLACEHOLDER = "\x00"  # never in a type name


@functools.cache
def _binding(column: ServerColumnSchema, comparison: bool) -> tuple[bool, str, str]:
    """How a value for `column` is bound: whether a scalar goes as the text postgres.js sends rather than
    as JSON, and the SQL before and after its placeholder that casts it to the column's type."""
    before, after = _cast(_PLACEHOLDER, column, comparison).split(_PLACEHOLDER)
    return column.is_enum or is_string(column.type), before, after


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


def _first_leaf(value: object) -> object:
    while isinstance(value, list | tuple) and value:
        value = value[0]
    return value


def _driver_text(value: object) -> str:
    """The text postgres.js sends for a raw value: `String(value)`, but an array typed by its first leaf,
    so one led by a boolean is the boolean `false` (it is not `true`)."""
    if isinstance(value, list | tuple) and isinstance(_first_leaf(value), bool):
        return "false"
    return js_string(value)


def _same_value_key(value: object) -> object:
    """A key equal for values a JavaScript `Map` treats as one key: equal primitives, the same object.

    The builder gives every literal its own list, as every JavaScript array literal is its own object.
    """
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

    Upstream builds the whole statement before it formats it, so an error it only meets while
    formatting loses to any error building raises, wherever that is. `fail` keeps the first such error
    for the caller to raise once the statement is built.
    """

    def __init__(self) -> None:
        self.values: list[str | None] = []
        self.failure: Exception | None = None
        self._seen: dict[tuple[object, str], int] = {}
        # Objects keyed by identity are kept alive so their ids stay unique for the statement.
        self._held: list[object] = []

    def _bind(self, value: object, type_key: str, bound: str | None) -> int:
        key = (_same_value_key(value), type_key)
        index = self._seen.get(key)
        if index is None:
            self._held.append(value)
            self.values.append(bound)
            index = self._seen[key] = len(self.values)
        return index

    def column(self, column: ServerColumnSchema, value: Any, *, plural: bool = False, comparison: bool) -> str:
        """A value compared with or written to `column`, cast to the column's type."""
        plural = plural or column.is_array
        as_text, before, after = _binding(column, comparison)
        if value is None:
            bound = None
        elif plural:
            bound = js_stringify(value)
        elif as_text:
            bound = value if isinstance(value, str) else _driver_text(value)
        else:
            bound = js_stringify(value)
        index = self._bind(value, column.type, bound)
        if value is None and plural:
            return f"${index}"
        if plural:
            return _plural(index, f"{before}value{after}")
        return f"{before}${index}{after}"

    def fail(self, error: Exception) -> str:
        """Record an error upstream raises while formatting, and stand in for the text it never wrote."""
        self.failure = self.failure or error
        return ""

    def literal(self, value: str | int | float | bool) -> str:
        """A scalar compared with another literal, typed by its own JSON type."""
        kind = literal_type(value)
        index = self._bind(value, kind, value if isinstance(value, str) else js_stringify(value))
        return f"${index}::text::{_LITERAL_PG_TYPE[kind]}"

    def plural_literal(self, kind: LiteralType, values: Sequence[Any]) -> str:
        if kind not in _LITERAL_PG_TYPE:
            return self.fail(Exception("Unreachable"))
        index = self._bind(values, kind, js_stringify(values))
        return _plural(index, f"value::{_LITERAL_PG_TYPE[kind]}")
