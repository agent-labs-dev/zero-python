"""The Python twin of `harness/query.ts`: the same queries, interpreting the same builder programs."""

import asyncio
from typing import Any

from hypothesis import strategies as st

from tests.strategies import strings
from tests.wire import seed
from tests.wire.errors import SHAPES
from zero_server.protocol.application_error import ApplicationError
from zero_server.protocol.ast import AST, Parameter
from zero_server.query.builder import Query, create_builder
from zero_server.query.expression import ExpressionBuilder
from zero_server.query.registry import QueryFn, QueryParseError, define_queries, define_query, must_get_query
from zero_server.schema import Schema
from zero_server.shared.js import js_string


def _raw_condition(condition: Any) -> Any:
    """A condition parsed off the wire, as upstream's `astSchema` parses one."""
    return AST.from_json({"table": "_", "where": condition}).where


def _sub(program: dict[str, Any] | None) -> Any:
    return (lambda q: apply(q, program["ops"])) if program else None


def _opts(opts: dict[str, Any] | None) -> dict[str, Any]:
    # The TypeScript twin reads only these two, whatever else the options hold.
    return {k: v for k, v in (opts or {}).items() if k in ("flip", "scalar")}


def v(value: Any) -> Any:
    """`{"$param": [anchor, field]}` becomes a static parameter."""
    if isinstance(value, dict) and "$param" in value:
        anchor, field = value["$param"]
        return Parameter(anchor=anchor, field=field)
    return value


def ref(b: Any, value: Any) -> Any:
    """`{"json": [column, *path]}` becomes `b.json(column, *path)` of the builder `b`."""
    if isinstance(value, dict) and "json" in value:
        column, *path = value["json"]
        return b.json(column, *path)
    return value


def selector(value: Any) -> Any:
    """An aggregate's argument: `{"json": [...]}` becomes a selector callback, `{"returns": x}` one returning `x`."""
    if isinstance(value, dict) and "json" in value:
        return lambda b: ref(b, value)
    if isinstance(value, dict) and "returns" in value:
        return lambda _b: value["returns"]
    return value


def aggregate(target: Any, fn: str, args: list[Any]) -> Any:
    return getattr(target, fn)() if not args else getattr(target, fn)(selector(args[0]))


def expr(eb: ExpressionBuilder, e: list[Any]) -> Any:
    match e[0]:
        case "cmp":
            if len(e) == 3:
                return eb.cmp(ref(eb, e[1]), ref(eb, v(e[2])))
            return eb.cmp(ref(eb, e[1]), e[2], ref(eb, v(e[3])))
        case "cmpLit":
            return eb.cmp_lit(v(e[1]), e[2], v(e[3]))
        case "and":
            return eb.and_(*(expr(eb, x) for x in e[1]))
        case "or":
            return eb.or_(*(expr(eb, x) for x in e[1]))
        case "not":
            return eb.eb.not_(expr(eb, e[1]))
        case "exists":
            return eb.exists(e[1], _sub(e[2]), **_opts(e[3]))
    raise Exception(f"bad expression {js_string(e[0])}")


def apply(q: Query, ops: list[list[Any]]) -> Query:
    if not isinstance(ops, list):
        raise TypeError("ops is not iterable")
    for op in ops:
        match op[0]:
            case "where":
                q = q.where(op[1], v(op[2])) if len(op) == 3 else q.where(op[1], op[2], v(op[3]))
            case "whereFn":
                q = q.where(lambda eb, e=op[1]: expr(eb, e))
            case "whereRaw":
                q = q.where(lambda _eb, c=op[1]: _raw_condition(c))
            case "whereExists":
                q = q.where_exists(op[1], _sub(op[2]), **_opts(op[3]))
            case "related":
                q = q.related(op[1], _sub(op[2]))
            case "orderBy":
                q = q.order_by(op[1], op[2])
            case "limit":
                q = q.limit(op[1])
            case "start":
                q = q.start(op[1]) if op[2] is None else q.start(op[1], inclusive=op[2])
            case "one":
                q = q.one()
            case "count" | "sum" | "avg" | "min" | "max":
                q = aggregate(q, op[0], op[1:])
            case "distinct":
                q = q.distinct(*op[1])
            case "groupBy":
                q = aggregate(q.group_by(*op[1]), op[2], op[3:])
            case _:
                raise Exception(f"bad op {js_string(op[0])}")
    return q


def is_program(value: Any) -> Any:
    if isinstance(value, dict) and isinstance(value.get("table"), str):
        return value
    raise ValueError("expected a program")


def make_queries(schema: Schema) -> Any:
    zql = create_builder(schema)

    def run(*, args: Any, ctx: Any) -> Query:
        ops = args.get("ops")
        if args["table"] not in schema.tables and isinstance(ops, list):
            # `zql[table]` is undefined in the TypeScript twin, which fails on the first op read off it.
            for op in ops:
                method = {"whereFn": "where"}.get(op[0], op[0])
                if method in (
                    "where", "whereExists", "related", "orderBy", "limit", "start", "one", "count", "sum", "avg",
                    "min", "max", "distinct", "groupBy",
                ):  # fmt: skip
                    raise TypeError(f"Cannot read properties of undefined (reading '{method}')")
                raise Exception(f"bad op {js_string(op[0])}")
            return None  # ty: ignore[invalid-return-type]
        return apply(zql.table(args["table"]) if args["table"] in schema.tables else None, ops)  # ty: ignore[invalid-argument-type]

    def parse_error(*, args: Any, ctx: Any) -> Query:
        raise QueryParseError(Exception("bad args"))

    def parse_error_empty_cause(*, args: Any, ctx: Any) -> Query:
        raise QueryParseError(Exception(""))

    def parse_error_no_cause(*, args: Any, ctx: Any) -> Query:
        raise QueryParseError()

    def app_error(*, args: Any, ctx: Any) -> Query:
        raise Exception("nope")

    def app_details(*, args: Any, ctx: Any) -> Query:
        raise ApplicationError("app", details=args)

    def returning(value: Any) -> QueryFn:
        return lambda *, args, ctx: value

    def raising(make: Any) -> QueryFn:
        def fn(*, args: Any, ctx: Any) -> Any:
            raise make()

        return fn

    def wrapped(*, args: Any, ctx: Any) -> Any:
        return {"query": run(args=args, ctx=ctx)}

    return define_queries(
        {
            "prog": define_query(run),
            "nested": {"prog": define_query(run)},
            "validated": define_query(is_program, run),
            "parseError": define_query(parse_error),
            "parseErrorEmptyCause": define_query(parse_error_empty_cause),
            "parseErrorNoCause": define_query(parse_error_no_cause),
            "appError": define_query(app_error),
            "appDetails": define_query(app_details),
            "returns": define_query(lambda *, args, ctx: _returns(args)),
            # A Python function that returns nothing returns None, as a JavaScript one returns undefined.
            "constant": define_query(lambda *, args, ctx: zql.issue),
            "returnsNothing": define_query(returning(None)),
            "returnsString": define_query(returning("abc")),
            "returnsNumber": define_query(returning(5)),
            "returnsObject": define_query(returning({})),
            "wrapped": define_query(wrapped),
            "throw": {shape: define_query(raising(make)) for shape, make in SHAPES.items()},
        }
    )


RETURNS: dict[str, Any] = {"number": 5, "fraction": 1.5, "string": "x", "boolean": True, "object": {}, "array": []}
"""What a query function may return in place of a query, by name. Anything else is `None`, for `undefined`."""


def _returns(args: Any) -> Any:
    return RETURNS.get(args) if isinstance(args, str) else None


def make_handler(schema: Schema) -> Any:
    """Queries by name through the registry, except `raw`, which returns `RETURNS[args]` itself, and
    `gate.wait`, which finishes only once `gate.open` has run in the same request, and the coroutines
    `async.now`, which never suspends, and `async.throw`. One per request."""
    queries = make_queries(schema)
    zql = create_builder(schema)
    gate = asyncio.Event()

    async def wait() -> Any:
        await gate.wait()
        return zql.issue

    async def now() -> Any:
        return zql.issue

    async def throw() -> Any:
        raise Exception("nope")

    # Synchronous except for `gate.wait`, as a Python handler may be.
    def handler(name: str, args: Any) -> Any:
        if name == "raw":
            return _returns(args)
        if name == "gate.wait":
            return wait()
        if name == "gate.open":
            gate.set()
            return zql.user
        if name == "async.now":
            return now()
        if name == "async.throw":
            return throw()
        return must_get_query(queries, name).fn(args=args, ctx=None)

    return handler


# ---------------------------------------------------------------------------------------------------
# Programs: what Hypothesis generates.

OPS = ["=", "!=", "IS", "IS NOT", "<", ">", "<=", ">=", "LIKE", "NOT LIKE", "ILIKE", "NOT ILIKE", "IN", "NOT IN"]
scalars = st.none() | strings(max_size=3) | st.integers(-5, 5) | st.floats(-10, 10) | st.booleans()
params = st.builds(
    lambda anchor, field: {"$param": [anchor, field]},
    st.sampled_from(["authData", "preMutationRow"]),
    st.sampled_from(["sub", "role"]),
)
# Upstream's builder takes any JSON value, so arrays hold nulls and arrays, and objects reach json
# columns and everything else.
_json = st.recursive(
    st.none() | strings(max_size=2) | st.integers(-3, 3) | st.booleans(),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.sampled_from(["a", "b"]), inner, max_size=2),
    max_leaves=5,
)
containers = st.lists(_json, max_size=3) | st.dictionaries(st.sampled_from(["a", "b"]), _json, max_size=2)


values = scalars | containers | params


def _values(table: str, column: Any) -> st.SearchStrategy[Any]:
    """`values`, and for a column of its own Postgres type, values that column holds or could."""
    pool = seed.COLUMN_VALUES.get((table, column))
    if pool is None:
        return values
    return values | st.sampled_from(pool) | st.lists(st.sampled_from(pool), max_size=3)


def programs(schema: Schema, depth: int = 2) -> st.SearchStrategy[dict[str, Any]]:
    # Mostly tables with relationships, which have the most to build.
    tables = sorted(schema.relationships) * 4 + sorted(schema.tables)
    return st.sampled_from(tables).flatmap(lambda t: _ops(schema, t, depth).map(lambda ops: {"table": t, "ops": ops}))


def _rarely[T](usual: st.SearchStrategy[T], rare: st.SearchStrategy[T], one_in: int) -> st.SearchStrategy[T]:
    return st.sampled_from([False] * (one_in - 1) + [True]).flatmap(lambda r: rare if r else usual)


def _columns(schema: Schema, table: str) -> st.SearchStrategy[Any]:
    # The odd unknown column fails the whole batch when the AST is mapped. Upstream takes a number
    # for a column too, and fails on it the same way.
    return _rarely(st.sampled_from(sorted(schema.tables[table].columns)), st.sampled_from(["nope", 1]), 60)


def _relationships(schema: Schema, table: str) -> st.SearchStrategy[str]:
    names = sorted(schema.relationships.get(table, {}))
    bad = st.sampled_from(["nope", "zsubq_x"])
    return _rarely(st.sampled_from(names), bad, 30) if names else bad


def _dest(schema: Schema, table: str, relationship: str) -> str | None:
    hops = schema.relationships.get(table, {}).get(relationship)
    return hops[-1].dest_schema if hops else None


def _sub_program(
    schema: Schema, table: str, relationship: str, depth: int, *, exists: bool = False
) -> st.SearchStrategy[Any]:
    """The program a `related` or `exists` callback runs. An `exists` refuses an aggregate, so it seldom gets one."""
    dest = _dest(schema, table, relationship)
    if dest is None or depth <= 0:
        return st.none()
    ops = _ops(schema, dest, depth - 1)
    if exists:
        ops = _rarely(_plain_ops(schema, dest, depth - 1), ops, 10)
    return st.none() | ops.map(lambda ops: {"ops": ops})


def _exists_opts() -> st.SearchStrategy[Any]:
    # Upstream puts whatever `flip` and `scalar` it is given into the AST.
    flag = _rarely(st.booleans(), st.sampled_from(["yes", 0]), 20)
    return st.none() | st.fixed_dictionaries({}, optional={"flip": flag, "scalar": flag})


# Upstream refuses an unknown operator only once it formats the SQL, and sends it to zero-cache as is.
_ops_strategy = _rarely(st.sampled_from(OPS), st.sampled_from(["==", "like"]), 30)


def _comparison(schema: Schema, table: str, kind: str) -> st.SearchStrategy[list[Any]]:
    """`[kind, column, value]` or `[kind, column, op, value]`, the value drawn for the column."""
    return _columns(schema, table).flatmap(
        lambda c: (
            st.builds(lambda v: [kind, c, v], _values(table, c))
            | st.builds(lambda o, v: [kind, c, o, v], _ops_strategy, _values(table, c))
        )
    )


def _start_rows(schema: Schema, table: str) -> st.SearchStrategy[Any]:
    # A row's keys are strings once it is JSON, as every program is on its way to the query endpoint.
    entry = _columns(schema, table).flatmap(lambda c: st.tuples(st.just(str(c)), scalars | _values(table, c)))
    # Upstream takes an array for a row, which has no value for any column.
    return _rarely(st.lists(entry, max_size=2).map(dict), st.just(["a"]), 30)


_SEGMENTS = st.sampled_from(["a", "b", "k", "n", 0, 1])
# Paths `json()` refuses: none at all, and numbers that are no array index on every engine.
_BAD_PATHS = st.sampled_from([[], [-1], [1.5], [2**31], ["a", -2]])


def _json_ref(schema: Schema, table: str) -> st.SearchStrategy[dict[str, Any]]:
    """`{"json": [column, *path]}`: mostly a json column of `table` and a path into the seeded values, the odd
    other column, which the mapper refuses, or path `json()` refuses."""
    json_columns = sorted(c for c, column in schema.tables[table].columns.items() if column.type == "json")
    column = (
        _rarely(st.sampled_from(json_columns), _columns(schema, table), 15) if json_columns else _columns(schema, table)
    )
    path = _rarely(st.lists(_SEGMENTS, min_size=1, max_size=3), _BAD_PATHS, 25)
    return st.builds(lambda c, p: {"json": [c, *p]}, column, path)


def _json_comparison(schema: Schema, table: str) -> st.SearchStrategy[list[Any]]:
    """A JSON leaf compared with a scalar or list, a list of one type or not, or now and then another ref."""
    lists = st.lists(st.integers(-2, 3), max_size=3) | st.lists(st.sampled_from(["", "a", "x", "s"]), max_size=3)
    value = scalars | st.sampled_from(["s", "x", 1, 2.5, True]) | lists | st.lists(scalars, max_size=3) | params
    value = _rarely(value, _json_ref(schema, table), 30)
    return _json_ref(schema, table).flatmap(
        lambda r: (
            st.builds(lambda v: ["cmp", r, v], value) | st.builds(lambda o, v: ["cmp", r, o, v], _ops_strategy, value)
        )
    )


def _exprs(schema: Schema, table: str, depth: int) -> st.SearchStrategy[list[Any]]:
    literal = _rarely(scalars | params, containers, 10)
    leaves = st.one_of(
        _comparison(schema, table, "cmp"),
        _json_comparison(schema, table),
        st.builds(lambda a, o, b: ["cmpLit", a, o, b], literal, _ops_strategy, literal),
        _relationships(schema, table).flatmap(
            lambda r: st.builds(
                lambda sub, opts: ["exists", r, sub, opts],
                _sub_program(schema, table, r, depth, exists=True),
                _exists_opts(),
            )
        ),
    )
    return st.recursive(
        leaves,
        lambda inner: st.one_of(
            st.builds(lambda xs: ["and", xs], st.lists(inner, max_size=3)),
            st.builds(lambda xs: ["or", xs], st.lists(inner, max_size=3)),
            st.builds(lambda x: ["not", x], inner),
        ),
        max_leaves=5,
    )


_FNS = ["sum", "avg", "min", "max"]


def _selector(schema: Schema, table: str) -> st.SearchStrategy[list[Any]]:
    """An aggregate's arguments: a column, a JSON leaf, or now and then none or a callback returning no ref."""
    usual = _columns(schema, table).map(lambda c: [c]) | _json_ref(schema, table).map(lambda r: [r])
    return _rarely(usual, st.sampled_from([[], [{"returns": 5}]]), 10)


def _group_columns(schema: Schema, table: str) -> st.SearchStrategy[list[Any]]:
    # Now and then none, or one named twice.
    columns = st.lists(_columns(schema, table), min_size=1, max_size=2, unique_by=str)
    return _rarely(columns, st.sampled_from([[], ["id", "id"]]), 30)


def _reductions(schema: Schema, table: str) -> st.SearchStrategy[list[Any]]:
    """A query reduced to one value."""
    return st.just(["count"]) | st.builds(lambda f, a: [f, *a], st.sampled_from(_FNS), _selector(schema, table))


def _groupings(schema: Schema, table: str) -> st.SearchStrategy[list[Any]]:
    """A `distinct()`, or a `groupBy()` with its aggregate."""
    columns = _group_columns(schema, table)
    return st.one_of(
        st.builds(lambda cs: ["distinct", cs], columns),
        st.builds(lambda cs: ["groupBy", cs, "count"], columns),
        st.builds(lambda cs, f, a: ["groupBy", cs, f, *a], columns, st.sampled_from(_FNS), _selector(schema, table)),
    )


def _aggregate_ops(schema: Schema, table: str) -> st.SearchStrategy[list[Any]]:
    return _reductions(schema, table) | _groupings(schema, table)


def _grouped(schema: Schema, table: str, depth: int) -> st.SearchStrategy[list[list[Any]]]:
    """Filters, a grouping, then an order by its columns and the odd count of it, as a grouping takes them."""
    filters = st.lists(
        _comparison(schema, table, "where") | st.builds(lambda e: ["whereFn", e], _exprs(schema, table, depth)),
        max_size=2,
    )

    def after(grouping: list[Any]) -> st.SearchStrategy[list[list[Any]]]:
        orders = st.lists(
            st.builds(
                lambda c, d: ["orderBy", c, d], st.sampled_from(grouping[1] or ["id"]), st.sampled_from(["asc", "desc"])
            ),
            max_size=2,
        )
        tail = st.just([]) | st.just([["count"]]) if grouping[0] == "distinct" else st.just([])
        return st.builds(lambda o, t: [grouping, *o, *t], orders, tail)

    return st.builds(lambda f, rest: [*f, *rest], filters, _groupings(schema, table).flatmap(after))


def _ops(schema: Schema, table: str, depth: int) -> st.SearchStrategy[list[list[Any]]]:
    # A third plain, a third reduced to one value, a third grouped; now and then an aggregate anywhere.
    plain = _plain_ops(schema, table, depth)
    reduced = st.builds(lambda ops, agg: [*ops, agg], plain, _reductions(schema, table))
    return st.one_of(plain, reduced, _grouped(schema, table, depth))


def _plain_ops(schema: Schema, table: str, depth: int) -> st.SearchStrategy[list[list[Any]]]:
    col = _columns(schema, table)
    rel = _relationships(schema, table)
    op = st.one_of(
        _comparison(schema, table, "where"),
        st.builds(lambda e: ["whereFn", e], _exprs(schema, table, depth)),
        rel.flatmap(
            lambda r: st.builds(
                lambda sub, opts: ["whereExists", r, sub, opts],
                _sub_program(schema, table, r, depth, exists=True),
                _exists_opts(),
            )
        ),
        rel.flatmap(lambda r: st.builds(lambda sub: ["related", r, sub], _sub_program(schema, table, r, depth))),
        # Upstream's builder takes any direction, and z2s orders by anything but "asc" descending.
        st.builds(
            lambda c, d: ["orderBy", c, d],
            col,
            _rarely(st.sampled_from(["asc", "desc"]), st.sampled_from(["ASC", "\ufffdasc", None, "up"]), 30),
        ),
        st.builds(
            lambda n: ["limit", n],
            _rarely(st.integers(0, 50), st.sampled_from([-1, 1.5, 2**31, None, True, "3", "-1"]), 20),
        ),
        st.builds(lambda row, inc: ["start", row, inc], _start_rows(schema, table), st.none() | st.booleans()),
        st.just(["one"]),
    )
    return st.lists(_rarely(op, _aggregate_ops(schema, table), 15), max_size=4)
