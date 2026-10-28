"""The wire tier for `/mutate`: the same pushes through upstream's handler and Python's, then a diff of
the responses and of every row either side wrote."""

import contextlib
from typing import Any
from uuid import uuid4

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import ValidationError

from tests.harness import Harness, HarnessError
from tests.strategies import as_js, cleanup_args, corrupted, json_values, mutation_ids, strings
from tests.wire.conftest import ADAPTERS, WireDB
from tests.wire.errors import SHAPES
from tests.wire.mutators import PREDICATES, REGISTRY, handler, provider, reset_runs, use_schema
from zero_server.protocol.push import PushBody
from zero_server.schema import load_schema
from zero_server.server.process_mutations import handle_mutate_request
from zero_server.server.push_processor import PushProcessor
from zero_server.server.zql_database import PostgresError
from zero_server.shared.asserts import AssertionFailedError
from zero_server.shared.js import js_stringify

NAMES = [
    "item.put", "item.put", "item.put", "item.count", "item.appError", "item.error", "item.typeError", "item.dbError",
    "item.detailsError",
    "pre.appError", "pre.error", "post.put", "nope",
    "item.return", "item.retry", "item.retry", "item.keep", "item.useKept",
]  # fmt: skip

texts = st.sampled_from(["a", "b", "c", "é", "\ud800"])


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
    # postgres.js types an array by its first leaf, so a boolean-led one is bound as the boolean `false`.
    "s": _rarely(strings(max_size=8), st.sampled_from(["too long for eight", [True, "y"], [[False]], ["y", True]])),
    "tags": _rarely(st.lists(strings(max_size=3), max_size=3), st.just([1, None])),
    "u": _rarely(st.uuids().map(str), st.just("not-a-uuid")),
    "mood": _rarely(st.sampled_from(["happy", "sad"]), st.just("meh")),
    "tm": st.integers(0, 86_399_999),
    "ttz": st.integers(0, 86_399_999),
    "amount": _rarely(st.integers(-(10**6), 10**6).map(lambda cents: cents / 100), st.just(10**9)),
    "ip": _rarely(st.sampled_from(["10.0.0.1", "::1", "192.168.0.0/24"]), st.just("nope")),
    # Arrays, whose elements Postgres converts one by one.
    "stamps": st.lists(st.integers(MS_2000, MS_2000 + 10**12), max_size=2),
    "clocks": st.lists(st.integers(0, 86_399_999), max_size=2),
}


MISSING = object()


def _key(value: st.SearchStrategy[Any]) -> st.SearchStrategy[Any]:
    """A primary-key value, or rarely one of the wrong type, null, or `MISSING` from the row."""
    return _rarely(value, st.sampled_from([MISSING, None, 1, 1.5, True, "1", [1]]), 8)


def _row(columns: dict[str, st.SearchStrategy[Any]], pk: dict[str, st.SearchStrategy[Any]]) -> st.SearchStrategy[Any]:
    optional = {name: st.none() | strategy for name, strategy in columns.items()}
    keys = {name: _key(strategy) for name, strategy in pk.items()}
    return st.fixed_dictionaries(keys, optional=optional).map(
        lambda row: {k: v for k, v in row.items() if v is not MISSING}
    )


crud_args = st.one_of(
    st.fixed_dictionaries({"table": st.just("thing"), "row": _row(THING_COLUMNS, {"id": texts})}),
    st.fixed_dictionaries(
        {
            "table": st.just("pair"),
            "row": _row({"note": strings(max_size=3)}, {"a": texts, "b": st.integers(0, 2)}),
        }
    ),
    # Columns the schema does not declare are refused before any SQL runs.
    st.just({"table": "thing", "row": {"id": "a", "nope": 1}}),
)
CRUD_NAMES = ["crud.insert", "crud.upsert", "crud.update", "crud.delete"]
mutator_args = st.fixed_dictionaries(
    {"id": texts, "value": texts},
    optional={
        "message": st.sampled_from(["no", "nope", "", "m\udc00"]),
        "details": json_values,
        # A mutator that returns `None` returns nothing, so JSON `null` is never result data.
        "ret": json_values.filter(lambda v: v is not None),
        "predicates": st.lists(st.sampled_from(sorted(PREDICATES)), max_size=3),
        "fail": st.integers(0, 6),
        "db": st.booleans(),
    },
)


VALID_QUERY = {"schema": "zero_0", "appID": "zero"}
BAD_QUERIES = [
    {"schema": "zero_0"},
    {"appID": "zero"},
    {"schema": "zero_0", "appID": "zero", "x": "y"},
    # Upstream schemas its escaper refuses: empty, past 60 characters, not ASCII, or not valid Unicode.
    {"schema": "", "appID": "zero"},
    {"schema": "s" * 61, "appID": "zero"},
    {"schema": "zero_é", "appID": "zero"},
    {"schema": "zero_\ud800", "appID": "zero"},
    {"schema": "\udc00zero", "appID": "zero"},
    {"schema": "ze\ud800ro", "appID": "zero"},
    {"schema": "zero_😀", "appID": "zero"},
    # One Postgres takes quoted, a quote doubled, and which does not exist.
    {"schema": 'ze"ro', "appID": "zero"},
]


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
            if isinstance(mutation_id, int) and mutation_id <= expected + 1:
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


def comparable(response: dict[str, Any]) -> str:
    """A response as JSON text, key order included."""
    return js_stringify(response)


def _m(mid: float, name: str = "item.put", /, *, client: str = "c1", **args: Any) -> dict[str, Any]:
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


# Upstream logs a failed mutation's error at warn level, and its logger recurses through a circular cause
# until the stack overflows, so at the default level that mutation fails differently and never commits. Below
# warn it is reported as Python reports it, which `test_circular_cause_matches_upstream` compares.
LOGGABLE_SHAPES = [shape for shape in SHAPES if shape != "circular"]

# One per branch of upstream's process-mutations.ts, so each runs whatever Hypothesis draws.
BRANCHES = {
    "each thrown shape": _scenario(_push(*(_m(n + 1, f"throw.{shape}") for n, shape in enumerate(LOGGABLE_SHAPES)))),
    "ok": _scenario(_push(_m(1, id="a", value="x"), _m(2, id="a", value="y"))),
    "tx.run": _scenario(_push(_m(1, id="a", value="x"), _m(2, id="b", value="y"), _m(3, "item.count", value="y"))),
    "app error": _scenario(_push(_m(1, "item.appError", id="a", value="x", details={"k": [1]}), _m(2))),
    "plain error, retried": _scenario(_push(_m(1, "item.error", message=""), _m(2, "item.typeError"))),
    "error details null or absent": _scenario(
        _push(
            _m(1, "item.detailsError", details=None), _m(2, "item.detailsError"), _m(3, "item.detailsError", details=0)
        )
    ),
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
    "nul byte": _scenario(_push(_m(1, id="n", value="a\x00b"))),
    "lone surrogates": _scenario(
        _push(_m(1, id="s", value="a\ud800b"), _m(2, "item.appError", message="m\udc00", details={"\udfff": "\ud83d"})),
        # A client ID is bound beside the integer first mutation ID.
        _push(_m(1, client="c\udc00", id="t", value="v")),
        # Bulk cleanup binds its client IDs as an array.
        _push(_cleanup({"type": "bulk", "clientGroupID": "cg1", "clientIDs": ["c\udc00"]})),
    ),
    "malformed body": [({**_push(_m(1)), "clientGroupID": 1}, VALID_QUERY, None)],
    "unescapable upstream schemas": [(_push(_m(1)), query, None) for query in BAD_QUERIES[3:]],
    "bad query params": [(_push(_m(1)), {"schema": "zero_0"}, None)],
    "crud rows that are not objects": _scenario(
        _push(
            *(
                {**_m(n + 1, "crud.insert"), "args": [{"table": "thing", "row": row}]}
                for n, row in enumerate([None, "ab", ["x"], 7, {"id": "ok"}])
            )
        )
    ),
    "crud with keys missing": _scenario(
        _push(
            *(
                {**_m(n + 1, f"crud.{kind}"), "args": [{"table": table, "row": row}]}
                for n, (kind, table, row) in enumerate(
                    [
                        ("insert", "thing", {"id": "a"}),
                        ("delete", "thing", {}),
                        ("update", "thing", {"n": 1}),
                        ("update", "thing", {}),
                        ("delete", "pair", {"a": "a"}),
                        ("update", "pair", {"note": "x", "b": 1}),
                        ("delete", "thing", {"id": None}),
                        ("upsert", "thing", {"n": 1}),
                        ("insert", "thing", {"id": "b", "s": [True, "y"], "u": None}),
                        ("update", "thing", {"id": "b", "s": [[False]]}),
                    ]
                )
            )
        )
    ),
    "odd mutation IDs": _scenario(
        _push(_m(1), _m(1.5), _m(2.0), _m(-1)),
        *(_push(_m(mid, client=client)) for mid, client in [(2**53 + 1, "c1"), (2**63, "c2"), (1e21, "c2")]),
    ),
    "cleanup odd IDs": _scenario(
        _push(_m(1, "item.appError"), _m(2, "item.appError"), _m(3, "item.appError")),
        *(
            _push(_cleanup({"clientGroupID": "cg1", "clientID": "c1", "upToMutationID": up_to}))
            for up_to in [1.5, 1e20, 2**63, 1e21, -1, 2**53 + 1]
        ),
    ),
    "return values": _scenario(
        _push(_m(1, "item.return", ret={"n": [1, None]}), _m(2, "item.return"), _m(3, "item.return", ret=0)),
    ),
    "retry until it succeeds": _scenario(
        _push(
            _m(1, "item.retry", fail=2, predicates=["yes"]),
            _m(2, "item.retry", fail=1, predicates=["no", "later"]),
            _m(3, "item.retry", fail=1, predicates=["throw", "reject", "truthy"]),
            _m(4, "item.retry", fail=2, predicates=["firstOnly"]),
            _m(5, "item.retry", fail=1, predicates=["matches"], db=True),
            _m(6, "item.retry", fail=1, predicates=["matches"]),
            _m(7, "item.retry", fail=6, predicates=["yes"]),
            _m(8, "item.retry", fail=6, predicates=["yes"], db=True),
            # One answers yes while the other is still deciding.
            _m(9, "item.retry", fail=1, predicates=["later", "yes"]),
        ),
    ),
    "transaction used after it ended": _scenario(
        _push(
            _m(1, "item.useKept"),
            _m(2, "item.keep"),
            _m(3, "item.useKept"),
            _m(4, "item.useKept", db=True),
            _m(5, "item.useKept", run=True),
        ),
    ),
    "cleanup invalid and failing": _scenario(
        _push(_cleanup({"type": "bulk", "clientGroupID": "cg1", "clientIDs": []}), _m(1)),
        _push(_cleanup({"clientGroupID": "cg1", "clientID": "cfail", "upToMutationID": 2}), _m(2)),
    ),
}


def _with_branches(test: Any) -> Any:
    for scenario in BRANCHES.values():
        test = example(scenario)(test)
    return test


@pytest.mark.parametrize("adapter", ADAPTERS)
@_with_branches
@given(scenarios())
def test_pushes_match_upstream(harness: Harness, wire_db: WireDB, adapter: str, pushes: list[Any]) -> None:
    wire_db.reset()
    harness.call("resetRuns")
    reset_runs()
    schema = load_schema(harness.call("wireSchema"))
    use_schema(schema)
    # One adapter runs without sessions, and one takes each body already parsed where it parses.
    py_db = provider(wire_db.py[adapter](schema), sessions=adapter != "psycopg")
    for body, query, user_id in pushes:
        expected = harness.call("mutate", url=wire_db.ts_url, query=query, body=body, userID=user_id)
        py_body = body
        if adapter == "asyncpg":
            with contextlib.suppress(ValidationError):
                py_body = PushBody.from_json(body)
        actual = wire_db.run(
            handle_mutate_request(db_provider=py_db, handler=handler, query=query, body=py_body, user_id=user_id)
        ).to_json()
        assert comparable(actual) == comparable(expected)
    assert as_js(wire_db.dump(wire_db.py_engine)) == as_js(wire_db.dump(wire_db.ts_engine))


@pytest.mark.parametrize(
    ("adapter", "wrapped"),
    [("sqlalchemy", "AsyncConnection"), ("asyncpg", "PoolConnectionProxy"), ("psycopg", "AsyncConnection")],
)
def test_transaction_exposes_the_driver_connection(wire_db: WireDB, adapter: str, wrapped: str) -> None:
    async def check() -> None:
        async def callback(tx: Any, _hooks: Any) -> Any:
            return tx.db_transaction.wrapped_transaction

        connection = await wire_db.py[adapter](None).transaction(callback)
        assert type(connection).__name__ == wrapped

    wire_db.run(check())


@pytest.mark.parametrize("first", [True, False])
def test_asyncpg_reports_a_refused_argument_as_sqlalchemy_does(wire_db: WireDB, first: bool) -> None:
    # asyncpg refuses an int for a text parameter before Postgres sees the statement.
    # SQLAlchemy runs a transaction's first statement itself and hands the rest to asyncpg.
    async def message(adapter: str) -> str:
        async def callback(tx: Any, _hooks: Any) -> Any:
            if not first:
                await tx.db_transaction.query("SELECT 1")
            await tx.db_transaction.query("SELECT $1::text", [5])

        with pytest.raises(PostgresError) as error:
            await wire_db.py[adapter](None).transaction(callback)
        return error.value.message

    assert wire_db.run(message("asyncpg")) == wire_db.run(message("sqlalchemy"))
    assert wire_db.run(message("asyncpg")).startswith("invalid input for query argument $1")


PROCESSOR_NAMES = [
    "item.put", "item|put", "item.appError", "item.ret", "legacy.put", "legacy|put", "legacy.ret", "item", "nope",
]  # fmt: skip


@pytest.mark.parametrize("adapter", ADAPTERS)
@example([(None, VALID_QUERY, None)])
@given(scenarios(PROCESSOR_NAMES))
def test_push_processor_matches_upstream(harness: Harness, wire_db: WireDB, adapter: str, pushes: list[Any]) -> None:
    wire_db.reset()
    processor = PushProcessor(provider(wire_db.py[adapter](None)), context={"tag": "ctx"})
    for body, query, _user_id in pushes:
        try:
            expected: Any = harness.call("pushProcessor", url=wire_db.ts_url, query=query, body=body)
        except HarnessError as error:
            # Upstream throws rather than answering, for a body of null.
            with pytest.raises(AssertionFailedError, match=str(error).split("\n")[0].removeprefix("Error: ")):
                wire_db.run(processor.process(REGISTRY, query, body))
            continue
        actual = wire_db.run(processor.process(REGISTRY, query, body)).to_json()
        assert comparable(actual) == comparable(expected)
    assert as_js(wire_db.dump(wire_db.py_engine)) == as_js(wire_db.dump(wire_db.ts_engine))


def test_circular_cause_matches_upstream(harness: Harness, wire_db: WireDB) -> None:
    wire_db.reset()
    wire_schema = harness.call("wireSchema")
    schema = load_schema(wire_schema)
    use_schema(schema)
    body = _push(_m(1, "throw.circular"), _m(2))
    # `mutateWithSchema` logs at error level only.
    expected = harness.call("mutateWithSchema", url=wire_db.ts_url, schema=wire_schema, query=VALID_QUERY, body=body)
    actual = wire_db.run(
        handle_mutate_request(
            db_provider=provider(wire_db.py["sqlalchemy"](schema)),
            handler=handler,
            query=VALID_QUERY,
            body=body,
            user_id=None,
        )
    )
    expected.pop("userID")
    assert as_js(actual.to_json()) == as_js({**expected, "userID": None})
    assert as_js(wire_db.dump(wire_db.py_engine)) == as_js(wire_db.dump(wire_db.ts_engine))


def test_retry_on_unsupported_matches_upstream(harness: Harness, wire_db: WireDB) -> None:
    """A transaction opened with no input has nothing to re-run its mutator, so `retry_on` refuses."""
    expected = harness.call("retryOnUnsupported", url=wire_db.ts_url)
    schema = load_schema(harness.call("wireSchema"))

    async def callback(tx: Any, _hooks: Any) -> Any:
        try:
            tx.retry_on(lambda _error, _attempt: True)
        except Exception as error:
            return str(error)
        return None

    assert wire_db.run(wire_db.py["asyncpg"](schema).transaction(callback)) == expected


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("level", [None, "read committed", "repeatable read", "serializable"])
def test_isolation_level_matches_upstream(harness: Harness, wire_db: WireDB, adapter: str, level: Any) -> None:
    from tests.wire.conftest import with_isolation

    expected = harness.call("isolation", url=wire_db.ts_url, level=level)
    db = with_isolation(wire_db, adapter, level)

    async def callback(tx: Any, _hooks: Any) -> str:
        rows = await tx.db_transaction.query("SHOW transaction_isolation")
        return rows[0]["transaction_isolation"]

    async def in_session() -> str:
        async with db.session() as session:
            return await session.transaction(callback)

    assert wire_db.run(db.transaction(callback)) == expected
    # A push runs its mutations in a session.
    assert wire_db.run(in_session()) == expected


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("bare", [True, False], ids=["bare", "in transaction"])
@pytest.mark.parametrize(
    ("fail_first", "concurrent", "runs"),
    # Not a failed fetch with concurrent runs: whether a run shares it or starts its own depends on timing.
    [(False, False, 2), (True, False, 3), (False, True, 3)],
    ids=["cached", "failed fetch not cached", "concurrent fetch shared"],
)
def test_run_matches_upstream(
    harness: Harness, wire_db: WireDB, adapter: str, bare: bool, fail_first: bool, concurrent: bool, runs: int
) -> None:
    """`ZQLDatabase.run` reads without a transaction when the connection can, and caches the server schema
    once fetched, sharing a fetch in flight and dropping one that failed. Compared by what each side asked
    its connection to do."""
    from tests.wire.conftest import recorded_run

    wire_db.reset()
    for engine in (wire_db.ts_engine, wire_db.py_engine):
        wire_db.run(_insert_items(engine))
    args = {"bare": bare, "failFirst": fail_first, "concurrent": concurrent, "runs": runs}
    expected = harness.call("recordedRun", url=wire_db.ts_url, **args)
    schema = load_schema(harness.call("wireSchema"))
    actual = wire_db.run(recorded_run(wire_db, adapter, schema, **args))
    assert as_js(actual) == as_js(expected)


async def _insert_items(engine: Any) -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO item (id, value) VALUES ('a', 'x'), ('b', 'y'), ('c', 'z')"))


# What a pool hook runs on each connection it hands out. `SELECT 1` takes a snapshot, after which a
# transaction's isolation level can no longer be set.
CHECKOUT_STATEMENTS = ["SELECT 1", "SET statement_timeout = '4321ms'"]


async def _sqlalchemy_checked_out_in_a_transaction(wire_db: WireDB, driver: str, level: Any) -> tuple[Any, Any]:
    """An engine whose `checkout` hook runs statements through the driver, which opens a transaction."""
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import create_async_engine

    from zero_server.server.adapters.sqlalchemy import SQLAlchemyConnection

    engine = create_async_engine(wire_db.py_engine.url.set(drivername=f"postgresql+{driver}"), pool_size=1)

    @event.listens_for(engine.sync_engine, "checkout")
    def _checkout(dbapi_connection: Any, _record: Any, _proxy: Any) -> None:
        cursor = dbapi_connection.cursor()
        for statement in CHECKOUT_STATEMENTS:
            cursor.execute(statement)
        cursor.close()

    return SQLAlchemyConnection(engine, level), engine.dispose


async def _psycopg_checked_out_in_a_transaction(wire_db: WireDB, _driver: str, level: Any) -> tuple[Any, Any]:
    """A pool whose `check` runs statements, which opens a transaction; the pool checks only that they ran."""
    from psycopg_pool import AsyncConnectionPool

    from zero_server.server.adapters.psycopg import PsycopgConnection

    async def check(connection: Any) -> None:
        for statement in CHECKOUT_STATEMENTS:
            await connection.execute(statement)

    url = wire_db.py_engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    pool = AsyncConnectionPool(url, min_size=1, max_size=1, check=check, open=False)
    await pool.open()
    return PsycopgConnection(pool, level), pool.close


CHECKED_OUT_IN_A_TRANSACTION = {
    "sqlalchemy": (_sqlalchemy_checked_out_in_a_transaction, "asyncpg"),
    "sqlalchemy-psycopg": (_sqlalchemy_checked_out_in_a_transaction, "psycopg"),
    "psycopg": (_psycopg_checked_out_in_a_transaction, "psycopg"),
}
"""The adapters whose pool can hand a connection out in a transaction. asyncpg opens none implicitly."""


@pytest.mark.parametrize("adapter", CHECKED_OUT_IN_A_TRANSACTION)
def test_run_matches_upstream_on_a_connection_checked_out_in_a_transaction(
    harness: Harness, wire_db: WireDB, adapter: str
) -> None:
    """A bare read on a connection a pool hook left in a transaction reads as upstream does, and keeps
    what the hook set."""
    from tests.wire.conftest import recorded_run

    wire_db.reset()
    for engine in (wire_db.ts_engine, wire_db.py_engine):
        wire_db.run(_insert_items(engine))
    args: dict[str, Any] = {"bare": True, "failFirst": False, "concurrent": False, "runs": 2}
    expected = harness.call("recordedRun", url=wire_db.ts_url, **args)
    schema = load_schema(harness.call("wireSchema"))
    open_connection, driver = CHECKED_OUT_IN_A_TRANSACTION[adapter]

    async def check() -> tuple[Any, Any]:
        connection, close = await open_connection(wire_db, driver, None)
        try:
            actual = await recorded_run(wire_db, adapter, schema, inner=connection, **args)
            return actual, await connection.query("SHOW statement_timeout")
        finally:
            await close()

    actual, timeout = wire_db.run(check())
    assert as_js(actual) == as_js(expected)
    assert timeout == [{"statement_timeout": "4321ms"}]


@pytest.mark.parametrize("adapter", CHECKED_OUT_IN_A_TRANSACTION)
@pytest.mark.parametrize("level", [None, "serializable"])
def test_transactions_match_upstream_on_a_connection_checked_out_in_a_transaction(
    harness: Harness, wire_db: WireDB, adapter: str, level: Any
) -> None:
    """A transaction on a connection a pool hook left in a transaction runs at upstream's isolation level,
    keeps what the hook set, and commits, alone and in a push's session."""
    from sqlalchemy import text

    from zero_server.server.zql_database import ZQLDatabase

    wire_db.reset()
    expected = harness.call("isolation", url=wire_db.ts_url, level=level)
    open_connection, driver = CHECKED_OUT_IN_A_TRANSACTION[adapter]

    async def callback(tx: Any, _hooks: Any) -> tuple[str, str]:
        await tx.db_transaction.query("INSERT INTO item (id, value) VALUES ($1, 'x')", [str(uuid4())])
        rows = await tx.db_transaction.query("SHOW transaction_isolation")
        timeout = await tx.db_transaction.query("SHOW statement_timeout")
        return rows[0]["transaction_isolation"], timeout[0]["statement_timeout"]

    async def check() -> tuple[Any, Any, int]:
        connection, close = await open_connection(wire_db, driver, level)
        try:
            db = ZQLDatabase(connection)
            alone = await db.transaction(callback)
            async with db.session() as session:
                in_session = await session.transaction(callback)
            async with wire_db.py_engine.connect() as conn:
                committed = (await conn.execute(text("SELECT count(*) FROM item"))).scalar_one()
            return alone, in_session, committed
        finally:
            await close()

    alone, in_session, committed = wire_db.run(check())
    assert alone == in_session == (expected, "4321ms")
    assert committed == 2
