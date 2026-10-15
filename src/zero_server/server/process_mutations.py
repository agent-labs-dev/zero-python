"""The mutate endpoint: exactly-once processing of a push from zero-cache.

Ported from rocicorp/mono `packages/zero-server/src/process-mutations.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482), with `MutationAlreadyProcessedError` from
`packages/zero-cache/src/services/mutagen/error.ts`. Apache-2.0, Copyright Rocicorp.

Each mutation runs inside the same database transaction that bumps its client's `lastMutationID`.
The bump comes first and the comparison second, so a duplicate or a gap is found after the bump and
undone by rolling that transaction back. A mutation whose mutator fails is retried without the
mutator: the second transaction takes the ID and records the error in the `mutations` table, which is
where the client's pending mutation learns it failed.
"""

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import TypeAdapter, ValidationError

from zero_server.protocol.application_error import ApplicationError, wrap_with_application_error
from zero_server.protocol.error import ErrorReason, ServerPushFailed
from zero_server.protocol.mutate_server import MutateParams, MutateSuccess
from zero_server.protocol.mutation import (
    CLEANUP_RESULTS_MUTATION_NAME,
    CleanupResultsArg,
    CleanupResultsBulkArg,
    CustomMutation,
    MutationAppError,
    MutationOk,
    MutationResponse,
    MutationZeroError,
)
from zero_server.protocol.mutation_id import MutationID
from zero_server.protocol.push import PushBody
from zero_server.protocol.wire import parse_wire
from zero_server.shared.asserts import check
from zero_server.shared.error import get_error_details, get_error_message
from zero_server.shared.js import js_number, js_truthy
from zero_server.shared.json import JSONValue

logger = logging.getLogger("zero_server.mutate")

_cleanup_results_arg: TypeAdapter[CleanupResultsArg] = TypeAdapter(CleanupResultsArg)


class TransactionProviderHooks(Protocol):
    """The three statements against zero-cache's bookkeeping tables, run in the mutation's transaction."""

    async def update_client_mutation_id(self) -> int: ...

    async def write_mutation_result(self, result: MutationResponse) -> None: ...

    async def delete_mutation_results(self, args: CleanupResultsArg) -> None: ...


@dataclass(frozen=True)
class TransactionProviderInput:
    upstream_schema: str
    client_group_id: str
    client_id: str
    mutation_id: int | float


class Database[T](Protocol):
    """A database `handle_mutate_request` can run transactions against.

    `transaction` opens one transaction, calls `callback` inside it with the app's transaction object and
    the bookkeeping hooks, and commits when `callback` returns.
    """

    async def transaction[R](
        self,
        callback: Callable[[T, TransactionProviderHooks], Awaitable[R]],
        transaction_input: TransactionProviderInput | None = None,
    ) -> R: ...


type TransactFnCallback[T] = Callable[[T, str, JSONValue | None], Awaitable[None]]
type TransactFn[T] = Callable[[TransactFnCallback[T]], Awaitable[MutationResponse]]
type MutateRequestHandler[T] = Callable[[TransactFn[T], CustomMutation], Awaitable[MutationResponse]]
"""Runs once per custom mutation.

Call `transact` to run the mutation's database work. Raising before `transact` records an application
error for the mutation; raising after it only reaches the log, because the mutation has committed.
"""

type MutateResponse = MutateSuccess | ServerPushFailed

type _DatabaseTransactionPhase = Literal["open", "execute"]
type _MutationPhase = Literal["preTransaction", "transactionPending", "postCommit"]


class OutOfOrderMutation(Exception):
    error_name = "Error"

    def __init__(self, client_id: str, received_mutation_id: float, last_mutation_id: float) -> None:
        super().__init__(
            f"Client {client_id} sent mutation ID {js_number(received_mutation_id)} "
            f"but expected {js_number(last_mutation_id)}"
        )


class MutationAlreadyProcessedError(Exception):
    error_name = "Error"

    def __init__(self, client_id: str, received: float, actual: float) -> None:
        super().__init__(
            f"Ignoring mutation from {client_id} with ID {js_number(received)} as it was already processed. "
            f"Expected: {js_number(actual)}"
        )
        check(
            received < actual,
            f"MutationAlreadyProcessedError: received ({js_number(received)}) must be < actual ({js_number(actual)})",
        )


class DatabaseTransactionError(Exception):
    def __init__(self, phase: _DatabaseTransactionPhase, cause: BaseException) -> None:
        message = (
            f"Failed to open database transaction: {get_error_message(cause)}"
            if phase == "open"
            else f"Database transaction failed after opening: {get_error_message(cause)}"
        )
        super().__init__(message)
        self.__cause__ = cause


_PASSES_THROUGH = (DatabaseTransactionError, OutOfOrderMutation, MutationAlreadyProcessedError, ApplicationError)


async def _application_error_wrapper[R](fn: Callable[[], Awaitable[R]]) -> R:
    try:
        return await fn()
    except _PASSES_THROUGH:
        raise
    except Exception as error:
        raise wrap_with_application_error(error) from error


def _refuse_constant(name: str) -> None:
    raise ValueError(f"{name} is not valid JSON")


def _parse_failure(
    subject: str, error: BaseException, mutation_ids: list[MutationID], *, syntax: bool = False
) -> ServerPushFailed:
    return ServerPushFailed(
        reason=ErrorReason.PARSE,
        message=f"Failed to parse {subject}: {get_error_message(error)}",
        mutationIDs=mutation_ids,
        # Upstream's parsers throw a `TypeError`, and `JSON.parse` a `SyntaxError`.
        details={"name": "SyntaxError" if syntax else "TypeError"},
    )


def _parse_push_body(json_body: object) -> PushBody | ServerPushFailed:
    try:
        return PushBody.from_json(json_body)
    except ValidationError as error:
        logger.error("Failed to parse push body", exc_info=error)
        return _parse_failure("push body", error, [])


async def handle_mutate_request[T](
    *,
    db_provider: Database[T],
    handler: MutateRequestHandler[T],
    query: Mapping[str, str],
    body: object,
    user_id: str | None,
) -> MutateResponse:
    """Process one `/mutate` request from zero-cache.

    `query` is the request's query string and `body` its JSON: raw bytes, decoded JSON, or a `PushBody`.
    `user_id` is the authenticated user, `None` when logged out; reporting it back is what makes
    zero-cache treat the connection's user as server-validated.

    A caller that needs push-level fields such as `timestamp` parses once with `PushBody.from_json` and
    passes the result. If that parse fails, pass the raw body instead and return the parse failure this
    produces.
    """
    return await handle_mutate(db_provider, handler, query, body, user_id, report_user_id=True)


async def handle_mutate[T](
    db_provider: Database[T],
    handler: MutateRequestHandler[T],
    query: Mapping[str, str],
    body: object,
    user_id: str | None,
    *,
    report_user_id: bool,
) -> MutateResponse:
    """`handle_mutate_request`, and the legacy form that reports no user ID at all."""
    match body:
        case PushBody():
            push_body = body
        case bytes():
            try:
                json_body = json.loads(body, parse_constant=_refuse_constant)
            except ValueError as error:
                logger.error("Failed to parse push body", exc_info=error)
                return _parse_failure("push body", error, [], syntax=True)
            push_body = _parse_push_body(json_body)
        case _:
            push_body = _parse_push_body(body)
    if isinstance(push_body, ServerPushFailed):
        return push_body
    mutation_ids = [MutationID(id=m.id, clientID=m.client_id) for m in push_body.mutations]

    try:
        params = MutateParams.from_json(dict(query))
    except ValidationError as error:
        logger.error("Failed to parse push query parameters", exc_info=error)
        return _parse_failure("push query parameters", error, mutation_ids)

    if push_body.push_version != 1:
        return ServerPushFailed(
            reason=ErrorReason.UNSUPPORTED_PUSH_VERSION,
            mutationIDs=mutation_ids,
            message=f"Unsupported push version: {js_number(push_body.push_version)}",
        )

    responses: list[MutationResponse] = []
    processed_count = 0

    try:
        transactor = _Transactor(db_provider, push_body, params)

        for m in push_body.mutations:
            if m.type == "custom" and m.name == CLEANUP_RESULTS_MUTATION_NAME:
                try:
                    await _process_cleanup_results_mutation(db_provider, m, params)
                except Exception as error:
                    # A failed cleanup costs disk, not correctness, so it does not fail the push.
                    logger.warning("Failed to process cleanup mutation for client %s", m.client_id, exc_info=error)
                processed_count += 1
                continue

            check(m.type == "custom", "Expected custom mutation")
            assert isinstance(m, CustomMutation)
            response = await _process_one(transactor, handler, m)
            responses.append(response)
            processed_count += 1

        if report_user_id:
            return MutateSuccess(mutations=responses, userID=user_id)
        return MutateSuccess(mutations=responses)
    except Exception as error:
        logger.error("Failed to process push request", exc_info=error)
        details = get_error_details(error)
        return ServerPushFailed(
            reason=(
                ErrorReason.OUT_OF_ORDER_MUTATION
                if isinstance(error, OutOfOrderMutation)
                else ErrorReason.DATABASE
                if isinstance(error, DatabaseTransactionError)
                else ErrorReason.INTERNAL
            ),
            message=get_error_message(error),
            # Only the mutations not yet processed.
            mutationIDs=mutation_ids[processed_count:],
            # Upstream drops falsy details, `0` and `""` included.
            details=details if js_truthy(details) else None,
        )


async def _process_one[T](
    transactor: "_Transactor[T]", handler: MutateRequestHandler[T], m: CustomMutation
) -> MutationResponse:
    phase: _MutationPhase = "preTransaction"

    async def transact(callback: TransactFnCallback[T]) -> MutationResponse:
        nonlocal phase
        phase = "transactionPending"
        result = await transactor.transact(m, callback)
        phase = "postCommit"
        return result

    try:
        return await _application_error_wrapper(lambda: handler(transact, m))
    except ApplicationError as error:
        if phase == "preTransaction":
            await transactor.persist_pre_transaction_failure(m, error)
        elif phase == "postCommit":  # pragma: no branch - `transact` never raises an ApplicationError
            logger.error(
                "Post-commit mutation handler failed for mutation %s for client %s", m.id, m.client_id, exc_info=error
            )
        logger.warning("Application error processing mutation %s for client %s", m.id, m.client_id, exc_info=error)
        return _app_error_response(m, error)


class _Transactor[T]:
    def __init__(self, db_provider: Database[T], push: PushBody, params: MutateParams) -> None:
        self._db = db_provider
        self._push = push
        self._params = params

    async def transact(self, mutation: CustomMutation, callback: TransactFnCallback[T]) -> MutationResponse:
        app_error: ApplicationError | None = None
        while True:
            try:
                result = await self._transact_impl(mutation, callback, app_error)
                if app_error is not None:
                    logger.warning(
                        "Mutation %s for client %s was retried after an error",
                        mutation.id,
                        mutation.client_id,
                        exc_info=app_error,
                    )
                    return _app_error_response(mutation, app_error)
                return result
            except OutOfOrderMutation as error:
                logger.error("%s", error)
                raise
            except MutationAlreadyProcessedError as error:
                logger.warning("%s", error)
                return MutationResponse(
                    id=MutationID(clientID=mutation.client_id, id=mutation.id),
                    result=MutationZeroError(error="alreadyProcessed", details=str(error)),
                )
            except Exception as error:
                if app_error is not None:
                    # The retry, which runs no mutator, failed too: nothing to skip to.
                    logger.error(
                        "Retry also failed for mutation %s for client %s",
                        mutation.id,
                        mutation.client_id,
                        exc_info=error,
                    )
                    raise
                original: BaseException = error
                if isinstance(error, DatabaseTransactionError) and error.__cause__ is not None:
                    original = error.__cause__
                app_error = wrap_with_application_error(original)
                logger.warning(
                    "Error processing mutation %s for client %s, retrying without mutator",
                    mutation.id,
                    mutation.client_id,
                    exc_info=app_error,
                )

    async def persist_pre_transaction_failure(self, mutation: CustomMutation, app_error: ApplicationError) -> None:
        """The handler raised before calling `transact`. The ID still advances and the failure is recorded."""

        await self._transact_impl(mutation, None, app_error)

    async def _transact_impl(
        self, mutation: CustomMutation, callback: TransactFnCallback[T] | None, app_error: ApplicationError | None
    ) -> MutationResponse:
        """Bump the ID, then run `callback`, or with `app_error` set, record that error in its place."""
        phase: _DatabaseTransactionPhase = "open"

        async def run(tx: T, hooks: TransactionProviderHooks) -> MutationResponse:
            nonlocal phase
            phase = "execute"
            await self._check_and_increment_last_mutation_id(hooks, mutation.client_id, mutation.id)
            if app_error is None:
                assert callback is not None
                logger.debug("Executing mutator '%s' (id=%s)", mutation.name, mutation.id)
                await callback(tx, mutation.name, mutation.args[0] if mutation.args else None)
            else:
                await hooks.write_mutation_result(_app_error_response(mutation, app_error))
            return MutationResponse(id=MutationID(clientID=mutation.client_id, id=mutation.id), result=MutationOk())

        try:
            return await self._db.transaction(
                run,
                TransactionProviderInput(
                    upstream_schema=self._params.schema_,
                    client_group_id=self._push.client_group_id,
                    client_id=mutation.client_id,
                    mutation_id=mutation.id,
                ),
            )
        except (ApplicationError, OutOfOrderMutation, MutationAlreadyProcessedError):
            raise
        except Exception as error:
            raise DatabaseTransactionError(phase, error) from error

    async def _check_and_increment_last_mutation_id(
        self, hooks: TransactionProviderHooks, client_id: str, received: float
    ) -> None:
        last_mutation_id = await hooks.update_client_mutation_id()
        if received < last_mutation_id:
            raise MutationAlreadyProcessedError(client_id, received, last_mutation_id)
        if received > last_mutation_id:
            raise OutOfOrderMutation(client_id, received, last_mutation_id)


def _app_error_response(m: CustomMutation, error: ApplicationError) -> MutationResponse:
    return MutationResponse(
        id=MutationID(clientID=m.client_id, id=m.id),
        result=MutationAppError(message=error.message, details=error.details if js_truthy(error.details) else None),
    )


async def _process_cleanup_results_mutation[T](
    db_provider: Database[T], mutation: CustomMutation, params: MutateParams
) -> None:
    """Deletes acknowledged results. An internal mutation, so it never touches `lastMutationID`."""
    try:
        args = parse_wire(_cleanup_results_arg, mutation.args[0] if mutation.args else None)
    except ValidationError as error:
        logger.warning("Cleanup mutation has invalid args", exc_info=error)
        return

    client_id = args.client_ids[0] if isinstance(args, CleanupResultsBulkArg) else args.client_id

    async def delete(_tx: T, hooks: TransactionProviderHooks) -> None:
        await hooks.delete_mutation_results(args)

    # Only the upstream schema matters to the delete; the rest fills the interface.
    await db_provider.transaction(
        delete,
        TransactionProviderInput(
            upstream_schema=params.schema_,
            client_group_id=args.client_group_id,
            client_id=client_id,
            mutation_id=0,
        ),
    )
