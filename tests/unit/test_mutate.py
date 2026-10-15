"""Branches of the mutate endpoint the wire tier cannot reach: raw bodies upstream would read off a Request,
and bodies the caller has already parsed."""

import json
from typing import Any

import pytest

from zero_server.protocol.mutation import CustomMutation, MutationResponse
from zero_server.protocol.push import PushBody
from zero_server.server.process_mutations import TransactFn, handle_mutate_request

QUERY = {"schema": "zero_0", "appID": "zero"}


class NoDatabase:
    async def transaction(self, callback: Any, transaction_input: Any = None) -> Any:
        raise AssertionError("no transaction expected")


async def handler(transact: TransactFn[Any], mutation: CustomMutation) -> MutationResponse:
    raise AssertionError("no mutation expected")


@pytest.mark.parametrize("body", [b"{", b'{"a": NaN}', b"\xff"])
async def test_unparseable_body(body: bytes) -> None:
    response = await handle_mutate_request(
        db_provider=NoDatabase(), handler=handler, query=QUERY, body=body, user_id=None
    )
    assert response.to_json() == {
        "kind": "PushFailed",
        "origin": "server",
        "reason": "parse",
        "message": response.to_json()["message"],
        "mutationIDs": [],
        "details": {"name": "SyntaxError"},
    }
    assert str(response.to_json()["message"]).startswith("Failed to parse push body: ")


async def test_raw_body_is_parsed() -> None:
    body = b'{"clientGroupID": "g", "mutations": [], "pushVersion": 1, "timestamp": 1, "requestID": "r"}'
    response = await handle_mutate_request(
        db_provider=NoDatabase(), handler=handler, query=QUERY, body=body, user_id="u"
    )
    assert response.to_json() == {"kind": "MutateResponse", "userID": "u", "mutations": []}


PUSH = {"clientGroupID": "g", "mutations": [], "pushVersion": 1, "timestamp": 1, "requestID": "r"}
MUTATION = {"type": "custom", "id": 1, "clientID": "c", "name": "issue.create", "args": [], "timestamp": 1}


class AcceptingDatabase:
    """Accepts each mutation's ID as the next one, and records nothing."""

    async def transaction(self, callback: Any, transaction_input: Any = None) -> Any:
        class Hooks:
            async def update_client_mutation_id(self) -> int:
                return transaction_input.mutation_id

            async def write_mutation_result(self, result: MutationResponse) -> None:
                pass

        return await callback(None, Hooks())


async def failing_handler(transact: TransactFn[Any], mutation: CustomMutation) -> MutationResponse:
    raise ValueError("no such issue")


@pytest.mark.parametrize(
    ("push", "query"),
    [
        (PUSH, QUERY),
        ({**PUSH, "mutations": [MUTATION]}, QUERY),
        ({**PUSH, "mutations": [MUTATION], "pushVersion": 2}, QUERY),
        ({**PUSH, "mutations": [MUTATION]}, {"appID": "zero"}),
    ],
    ids=["success", "mutation-error", "unsupported-push-version", "bad-query"],
)
async def test_parsed_body_matches_raw(push: dict[str, Any], query: dict[str, str]) -> None:
    async def respond(body: object) -> object:
        response = await handle_mutate_request(
            db_provider=AcceptingDatabase(), handler=failing_handler, query=query, body=body, user_id="u"
        )
        return response.to_json()

    assert await respond(PushBody.from_json(push)) == await respond(json.dumps(push).encode())


async def test_parsed_body_is_not_validated_again(monkeypatch: pytest.MonkeyPatch) -> None:
    push_body = PushBody.from_json(PUSH)

    def refuse(value: object) -> PushBody:
        raise AssertionError("parsed body validated again")

    monkeypatch.setattr(PushBody, "from_json", refuse)
    response = await handle_mutate_request(
        db_provider=NoDatabase(), handler=handler, query=QUERY, body=push_body, user_id="u"
    )
    assert response.to_json() == {"kind": "MutateResponse", "userID": "u", "mutations": []}
