"""The mutate endpoint: exactly-once processing of a push from zero-cache.

Ported from rocicorp/mono `packages/zero-server/src/process-mutations.ts` at zero/v1.11.3
(e91e55f05b7a2c631f77238e1dd6fadf1029a538), with `MutationAlreadyProcessedError` from
`packages/zero-cache/src/services/mutagen/error.ts`. Apache-2.0, Copyright Rocicorp.

Each mutation runs inside the same database transaction that bumps its client's `lastMutationID`.
The bump comes first and the comparison second, so a duplicate or a gap is found after the bump and
undone by rolling that transaction back. A mutation whose mutator fails is retried without the
mutator: the second transaction takes the ID and records the error in the `mutations` table, which is
where the client's pending mutation learns it failed. A mutator that registered `tx.retry_on` predicates
may first be re-run, in a fresh transaction, when one of them says yes.
"""

import asyncio
import functools
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, nullcontext
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
from zero_server.protocol.schemas import mutate_params_schema, push_body_schema, refusal
from zero_server.protocol.wire import parse_wire
from zero_server.server.transaction import RetryPredicate
from zero_server.shared.asserts import check
from zero_server.shared.error import error_name, get_error_details, get_error_message
from zero_server.shared.js import js_number, js_truthy
from zero_server.shared.json import JSONValue
from zero_server.shared.json_parse import parse_body

logger = logging.getLogger("zero_server.mutate")


@functools.cache
def _cleanup_results_arg() -> TypeAdapter[CleanupResultsArg]:
    return TypeAdapter(CleanupResultsArg)


_OK = MutationOk.trusted()  # shared by every successful mutation; unvalidated, so importing builds no validator


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
    # Receives each predicate the mutator registers with `tx.retry_on()`. A `Database` passes it on to
    # the server transaction it creates.
    add_retry_predicate: Callable[[RetryPredicate], None] | None = None


class Database[T](Protocol):
    """A database `handle_mutate_request` can run transactions against.

    `transaction` opens one transaction, calls `callback` inside it with the app's transaction object and
    the bookkeeping hooks, and commits when `callback` returns.

    A database may also have a `session()`: an async context manager yielding a `Database` whose
    transactions all run on one connection. A push's mutations run in one session, `ZQLDatabase`'s.
    """

    async def transaction[R](
        self,
        callback: Callable[[T, TransactionProviderHooks], Awaitable[R]],
        transaction_input: TransactionProviderInput | None = None,
    ) -> R: ...


class _SessionOpener[T](Protocol):
    def session(self) -> AbstractAsyncContextManager[Database[T]]: ...


def _session[T](db_provider: Database[T]) -> AbstractAsyncContextManager[Database[T]]:
    """The database a push's mutations run on: a session of `db_provider` if it opens them, so the push
    takes one connection from the pool for all its transactions, or else `db_provider` itself."""
    if hasattr(db_provider, "session"):
        opener: _SessionOpener[T] = db_provider  # ty: ignore[invalid-assignment]
        return opener.session()
    return nullcontext(db_provider)


type TransactFnCallback[T] = Callable[[T, str, JSONValue | None], Awaitable[JSONValue | None]]
"""Runs the mutator. What it returns, unless `None`, is the mutation's result data."""
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


def _parse_failure(
    subject: str, message: str, mutation_ids: list[MutationID], *, name: str = "TypeError"
) -> ServerPushFailed:
    return ServerPushFailed(
        reason=ErrorReason.PARSE,
        message=f"Failed to parse {subject}: {message}",
        mutationIDs=mutation_ids,
        # Upstream's parsers throw a `TypeError`, and `JSON.parse` a `SyntaxError`.
        details={"name": name},
    )


def _parse_push_body(body: object) -> PushBody | ServerPushFailed:
    try:
        return PushBody.from_json(body)
    except ValidationError as error:
        logger.error("Failed to parse push body", exc_info=error)
        decoded = parse_body(body) if isinstance(body, bytes) else body
        message = refusal(decoded, push_body_schema, "passthrough", get_error_message(error))
        return _parse_failure("push body", message, [])
    except ValueError as error:
        # The body's bytes are not JSON: `JSON.parse`'s SyntaxError, or a RangeError for one nested too deep.
        logger.error("Failed to parse push body", exc_info=error)
        return _parse_failure("push body", str(error), [], name=error_name(error))


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

    With a database that opens sessions, as `ZQLDatabase` does, the push's mutations share one pooled
    connection: they see each other's session state, and a handler that takes another connection from
    the same pool can deadlock when the pool is smaller than the number of concurrent pushes.
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
    push_body = body if isinstance(body, PushBody) else _parse_push_body(body)
    if isinstance(push_body, ServerPushFailed):
        return push_body
    # Built unvalidated: the push body's own parse checked every ID.
    mutation_ids = [MutationID.trusted(id=m.id, client_id=m.client_id) for m in push_body.mutations]

    try:
        params = MutateParams.from_query(query)
    except ValidationError as error:
        logger.error("Failed to parse push query parameters", exc_info=error)
        message = refusal(dict(query), mutate_params_schema, "passthrough", get_error_message(error))
        return _parse_failure("push query parameters", message, mutation_ids)

    if push_body.push_version != 1:
        return ServerPushFailed(
            reason=ErrorReason.UNSUPPORTED_PUSH_VERSION,
            mutationIDs=mutation_ids,
            message=f"Unsupported push version: {js_number(push_body.push_version)}",
        )

    responses: list[MutationResponse] = []
    processed_count = 0

    try:
        async with _session(db_provider) as db:
            transactor = _Transactor(db, push_body, params)

            for m, mutation_id in zip(push_body.mutations, mutation_ids, strict=True):
                if m.type == "custom" and m.name == CLEANUP_RESULTS_MUTATION_NAME:
                    try:
                        await _process_cleanup_results_mutation(db, m, params)
                    except Exception as error:
                        # A failed cleanup costs disk, not correctness, so it does not fail the push.
                        logger.warning("Failed to process cleanup mutation for client %s", m.client_id, exc_info=error)
                    processed_count += 1
                    continue

                check(m.type == "custom", "Expected custom mutation")
                assert isinstance(m, CustomMutation)
                response = await _process_one(transactor, handler, m, mutation_id)
                responses.append(response)
                processed_count += 1

            if not report_user_id:
                return MutateSuccess.trusted(mutations=responses)
            if user_id is None or type(user_id) is str:
                return MutateSuccess.trusted(mutations=responses, user_id=user_id)
            return MutateSuccess(mutations=responses, userID=user_id)  # python-only: a user ID that is not a str
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
    transactor: "_Transactor[T]", handler: MutateRequestHandler[T], m: CustomMutation, mutation_id: MutationID
) -> MutationResponse:
    phase: _MutationPhase = "preTransaction"

    async def transact(callback: TransactFnCallback[T]) -> MutationResponse:
        nonlocal phase
        phase = "transactionPending"
        result = await transactor.transact(m, mutation_id, callback)
        phase = "postCommit"
        return result

    try:
        # Upstream's `applicationErrorWrapper`, inline.
        try:
            return await handler(transact, m)
        except _PASSES_THROUGH:
            raise
        except Exception as error:
            raise wrap_with_application_error(error) from error
    except ApplicationError as error:
        if phase == "preTransaction":
            await transactor.persist_pre_transaction_failure(m, mutation_id, error)
        elif phase == "postCommit":  # pragma: no branch - `transact` never raises an ApplicationError
            logger.error(
                "Post-commit mutation handler failed for mutation %s for client %s", m.id, m.client_id, exc_info=error
            )
        logger.warning("Application error processing mutation %s for client %s", m.id, m.client_id, exc_info=error)
        return _app_error_response(mutation_id, error)


MAX_MUTATOR_RUNS = 5
"""The most times a mutator runs for one mutation within one push when its `tx.retry_on()` predicate keeps
answering yes: the first run and four re-runs."""

# Predicates still running after another answered yes. Their answers are ignored, as upstream ignores
# them, but a task needs a reference until it finishes.
_unanswered: set[asyncio.Task[bool]] = set()


async def _ask(should_retry: RetryPredicate, error: BaseException, attempt: int) -> bool:
    try:
        answer = should_retry(error, attempt)
        if inspect.isawaitable(answer):
            answer = await answer
        return answer is True
    except Exception as retry_error:
        logger.warning("retryOn predicate threw; treating it as no", exc_info=retry_error)
        return False


async def _should_retry(predicates: list[RetryPredicate], error: BaseException, attempt: int) -> bool:
    """Asks all of a run's predicates at once whether to run the mutator again. The first to answer yes
    decides it; it is no once every predicate has answered no. A predicate that raises answers no:
    escaping here would skip recording the mutation's result, and the client would send it again."""
    if not predicates:
        return False
    pending = {asyncio.ensure_future(_ask(p, error, attempt)) for p in predicates}
    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        if any(task.result() for task in done):
            for task in pending:
                _unanswered.add(task)
                task.add_done_callback(_unanswered.discard)
            return True
    return False


class _Transactor[T]:
    def __init__(self, db_provider: Database[T], push: PushBody, params: MutateParams) -> None:
        self._db = db_provider
        self._push = push
        self._params = params

    async def transact(
        self, mutation: CustomMutation, mutation_id: MutationID, callback: TransactFnCallback[T]
    ) -> MutationResponse:
        app_error: ApplicationError | None = None
        # Which run of the mutator this is: 1, then 2 for the first re-run, and so on. When a run fails,
        # it is also the number of the re-run being decided.
        attempt = 1
        while True:
            # What this run registers with `tx.retry_on()`. Each run starts empty.
            predicates: list[RetryPredicate] = []
            try:
                result = await self._transact_impl(mutation, mutation_id, callback, app_error, predicates.append)
                if app_error is not None:
                    logger.warning(
                        "Mutation %s for client %s was retried after an error",
                        mutation.id,
                        mutation.client_id,
                        exc_info=app_error,
                    )
                    return _app_error_response(mutation_id, app_error)
                return result
            except OutOfOrderMutation as error:
                logger.error("%s", error)
                raise
            except MutationAlreadyProcessedError as error:
                logger.warning("%s", error)
                return MutationResponse(
                    id=mutation_id,
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
                # What the mutator or the database raised, without the wrapper. This includes errors
                # raised at COMMIT, which the mutator cannot catch.
                original: BaseException = error
                if isinstance(error, DatabaseTransactionError) and error.__cause__ is not None:
                    original = error.__cause__
                # Re-running without setting `app_error` is the whole mechanism: `app_error` being set
                # is what makes the next pass skip the mutator. Each pass opens a new transaction, and
                # the failed run rolled its ID bump back.
                if attempt < MAX_MUTATOR_RUNS and await _should_retry(predicates, original, attempt):
                    logger.warning(
                        "Mutation %s for client %s failed, re-running mutator (retry %s)",
                        mutation.id,
                        mutation.client_id,
                        attempt,
                        exc_info=original,
                    )
                    attempt += 1
                    continue
                app_error = wrap_with_application_error(original)
                logger.warning(
                    "Error processing mutation %s for client %s, retrying without mutator",
                    mutation.id,
                    mutation.client_id,
                    exc_info=app_error,
                )

    async def persist_pre_transaction_failure(
        self, mutation: CustomMutation, mutation_id: MutationID, app_error: ApplicationError
    ) -> None:
        """The handler raised before calling `transact`. The ID still advances and the failure is recorded."""

        await self._transact_impl(mutation, mutation_id, None, app_error, None)

    async def _transact_impl(
        self,
        mutation: CustomMutation,
        mutation_id: MutationID,
        callback: TransactFnCallback[T] | None,
        app_error: ApplicationError | None,
        add_retry_predicate: Callable[[RetryPredicate], None] | None,
    ) -> MutationResponse:
        """Bump the ID, then run `callback`, or with `app_error` set, record that error in its place."""
        phase: _DatabaseTransactionPhase = "open"

        async def run(tx: T, hooks: TransactionProviderHooks) -> MutationResponse:
            nonlocal phase
            phase = "execute"
            last_mutation_id = await hooks.update_client_mutation_id()
            if mutation.id < last_mutation_id:
                raise MutationAlreadyProcessedError(mutation.client_id, mutation.id, last_mutation_id)
            if mutation.id > last_mutation_id:
                raise OutOfOrderMutation(mutation.client_id, mutation.id, last_mutation_id)
            if app_error is None:
                assert callback is not None
                logger.debug("Executing mutator '%s' (id=%s)", mutation.name, mutation.id)
                data = await callback(tx, mutation.name, mutation.args[0] if mutation.args else None)
                if data is None:
                    return MutationResponse.trusted(id=mutation_id, result=_OK)
                # Persisted so the data still reaches the client if the push response is lost. A mutator
                # that returns nothing writes no row.
                success = MutationResponse.trusted(id=mutation_id, result=MutationOk.trusted(data=data))
                await hooks.write_mutation_result(success)
                return success
            response = _app_error_response(mutation_id, app_error)
            await hooks.write_mutation_result(response)
            return response

        try:
            return await self._db.transaction(
                run,
                TransactionProviderInput(
                    upstream_schema=self._params.schema_,
                    client_group_id=self._push.client_group_id,
                    client_id=mutation.client_id,
                    mutation_id=mutation.id,
                    add_retry_predicate=add_retry_predicate,
                ),
            )
        except (ApplicationError, OutOfOrderMutation, MutationAlreadyProcessedError):
            raise
        except Exception as error:
            raise DatabaseTransactionError(phase, error) from error


def _app_error_response(mutation_id: MutationID, error: ApplicationError) -> MutationResponse:
    return MutationResponse(
        id=mutation_id,
        result=MutationAppError(message=error.message, details=error.details if js_truthy(error.details) else None),
    )


async def _process_cleanup_results_mutation[T](
    db_provider: Database[T], mutation: CustomMutation, params: MutateParams
) -> None:
    """Deletes acknowledged results. An internal mutation, so it never touches `lastMutationID`."""
    try:
        args = parse_wire(_cleanup_results_arg(), mutation.args[0] if mutation.args else None)
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
