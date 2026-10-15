"""The Python twins of `harness/errors.ts`: what a mutator or query raises, by name."""

import json
from collections.abc import Callable

from zero_server.protocol.application_error import ApplicationError


class NamedError(Exception):
    pass


def _with(error: BaseException, **attributes: object) -> BaseException:
    for name, value in attributes.items():
        setattr(error, name, value)
    return error


def _caused(error: BaseException, cause: BaseException) -> BaseException:
    error.__cause__ = cause
    return error


def _circular() -> BaseException:
    error = Exception("")
    error.__cause__ = error
    return error


SHAPES: dict[str, Callable[[], BaseException]] = {
    "empty": lambda: Exception(""),
    "cause": lambda: _caused(Exception(""), Exception("inner")),
    "emptyCause": lambda: _caused(Exception(""), Exception("")),
    "deepCause": lambda: _caused(Exception(""), _caused(Exception(""), TypeError("deepest"))),
    "circular": _circular,
    "props": lambda: _with(Exception(""), code=5, tags=["a"]),
    "unknown": lambda: _with(Exception(""), fn=lambda: 1),
    "details": lambda: _with(Exception("m"), details={"a": [1, None]}),
    "badDetails": lambda: _with(Exception("m"), details=lambda: 1),
    "nullDetails": lambda: _with(TypeError("m"), details=None),
    "nanDetails": lambda: _with(Exception("m"), details={"x": float("nan"), "y": float("inf")}),
    "named": lambda: NamedError("named"),
    "numberMessage": lambda: Exception(5),
    "deepDetails": lambda: _with(Exception("m"), details=json.loads("[" * 300 + "]" * 300)),
    "appErrorCause": lambda: ApplicationError("", cause=Exception("inner"), details=0),
}
