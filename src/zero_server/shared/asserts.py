"""Upstream's `assert`, which, unlike Python's, survives `python -O`.

Ported from rocicorp/mono `packages/shared/src/asserts.ts` at zero/v1.11.3
(e91e55f05b7a2c631f77238e1dd6fadf1029a538). Apache-2.0, Copyright Rocicorp.
"""

from typing import Any, TypeGuard


class AssertionFailedError(Exception):
    """Upstream throws a plain `Error` here, so it reports no `name`."""

    error_name = "Error"


def check(condition: Any, message: str) -> TypeGuard[Any]:
    if not condition:
        raise AssertionFailedError(message)
    return True
