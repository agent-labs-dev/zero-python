"""Upstream's `assert`, which, unlike Python's, survives `python -O`.

Ported from rocicorp/mono `packages/shared/src/asserts.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.
"""

from typing import Any, TypeGuard


class AssertionFailedError(Exception):
    """Upstream throws a plain `Error` here, so it reports no `name`."""

    error_name = "Error"


def check(condition: Any, message: str) -> TypeGuard[Any]:
    if not condition:
        raise AssertionFailedError(message)
    return True
