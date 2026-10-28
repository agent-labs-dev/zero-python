"""The error application code raises to send a message and JSON details back to the client.

Ported from rocicorp/mono `packages/zero-protocol/src/application-error.ts` at zero/v1.11.3
(e91e55f05b7a2c631f77238e1dd6fadf1029a538). Apache-2.0, Copyright Rocicorp.
"""

from typing import Literal

from zero_server.shared.error import get_error_details, get_error_message
from zero_server.shared.json import JSONValue


class ApplicationError(Exception):
    # Upstream's constructor sets the name, so a subclass reports it too.
    error_name = "ApplicationError"

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


def wrap_with_application_error(error: BaseException) -> ApplicationError:
    if isinstance(error, ApplicationError):
        return error
    return ApplicationError(get_error_message(error), cause=error, details=get_error_details(error))
