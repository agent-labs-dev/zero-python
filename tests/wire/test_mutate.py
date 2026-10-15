"""The wire tier for `/mutate`: the same pushes through upstream's handler and Python's, then a diff of
the responses and of every row either side wrote."""

from typing import Any

from hypothesis import example, given
from hypothesis import strategies as st

from tests.harness import Harness
from tests.strategies import as_js, cleanup_args, corrupted, json_values, mutation_ids
from tests.wire.conftest import WireDB
from tests.wire.mutators import REGISTRY, handler, provider, use_schema
from zero_python.schema import load_schema
from zero_python.server.process_mutations import handle_mutate_request
from zero_python.server.push_processor import PushProcessor
from zero_python.server.sqlalchemy import SQLAlchemyDatabase

NAMES = [
    "item.put", "item.put", "item.put", "item.count", "item.appError", "item.error", "item.typeError", "item.dbError",
    "pre.appError", "pre.error", "post.put", "nope",
]  # fmt: skip

texts = st.sampled_from(["a", "b", "c", "é"])


def _rarely(usual: st.SearchStrategy[Any], rare: st.SearchStrategy[Any], one_in: int = 15) -> st.SearchStrategy[Any]:
    return st.sampled_from([False] * (one_in - 1) + [True]).flatmap(lambda r: rare if r else usual)


MS_2000 = 946_684_800_000
THING_COLUMNS: dict[str, st.SearchStrategy[Any]] = {
    "n": _rarely(st.integers(-1000, 1000), st.sampled_from([1.5, "abc", 2**40])),
    "f": _rarely(st.floats(-1e6, 1e6), st.just("x")),
    "b": _rarely(st.booleans(), st.just("yes")),
    "j": json_values,
    "t": _rarely(st.integers(MS_2000, MS_2000 + 10**12), st.just(-1.5)),
    "d": st.integers(0, 3000).map(lambda days: MS_2000 + days * 86_400_000),
    "s": _rarely(st.text(max_size=8), st.just("too long for eight")),
    "tags": _rarely(st.lists(st.text(max_size=3), max_size=3), st.just([1, None])),
    "u": _rarely(st.uuids().map(str), st.just("not-a-uuid")),
    "mood": _rarely(st.sampled_from(["happy", "sad"]), st.just("meh")),
    "tm": st.integers(0, 86_399_999),
    "ttz": st.integers(0, 86_399_999),
    "amount": _rarely(st.integers(-(10**6), 10**6).map(lambda cents: cents / 100), st.just(10**9)),
    "ip": _rarely(st.sampled_from(["10.0.0.1", "::1", "192.168.0.0/24"]), st.just("nope")),
}


def _row(columns: dict[str, st.SearchStrategy[Any]], pk: dict[str, st.SearchStrategy[Any]]) -> st.SearchStrategy[Any]:
    optional = {name: st.none() | strategy for name, strategy in columns.items()}
    return st.fixed_dictionaries(pk, optional=optional)


crud_args = st.one_of(
    st.fixed_dictionaries({"table": st.just("thing"), "row": _row(THING_COLUMNS, {"id": texts})}),
    st.fixed_dictionaries(
        {
            "table": st.just("pair"),
            "row": _row({"note": st.text(max_size=3)}, {"a": texts, "b": st.integers(0, 2)}),
        }
    ),
    # Columns the schema does not declare are refused before any SQL runs.
    st.just({"table": "thing", "row": {"id": "a", "nope": 1}}),
)
CRUD_NAMES = ["crud.insert", "crud.upsert", "crud.update", "crud.delete"]
mutator_args = st.fixed_dictionaries(
    {"id": texts, "value": texts},
    optional={"message": st.sampled_from(["no", "nope", ""]), "details": json_values},
)


VALID_QUERY = {"schema": "zero_0", "appID": "zero"}
BAD_QUERIES = [{"schema": "zero_0"}, {"appID": "zero"}, {"schema": "zero_0", "appID": "zero", "x": "y"}]


def _weighted(draw: st.DrawFn, choices: list[tuple[int, Any]]) -> Any:
    # Indexes into an expanded list: an integer draw would favour its bounds, and so the rare choices.
    expanded = [i for i, (weight, _) in enumerate(choices) for _ in range(weight)]
    return choices[draw(st.sampled_from(expanded))][1]


def _one_in(draw: st.DrawFn, n: int) -> bool:
    return draw(st.sampled_from([False] * (n - 1) + [True]))


@st.composite
def scenarios(draw: st.DrawFn, names: list[str] | None = None) -> list[tuple[Any, dict[str, str], str | None]]:
    """Pushes as a client group sends them: per-client IDs in sequence, with the odd duplicate, gap or
    malformed request, so every branch of the state machine runs and most mutations get through."""
    next_id: dict[str, int] = {}
    pushes = []
    for _ in range(draw(st.integers(1, 4))):
        mutations = []
        for _ in range(draw(st.integers(0, 4))):
            kind = _weighted(draw, [(85, "custom"), (10, "cleanup"), (2, "crud")])
            client = _weighted(draw, [(49, "c1"), (49, "c2"), (1, "cfail")])
            if kind == "cleanup":
                # Cleanup args name their own clients; `cfail` makes the cleanup's transaction fail.
                args = draw(st.lists(cleanup_args() | json_values, max_size=1))
                if args and isinstance(args[0], dict) and "clientID" in args[0] and _one_in(draw, 10):
                    args = [{**args[0], "clientID": "cfail"}]
                mutations.append(
                    {
                        "type": "custom",
                        "id": 0,
                        "clientID": client,
                        "name": "_zero_cleanupResults",
                        "args": args,
                        "timestamp": 1,
                    }
                )
                continue
            expected = next_id.get(client, 1)
            mutation_id = _weighted(
                draw, [(93, expected), (4, max(expected - 1, 0)), (2, expected + 1), (1, draw(mutation_ids))]
            )
            next_id[client] = max(expected, mutation_id + 1)
            if kind == "crud":
                mutations.append(
                    {
                        "type": "crud",
                        "id": mutation_id,
                        "clientID": client,
                        "name": "_zero_crud",
                        "args": [{"ops": []}],
                        "timestamp": 1,
                    }
                )
                continue
            if names is None and draw(st.booleans()):
                mutations.append(
                    {
                        "type": "custom",
                        "id": mutation_id,
                        "clientID": client,
                        "name": draw(st.sampled_from(CRUD_NAMES)),
                        "args": [draw(crud_args)],
                        "timestamp": 1,
                    }
                )
                continue
            mutations.append(
                {
                    "type": "custom",
                    "id": mutation_id,
                    "clientID": client,
                    "name": draw(st.sampled_from(names or NAMES)),
                    "args": draw(st.lists(mutator_args, max_size=1)) if _one_in(draw, 10) else [draw(mutator_args)],
                    "timestamp": 1,
                }
            )
        body = {
            "clientGroupID": "cg1",
            "mutations": mutations,
            "pushVersion": _weighted(draw, [(49, 1), (1, 2)]),
            "timestamp": 1,
            "requestID": "r",
        }
        if _one_in(draw, 40):
            body = draw(corrupted(st.just(body)))
        query = draw(st.sampled_from(BAD_QUERIES)) if _one_in(draw, 40) else VALID_QUERY
        pushes.append((body, query, draw(st.sampled_from([None, "u1"]))))
    return pushes


def comparable(response: dict[str, Any]) -> dict[str, Any]:
    """A response with parse-failure messages cut to their prefix.

    Upstream's message embeds valita's description of the first bad field; Python's embeds Pydantic's.
    zero-cache only logs the message, so the prefix and the rest of the body are what must agree.
    """
    if response.get("reason") == "parse":
        response = {**response, "message": response["message"].split(":", 1)[0]}
    return as_js(response)


def _m(mid: int, name: str = "item.put", /, *, client: str = "c1", **args: Any) -> dict[str, Any]:
    return {"type": "custom", "id": mid, "clientID": client, "name": name, "args": [args], "timestamp": 1}


def _push(*mutations: dict[str, Any], version: int = 1) -> dict[str, Any]:
    return {
        "clientGroupID": "cg1",
        "mutations": list(mutations),
        "pushVersion": version,
        "timestamp": 1,
        "requestID": "r",
    }


def _cleanup(arg: Any) -> dict[str, Any]:
    return {"type": "custom", "id": 0, "clientID": "c1", "name": "_zero_cleanupResults", "args": [arg], "timestamp": 1}


def _scenario(*bodies: dict[str, Any]) -> list[tuple[Any, dict[str, str], str | None]]:
    return [(body, VALID_QUERY, "u1") for body in bodies]


# One per branch of upstream's process-mutations.ts, so each runs whatever Hypothesis draws.
BRANCHES = {
    "ok": _scenario(_push(_m(1, id="a", value="x"), _m(2, id="a", value="y"))),
    "tx.run": _scenario(_push(_m(1, id="a", value="x"), _m(2, id="b", value="y"), _m(3, "item.count", value="y"))),
    "app error": _scenario(_push(_m(1, "item.appError", id="a", value="x", details={"k": [1]}), _m(2))),
    "plain error, retried": _scenario(_push(_m(1, "item.error", message=""), _m(2, "item.typeError"))),
    "database error": _scenario(_push(_m(1, "item.dbError"))),
    "unknown mutator": _scenario(_push(_m(1, "nope"))),
    "pre-transaction error": _scenario(_push(_m(1, "pre.appError", details=0), _m(2, "pre.error"))),
    "post-commit error": _scenario(_push(_m(1, "post.put", id="b", value="z"))),
    "already processed": _scenario(_push(_m(1)), _push(_m(1), _m(2))),
    "out of order": _scenario(_push(_m(1), _m(3), _m(4))),
    "unsupported push version": _scenario(_push(_m(1), version=2)),
    "crud mutation": _scenario(_push(_m(1), {**_m(2), "type": "crud", "name": "_zero_crud", "args": [{"ops": []}]})),
    "open failure": _scenario(_push(_m(1, client="cfail"), _m(1, client="c2"))),
    "cleanup single and legacy": _scenario(
        _push(_m(1, "item.appError"), _m(2, "item.appError"), _m(3, "item.appError")),
        _push(_cleanup({"type": "single", "clientGroupID": "cg1", "clientID": "c1", "upToMutationID": 1})),
        _push(_cleanup({"clientGroupID": "cg1", "clientID": "c1", "upToMutationID": 2})),
    ),
    "cleanup bulk": _scenario(
        _push(_m(1, "item.appError"), _m(1, "item.appError", client="c2")),
        _push(_cleanup({"type": "bulk", "clientGroupID": "cg1", "clientIDs": ["c2", "c1"]})),
    ),
    "bad query params": [(_push(_m(1)), {"schema": "zero_0"}, None)],
    "cleanup invalid and failing": _scenario(
        _push(_cleanup({"type": "bulk", "clientGroupID": "cg1", "clientIDs": []}), _m(1)),
        _push(_cleanup({"clientGroupID": "cg1", "clientID": "cfail", "upToMutationID": 2}), _m(2)),
    ),
}


def _with_branches(test: Any) -> Any:
    for scenario in BRANCHES.values():
        test = example(scenario)(test)
    return test


@_with_branches
@given(scenarios())
def test_pushes_match_upstream(harness: Harness, wire_db: WireDB, pushes: list[Any]) -> None:
    wire_db.reset()
    schema = load_schema(harness.call("wireSchema"))
    use_schema(schema)
    py_db = provider(SQLAlchemyDatabase(wire_db.py_engine, schema))
    for body, query, user_id in pushes:
        expected = harness.call("mutate", url=wire_db.ts_url, query=query, body=body, userID=user_id)
        actual = wire_db.run(
            handle_mutate_request(db_provider=py_db, handler=handler, query=query, body=body, user_id=user_id)
        ).to_json()
        assert comparable(actual) == comparable(expected)
    assert as_js(wire_db.dump(wire_db.py_engine)) == as_js(wire_db.dump(wire_db.ts_engine))


def test_transaction_exposes_the_connection(wire_db: WireDB) -> None:
    async def check() -> None:
        async def callback(tx: Any, _hooks: Any) -> Any:
            return tx.db_transaction.wrapped_transaction

        connection = await SQLAlchemyDatabase(wire_db.py_engine).transaction(callback)
        assert connection.__class__.__name__ == "AsyncConnection"

    wire_db.run(check())


PROCESSOR_NAMES = ["item.put", "item|put", "item.appError", "legacy.put", "legacy|put", "item", "nope"]


@given(scenarios(PROCESSOR_NAMES))
def test_push_processor_matches_upstream(harness: Harness, wire_db: WireDB, pushes: list[Any]) -> None:
    wire_db.reset()
    processor = PushProcessor(provider(SQLAlchemyDatabase(wire_db.py_engine)), context={"tag": "ctx"})
    for body, query, _user_id in pushes:
        expected = harness.call("pushProcessor", url=wire_db.ts_url, query=query, body=body)
        actual = wire_db.run(processor.process(REGISTRY, query, body)).to_json()
        assert comparable(actual) == comparable(expected)
    assert as_js(wire_db.dump(wire_db.py_engine)) == as_js(wire_db.dump(wire_db.ts_engine))
