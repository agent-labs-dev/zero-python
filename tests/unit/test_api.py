"""The Python-side API around the ported code: loaders, registries, builders and their refusals."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter

from zero_server.mutators import define_mutator, define_mutators, get_mutator, must_get_mutator
from zero_server.protocol import ast as ast_module
from zero_server.protocol.ast import AST, CorrelatedSubquery, Correlation, map_ast
from zero_server.query.builder import create_builder
from zero_server.query.registry import (
    InputValidationError,
    QueryParseError,
    define_queries,
    define_query,
    get_query,
    must_get_query,
)
from zero_server.schema import load_schema
from zero_server.server.adapters.sqlalchemy import sqlalchemy_transaction
from zero_server.server.process_queries import handle_query_request
from zero_server.server.server_schema import get_server_schema
from zero_server.server.transaction import ServerTransaction
from zero_server.server.zql_database import ZQLDatabase
from zero_server.shared.asserts import AssertionFailedError

SCHEMA = {
    "tables": {
        "issue": {
            "name": "issue",
            "serverName": "issues",
            "columns": {"id": {"type": "string"}, "ownerID": {"type": "string", "serverName": "owner_id"}},
            "primaryKey": ["id"],
        },
        "plain": {"name": "plain", "columns": {"id": {"type": "string"}}, "primaryKey": ["id"]},
    },
    "relationships": {},
}


def test_load_schema_from_text_path_or_object(tmp_path: Path) -> None:
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(SCHEMA))
    assert load_schema(path) == load_schema(json.dumps(SCHEMA)) == load_schema(SCHEMA)


def test_load_schema_str_that_is_not_json_hints_at_path() -> None:
    with pytest.raises(json.JSONDecodeError, match=r"pathlib\.Path") as raised:
        load_schema("schema.json")
    assert isinstance(raised.value.__cause__, json.JSONDecodeError)


def test_builder_tables() -> None:
    zql = create_builder(load_schema(SCHEMA))
    assert zql.issue.table == "issue"
    condition: Any = zql.issue.expression_builder().eb.cmp("id", 1).to_json()
    assert condition["op"] == "="
    with pytest.raises(AttributeError):
        _ = zql.nope


@pytest.mark.parametrize(
    ("limit", "message"),
    [
        # Values JSON cannot carry to the differential tier, as JavaScript would treat them.
        (float("inf"), "Limit must be an integer"),
        (float("nan"), "Limit must be an integer"),
        (float("-inf"), "Limit must be non-negative"),
        ("-Infinity", "Limit must be non-negative"),
        (["-1"], "Limit must be non-negative"),
        (" -1e2 ", "Limit must be non-negative"),
        ("-x", "Limit must be an integer"),
        ({}, "Limit must be an integer"),
    ],
)
def test_limit_refuses_what_javascript_would(limit: Any, message: str) -> None:
    with pytest.raises(Exception, match=message):
        create_builder(load_schema(SCHEMA)).issue.limit(limit)


def test_each_literal_is_its_own_list() -> None:
    eb = create_builder(load_schema(SCHEMA)).issue.expression_builder()
    first: Any = eb.cmp("id", "IN", ())
    second: Any = eb.cmp("id", "IN", ())
    assert first.right.value == second.right.value == []
    assert first.right.value is not second.right.value


class Args(BaseModel):
    id: str


def test_validators() -> None:
    zql = create_builder(load_schema(SCHEMA))
    queries = define_queries(
        {
            "byModel": define_query(Args, lambda *, args, ctx: zql.issue.where("id", args.id)),
            "byAdapter": define_query(TypeAdapter(int), lambda *, args, ctx: zql.issue.limit(args)),
        }
    )
    assert must_get_query(queries, "byModel").fn(args={"id": "a"}).ast.where is not None
    assert must_get_query(queries, "byAdapter").fn(args=3).ast.limit == 3
    with pytest.raises(InputValidationError) as raised:
        must_get_query(queries, "byModel").fn(args={})
    assert raised.value.details["result"]["issues"][0]["path"] == ["id"]
    assert str(raised.value).startswith("Validation failed for query byModel: Field required")


def test_query_registry() -> None:
    zql = create_builder(load_schema(SCHEMA))
    base = define_queries({"issue": {"all": define_query(lambda *, args, ctx: zql.issue)}})
    merged = define_queries(base, {"issue": {"one": define_query(lambda *, args, ctx: zql.issue.one())}, "x": {}})
    assert sorted(merged.issue) == ["all", "one"]
    assert len(merged) == 2
    assert get_query(merged, "issue") is merged.issue
    assert get_query(merged, "issue.nope.deeper") is None
    request = merged.issue.one({"a": 1})
    assert (request.query, request.args) == (merged.issue.one, {"a": 1})
    with pytest.raises(AttributeError):
        _ = merged.nope


async def test_mutator_registry() -> None:
    seen: list[Any] = []

    async def record(*, args: Any, ctx: Any, tx: Any) -> None:
        seen.append((args, ctx, tx))

    mutators = define_mutators({"issue": {"create": define_mutator(TypeAdapter(int), record)}})
    mutators = define_mutators(mutators, {"issue": {"delete": define_mutator(record)}})
    await must_get_mutator(mutators, "issue.create").fn(args=1, ctx="c", tx="t")
    assert seen == [(1, "c", "t")]
    assert get_mutator(mutators, "issue|create") is None
    assert get_mutator(mutators, "issue") is mutators.issue
    request = mutators.issue.delete(5)
    assert (request.mutator, request.args) == (mutators.issue.delete, 5)
    with pytest.raises(Exception, match=r"Mutator not found: nope"):
        must_get_mutator(mutators, "nope")
    assert define_mutators(mutators) is mutators


def test_query_parse_error_message() -> None:
    assert str(QueryParseError()) == "Failed to parse arguments for query"
    assert str(QueryParseError(ValueError("bad"))) == "Failed to parse arguments for query: bad"


def test_mutate_needs_a_schema() -> None:
    with pytest.raises(RuntimeError, match=r"tx\.mutate needs a schema"):
        _ = ServerTransaction(db_transaction=None, client_id="c", mutation_id=1).mutate  # ty: ignore[invalid-argument-type]


async def test_run_needs_a_schema() -> None:
    class Bare:
        async def transaction(self, fn: Any) -> Any: ...

        async def query(self, sql: str, params: Any = ()) -> Any: ...

    with pytest.raises(RuntimeError, match=r"tx\.run needs a schema"):
        await ZQLDatabase(Bare()).run(create_builder(load_schema(SCHEMA)).plain)


async def test_server_schema_of_no_tables() -> None:
    assert await get_server_schema(None, load_schema({"tables": {}})) == {}  # ty: ignore[invalid-argument-type]


async def test_query_endpoint_takes_raw_bytes_and_async_handlers() -> None:
    schema = load_schema(SCHEMA)
    zql = create_builder(schema)

    async def handler(name: str, args: Any) -> Any:
        return zql.issue.where("ownerID", args)

    body = json.dumps(["transform", [{"id": "1", "name": "q", "args": ["u"]}]]).encode()
    response: Any = (await handle_query_request(handler=handler, schema=schema, body=body, user_id=None)).to_json()
    assert response["queries"][0]["ast"]["table"] == "issues"

    failed = await handle_query_request(handler=handler, schema=schema, body=b"[", user_id=None)
    assert failed.to_json()["details"] == {"name": "SyntaxError"}

    def not_a_query(name: str, args: Any) -> Any:
        return object()

    internal = await handle_query_request(handler=not_a_query, schema=schema, body=json.loads(body), user_id="u")
    assert internal.to_json()["reason"] == "internal"


async def test_an_awaited_handler_that_raises_fails_only_its_query() -> None:
    async def handler(name: str, args: Any) -> Any:
        raise ValueError("nope")

    body = ["transform", [{"id": "1", "name": "q", "args": []}]]
    response: Any = (
        await handle_query_request(handler=handler, schema=load_schema(SCHEMA), body=body, user_id="u")
    ).to_json()
    assert response["queries"] == [
        {"error": "app", "id": "1", "name": "q", "message": "nope", "details": {"name": "ValueError"}}
    ]


def test_a_condition_needs_a_value() -> None:
    with pytest.raises(AssertionFailedError, match=r"Invalid condition\. Too few arguments\."):
        create_builder(load_schema(SCHEMA)).issue.where("id")


def test_map_ast_reuses_what_it_mapped_and_takes_keys_of_any_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    schema = load_schema(SCHEMA)
    mapper = schema.client_to_server_mapper

    def related(parent: Any, child: Any) -> AST:
        return AST(
            table="issue",
            related=(
                CorrelatedSubquery(
                    correlation=Correlation(parent_field=parent, child_field=child), subquery=AST(table="plain")
                ),
            ),
        )

    expected = {"parentField": ["owner_id"], "childField": ["id"]}
    listed = map_ast(related(["ownerID"], ["id"]), mapper)
    assert listed.to_json()["related"][0]["correlation"] == expected
    assert mapper.correlations == {}

    monkeypatch.setattr(ast_module, "_MAX_CORRELATIONS", 1)
    first = map_ast(related(("ownerID",), ("id",)), mapper).related
    assert first is not None
    assert map_ast(related(("ownerID",), ("id",)), mapper).related[0].correlation is first[0].correlation  # ty: ignore[not-subscriptable]
    map_ast(related(("id",), ("id",)), mapper)
    assert list(mapper.correlations) == [("issue", "plain", ("id",), ("id",))]


def test_simplify_unwraps_single_conditions() -> None:
    from zero_server.protocol.ast import Conjunction, Disjunction
    from zero_server.query.expression import FALSE, TRUE, cmp, simplify_condition

    eq = cmp("id", 1)
    assert simplify_condition(Conjunction(conditions=(eq,))) == eq
    assert simplify_condition(Conjunction(conditions=(eq, Disjunction(conditions=())))) == FALSE
    assert simplify_condition(Disjunction(conditions=(eq, Conjunction(conditions=())))) == TRUE


def test_mutate_by_attribute() -> None:
    from zero_server.server.crud import Mutate, TableCRUD
    from zero_server.server.server_schema import ServerColumnSchema

    column = ServerColumnSchema(type="text", is_enum=False, is_array=False)
    mutate = Mutate(load_schema(SCHEMA), None, {"issues": {"id": column, "owner_id": column}})  # ty: ignore[invalid-argument-type]
    assert isinstance(mutate.issue, TableCRUD)
    with pytest.raises(AttributeError):
        _ = mutate.nope


async def test_sqlalchemy_refuses_an_unsupported_driver() -> None:
    class Dialect:
        driver = "pg8000"

    class Connection:
        dialect = Dialect()

    with pytest.raises(ValueError, match="'pg8000' is not supported"):
        await sqlalchemy_transaction(Connection())  # ty: ignore[invalid-argument-type]


# What a handler author imports. Removing one breaks consumers, so it fails here first.
PUBLIC = [
    "AST",
    "Builder",
    "CustomMutation",
    "ErrorReason",
    "ExpressionBuilder",
    "JSONValue",
    "MutateResponse",
    "MutateSuccess",
    "MutationAppError",
    "MutationOk",
    "MutationResponse",
    "MutationZeroError",
    "OneQuery",
    "OutOfOrderMutation",
    "PushBody",
    "QueryRequestHandler",
    "ServerPushFailed",
    "TransactFn",
    "server_ast",
]


@pytest.mark.parametrize("name", PUBLIC)
def test_public_name(name: str) -> None:
    import zero_server

    assert name in zero_server.__all__
    assert hasattr(zero_server, name)


def test_public_mutate_response_is_what_the_endpoint_returns() -> None:
    import zero_server
    from zero_server.server import process_mutations

    assert zero_server.MutateResponse is process_mutations.MutateResponse


def test_server_ast_maps_to_server_names() -> None:
    from zero_server import server_ast

    schema = load_schema(SCHEMA)
    ast: Any = server_ast(create_builder(schema).issue.where("ownerID", "u"), schema).to_json()
    assert ast["table"] == "issues"
    assert ast["where"]["left"]["name"] == "owner_id"


def test_zql_result_names_the_first_unsafe_integer() -> None:
    from zero_server.server.pg_query_executor import extract_zql_result

    text = '[{"n": [1], "m": [2, -9007199254740993]}, {"n": 9007199254740993}]'
    with pytest.raises(Exception, match=r"^Value exceeds safe Number range\. \[0\]\['m'\]\[1\] = -9007199254740993$"):
        extract_zql_result([{"zql_result": text}])


def test_zql_result_ignores_an_unsafe_integer_a_duplicate_key_replaced() -> None:
    from zero_server.server.pg_query_executor import extract_zql_result

    text = '{"n": 9007199254740993, "n": 9007199254740991}'
    assert extract_zql_result([{"zql_result": text}]) == {"n": 9007199254740991}
