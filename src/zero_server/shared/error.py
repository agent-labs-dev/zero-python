"""Reading a message and JSON details off a raised exception.

Ported from rocicorp/mono `packages/shared/src/error.ts` at zero/v1.11.3
(e91e55f05b7a2c631f77238e1dd6fadf1029a538). Apache-2.0, Copyright Rocicorp.

JavaScript's `Error` is Python's `Exception`: an error whose class is exactly `Exception` carries no
`{name}` detail, the way a plain `Error` carries none upstream. A class can set `error_name` to the name
its upstream counterpart reports, which is `"Error"` for the upstream classes that never set `this.name`.
Upstream also reads values thrown that are not errors, which Python cannot raise.
"""

from typing import Any

from zero_server.shared.js import js_stringify
from zero_server.shared.json import JSONValue, is_json_value

_ABSENT: Any = object()


def get_error_message(error: BaseException) -> str:
    return _message(error, set())


def _message(error: BaseException, seen: set[int]) -> str:
    if id(error) in seen:
        return "Circular error reference detected while extracting the error message."
    seen.add(id(error))

    message = exception_message(error)
    if message:
        return message
    if error.__cause__ is not None:
        # Never empty: an error with no message is described by its properties.
        return _message(error.__cause__, seen)

    properties = enumerable_properties(error)
    if is_json_value(properties):
        return f"Parsed message: {js_stringify(properties)}"
    return "Unknown error of type object was thrown and the message could not be determined. See cause for details."


def enumerable_properties(error: BaseException) -> dict[str, Any]:
    """What `JSON.stringify` would see of an error: its own attributes, less those JavaScript hides.

    `message` is never enumerable on a JavaScript error, and `ApplicationError` keeps its details in a
    private field.
    """
    hidden = {"message", "details"} if error_name(error) == "ApplicationError" else {"message"}
    return {k: v for k, v in vars(error).items() if not k.startswith("_") and k not in hidden}


def exception_message(error: BaseException) -> str:
    """JavaScript's `error.message`, for a Python exception."""
    message = getattr(error, "message", None)
    if isinstance(message, str):
        return message
    if len(error.args) == 1 and isinstance(error.args[0], str):
        return error.args[0]
    return str(error)


def get_error_details(error: BaseException) -> JSONValue | None:
    details = getattr(error, "details", _ABSENT)
    # An `ApplicationError`'s unset details are `undefined` upstream, which is not JSON; another error's
    # `None` is a `null`, which is.
    unset = details is _ABSENT or (details is None and error_name(error) == "ApplicationError")
    if not unset and is_json_value(details):
        return details
    name = error_name(error)
    if name not in ("Exception", "Error"):
        return {"name": name}
    return None


def error_name(error: BaseException) -> str:
    """The `name` upstream would report for this error."""
    name = getattr(type(error), "error_name", None)
    return name if isinstance(name, str) else type(error).__name__
