"""JSON values, as Zero's wire format allows them.

Ported from rocicorp/mono `packages/shared/src/json.ts` at zero/v1.11.3
(e91e55f05b7a2c631f77238e1dd6fadf1029a538). Apache-2.0, Copyright Rocicorp.
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
