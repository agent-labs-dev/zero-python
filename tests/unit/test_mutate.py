"""Branches of the mutate endpoint the wire tier cannot reach: raw bodies upstream would read off a Request."""

from typing import Any

import pytest

from zero_server.protocol.mutation import CustomMutation, MutationResponse
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
