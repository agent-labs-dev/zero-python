"""JSON values, as Zero's wire format allows them.

Ported from rocicorp/mono `packages/shared/src/json.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.
"""

import json
import math
from typing import Any

from pydantic import JsonValue

type JSONValue = JsonValue

_BOM = b"\xef\xbb\xbf"


def _refuse_constant(name: str) -> None:
    raise ValueError(f"{name} is not valid JSON")


def parse_json_body(body: bytes) -> Any:
    """`await request.json()` as Node's fetch runs it: decode, then `JSON.parse`.

    The decode never fails: a byte that is not UTF-8 becomes U+FFFD. Node's fetch (undici) drops a
    leading BOM and then decodes with a `TextDecoder` that drops another, so up to two go. `JSON.parse`
    has no `NaN` or `Infinity`. Raises `ValueError` where `JSON.parse` throws a `SyntaxError`.
    """
    text = body.removeprefix(_BOM).decode("utf-8-sig", "replace")
    return json.loads(text, parse_constant=_refuse_constant)


def is_json_value(value: Any) -> bool:
    """Whether `value` survives `JSON.stringify` unchanged."""
    match value:
        case None | bool() | int() | str():
            return True
        case float():
            return math.isfinite(value)
        case list() | tuple():
            return all(is_json_value(item) for item in value)
        case dict():
            return all(isinstance(key, str) and is_json_value(item) for key, item in value.items())
        case _:
            return False
