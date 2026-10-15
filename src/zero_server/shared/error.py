"""Reading a message and JSON details off anything that was raised.

Ported from rocicorp/mono `packages/shared/src/error.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.

JavaScript's `Error` is Python's `Exception`: an error whose class is exactly `Exception` carries no
`{name}` detail, the way a plain `Error` carries none upstream. A class can set `error_name` to the name
its upstream counterpart reports, which is `"Error"` for the upstream classes that never set `this.name`.
"""

from typing import Any

from zero_server.shared.js import js_stringify
from zero_server.shared.json import JSONValue, is_json_value


def get_error_message(error: object) -> str:
    return _message(error, set())


def _message(error: object, seen: set[int]) -> str:
    if isinstance(error, str):
        return error
    if id(error) in seen:
        return "Circular error reference detected while extracting the error message."
    seen.add(id(error))

    if isinstance(error, BaseException):
        message = _exception_message(error)
        if message:
            return message
        if error.__cause__ is not None:
            return _message(error.__cause__, seen)

    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message

    as_object = enumerable_properties(error) if isinstance(error, BaseException) else error
    if is_json_value(as_object):
        return f"Parsed message: {js_stringify(as_object)}"

    return (
        f"Unknown error of type {js_typeof(error)} was thrown and the message could not be determined. "
        "See cause for details."
    )


def enumerable_properties(error: BaseException) -> dict[str, Any]:
    """What `JSON.stringify` would see of an error: its own attributes, less those JavaScript hides.

    `message` is never enumerable on a JavaScript error, and `ApplicationError` keeps its details in a
    private field.
    """
    hidden = {"message", "details"} if type(error).__name__ == "ApplicationError" else {"message"}
    return {k: v for k, v in vars(error).items() if not k.startswith("_") and k not in hidden}


def js_typeof(value: object) -> str:
    """`typeof` for what reaches the unknown-error path, which is never a JSON value."""
    return "function" if callable(value) else "object"


def _exception_message(error: BaseException) -> str:
    message = getattr(error, "message", None)
    if isinstance(message, str):
        return message
    if len(error.args) == 1 and isinstance(error.args[0], str):
        return error.args[0]
    return str(error)


def get_error_details(error: object) -> JSONValue | None:
    if isinstance(error, BaseException):
        details: Any = getattr(error, "details", None)
        if details is not None and is_json_value(details):
            return details
        name = error_name(error)
        if name not in ("Exception", "Error"):
            return {"name": name}
        return None

    if isinstance(error, dict) and "details" in error:
        details = error["details"]
        if is_json_value(details):
            return details

    if is_json_value(error):
        return error  # ty: ignore[invalid-return-type]
    return None


def error_name(error: BaseException) -> str:
    """The `name` upstream would report for this error."""
    name = getattr(type(error), "error_name", None)
    return name if isinstance(name, str) else type(error).__name__
