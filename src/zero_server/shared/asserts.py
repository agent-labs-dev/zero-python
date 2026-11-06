"""Upstream's `assert`, which, unlike Python's, survives `python -O`.

Ported from rocicorp/mono `packages/shared/src/asserts.ts` at zero/v1.11.4
(be94346faaf4ee7e7b2cf983f6e5accae3a6b4fb). Apache-2.0, Copyright Rocicorp.
"""

from typing import Any, TypeGuard


class AssertionFailedError(Exception):
    """Upstream throws a plain `Error` here, so it reports no `name`."""

    error_name = "Error"


def check(condition: Any, message: str) -> TypeGuard[Any]:
    if not condition:
        raise AssertionFailedError(message)
    return True
