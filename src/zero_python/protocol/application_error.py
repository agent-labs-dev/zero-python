"""The error application code raises to send a message and JSON details back to the client.

Ported from rocicorp/mono `packages/zero-protocol/src/application-error.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.
"""

from typing import Literal

from zero_python.shared.error import get_error_details, get_error_message
from zero_python.shared.json import JSONValue


class ApplicationError(Exception):
    def __init__(self, message: str, *, details: JSONValue | None = None, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        self.__cause__ = cause

    @property
    def kind(self) -> Literal["Application"]:
        return "Application"


def is_application_error(error: object) -> bool:
    return isinstance(error, ApplicationError)


def wrap_with_application_error(error: object) -> ApplicationError:
    if isinstance(error, ApplicationError):
        return error
    return ApplicationError(
        get_error_message(error),
        cause=error if isinstance(error, BaseException) else None,
        details=get_error_details(error),
    )
