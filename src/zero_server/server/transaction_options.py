"""The isolation level a mutator transaction runs at, an option of every adapter.

Mirrors `TransactionOptions` in rocicorp/mono's zero-server (transaction-options.ts) at zero/v1.11.4
(be94346faaf4ee7e7b2cf983f6e5accae3a6b4fb). Apache-2.0, Copyright Rocicorp.
"""

from typing import Literal

type IsolationLevel = Literal["read committed", "repeatable read", "serializable"]
"""Spelled as Postgres spells it. Left unset, a transaction opens with a bare `BEGIN` and the database
decides, which is `read committed` on a stock Postgres."""


def set_isolation_statement(isolation_level: IsolationLevel) -> str:
    """The statement that sets a transaction's isolation level: its first, before any query."""
    return f"SET TRANSACTION ISOLATION LEVEL {isolation_level.upper()}"
