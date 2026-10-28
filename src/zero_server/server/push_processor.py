"""`PushProcessor`: the older entry point to the mutate endpoint, dispatching to a mutator registry.

Ported from rocicorp/mono `packages/zero-server/src/push-processor.ts` at zero/v1.11.3
(e91e55f05b7a2c631f77238e1dd6fadf1029a538). Apache-2.0, Copyright Rocicorp.

Unlike `handle_mutate_request`, it reports no user ID to zero-cache.
"""

import re
from collections.abc import Mapping
from typing import Any

from zero_server.mutators import Mutator
from zero_server.protocol.mutation import CustomMutation, MutationResponse
from zero_server.server.process_mutations import Database, MutateResponse, TransactFn, handle_mutate
from zero_server.shared.asserts import AssertionFailedError, check
from zero_server.shared.json import JSONValue
from zero_server.shared.registry import Registry, value_at_path

# Older mutators were named with `|`, newer ones with `.`.
SEPARATOR = re.compile(r"[.|]")


class PushProcessor[T]:
    def __init__(self, db_provider: Database[T], context: Any = None) -> None:
        self._db = db_provider
        self._context = context

    async def process(
        self, mutators: Registry | Mapping[str, Any], query: Mapping[str, str], body: object
    ) -> MutateResponse:
        """Process a push. A mutator is a `Mutator` from `define_mutators`, or a plain `async (tx, args)`."""
        check(body is not None, "body is required when using query params directly")

        async def handler(transact: TransactFn[T], _mutation: CustomMutation) -> MutationResponse:
            async def dispatch(tx: T, name: str, args: JSONValue | None) -> JSONValue | None:
                return await self._dispatch(mutators, tx, name, args)

            return await transact(dispatch)

        return await handle_mutate(self._db, handler, query, body, None, report_user_id=False)

    async def _dispatch(self, mutators: Mapping[str, Any], tx: T, key: str, args: JSONValue | None) -> JSONValue | None:
        mutator = value_at_path(mutators, key, SEPARATOR)
        if not callable(mutator) or isinstance(mutator, Mapping):  # `check`, formatting only on failure
            raise AssertionFailedError(f"could not find mutator {key}")
        if isinstance(mutator, Mutator):
            return await mutator.fn(args=args, ctx=self._context, tx=tx)
        return await mutator(tx, args)
