"""Run a ZQL query against Postgres and return what the client-side query would have.

Ported from rocicorp/mono `packages/zero-server/src/pg-query-executor.ts` and `extractZqlResult` in
`packages/z2s/src/compiler.ts` at zero/v1.11.3 (e91e55f05b7a2c631f77238e1dd6fadf1029a538).
Apache-2.0, Copyright Rocicorp.
"""

import json
from typing import Any

from zero_server.protocol.ast import AST
from zero_server.query.builder import Format
from zero_server.schema import Schema
from zero_server.server.server_schema import ServerSchema
from zero_server.server.transaction import Queryable, Row
from zero_server.shared.js import js_keys
from zero_server.z2s.compiler import ZQL_RESULT_KEY, compile_query

_MAX_SAFE = 2**53 - 1


class _Unsafe(int):
    """An integer JavaScript cannot hold exactly. Upstream parses it as a bigint and refuses it."""


def _parse_float(text: str) -> int | float:
    """A whole number written with a fraction, as `numeric` epoch milliseconds are, comes back an `int`.

    JavaScript has one number type, so upstream cannot tell `1.0` from `1`; Python callers can.
    """
    value = float(text)
    return int(value) if value.is_integer() and abs(value) <= _MAX_SAFE else value


def _path_to_unsafe(value: Any) -> str | None:
    match value:
        case _Unsafe():
            return f" = {int(value)}"
        case list():
            for i, item in enumerate(value):
                if (path := _path_to_unsafe(item)) is not None:
                    return f"[{i}]{path}"
        case dict():
            for key in js_keys(value):
                if (path := _path_to_unsafe(value[key])) is not None:
                    return f"['{key}']{path}"
    return None


def extract_zql_result(rows: list[Row]) -> Any:
    unsafe = False

    def parse_int(text: str) -> int:
        nonlocal unsafe
        value = int(text)
        if abs(value) <= _MAX_SAFE:
            return value
        unsafe = True
        return _Unsafe(value)

    result = json.loads(rows[0][ZQL_RESULT_KEY], parse_int=parse_int, parse_float=_parse_float)
    # A duplicate key can drop a parsed unsafe integer, so the walk may still find none.
    if unsafe and (path := _path_to_unsafe(result)) is not None:
        raise Exception(f"Value exceeds safe Number range. {path}")
    return result


async def execute_postgres_query(
    tx: Queryable, ast: AST, format: Format, schema: Schema, server_schema: ServerSchema
) -> Any:
    """The query's rows, as lists and dicts in client names. A singular query with no row returns `None`."""
    text, values = compile_query(server_schema, schema, ast, format)
    rows = await tx.query(text, values)
    if not rows and format.singular:
        return None
    return extract_zql_result(rows)
