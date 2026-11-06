"""JSON values, as Zero's wire format allows them.

Ported from rocicorp/mono `packages/shared/src/json.ts` at zero/v1.11.4
(be94346faaf4ee7e7b2cf983f6e5accae3a6b4fb). Apache-2.0, Copyright Rocicorp.
"""

from typing import Any

from pydantic import JsonValue

type JSONValue = JsonValue


def is_json_value(value: Any) -> bool:
    """Upstream's `isJSONValue`, to which every number is JSON, `NaN` included: `JSON.stringify` writes it
    as `null`."""
    match value:
        case None | bool() | int() | float() | str():
            return True
        case list() | tuple():
            return all(is_json_value(item) for item in value)
        case dict():
            return all(isinstance(key, str) and is_json_value(item) for key, item in value.items())
        case _:
            return False
