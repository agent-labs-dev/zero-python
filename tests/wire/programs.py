"""The Python twin of `harness/query.ts`: the same queries, interpreting the same builder programs."""

import asyncio
from typing import Any

from hypothesis import strategies as st

from tests.strategies import strings
from tests.wire import seed
from zero_server.protocol.application_error import ApplicationError
from zero_server.protocol.ast import Parameter
from zero_server.query.builder import Query, create_builder
from zero_server.query.expression import ExpressionBuilder
from zero_server.query.registry import QueryParseError, define_queries, define_query, must_get_query
from zero_server.schema import Schema
from zero_server.shared.js import js_string


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


def expr(eb: ExpressionBuilder, e: list[Any]) -> Any:
    match e[0]:
        case "cmp":
            return eb.cmp(e[1], v(e[2])) if len(e) == 3 else eb.cmp(e[1], e[2], v(e[3]))
        case "cmpLit":
            return eb.cmp_lit(v(e[1]), e[2], v(e[3]))
        case "and":
            return eb.and_(*(expr(eb, x) for x in e[1]))
        case "or":
            return eb.or_(*(expr(eb, x) for x in e[1]))
        case "not":
            return eb.not_(expr(eb, e[1]))
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
                if method in ("where", "whereExists", "related", "orderBy", "limit", "start", "one"):
                    raise TypeError(f"Cannot read properties of undefined (reading '{method}')")
                raise Exception(f"bad op {js_string(op[0])}")
            return None  # ty: ignore[invalid-return-type]
        return apply(zql.table(args["table"]) if args["table"] in schema.tables else None, ops)  # ty: ignore[invalid-argument-type]

    def parse_error(*, args: Any, ctx: Any) -> Query:
        raise QueryParseError(Exception("bad args"))

    def app_error(*, args: Any, ctx: Any) -> Query:
        raise Exception("nope")

    def app_details(*, args: Any, ctx: Any) -> Query:
        raise ApplicationError("app", details=args)

    return define_queries(
        {
            "prog": define_query(run),
            "nested": {"prog": define_query(run)},
            "validated": define_query(is_program, run),
            "parseError": define_query(parse_error),
            "appError": define_query(app_error),
            "appDetails": define_query(app_details),
            "returns": define_query(lambda *, args, ctx: _returns(args)),
        }
    )


RETURNS: dict[str, Any] = {"number": 5, "fraction": 1.5, "string": "x", "boolean": True, "object": {}, "array": []}
"""What a query function may return in place of a query, by name. Anything else is `None`, for `undefined`."""


def _returns(args: Any) -> Any:
    return RETURNS.get(args) if isinstance(args, str) else None


def make_handler(schema: Schema) -> Any:
    """Queries by name through the registry, except `raw`, which returns `RETURNS[args]` itself, and
    `gate.wait`, which finishes only once `gate.open` has run in the same request. One per request."""
    queries = make_queries(schema)
    zql = create_builder(schema)
    gate = asyncio.Event()

    async def handler(name: str, args: Any) -> Any:
        if name == "raw":
            return _returns(args)
        if name == "gate.wait":
            await gate.wait()
            return zql.issue
        if name == "gate.open":
            gate.set()
            return zql.user
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


def _sub_program(schema: Schema, table: str, relationship: str, depth: int) -> st.SearchStrategy[Any]:
    dest = _dest(schema, table, relationship)
    if dest is None or depth <= 0:
        return st.none()
    return st.none() | _ops(schema, dest, depth - 1).map(lambda ops: {"ops": ops})


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


def _exprs(schema: Schema, table: str, depth: int) -> st.SearchStrategy[list[Any]]:
    literal = _rarely(scalars | params, containers, 10)
    leaves = st.one_of(
        _comparison(schema, table, "cmp"),
        st.builds(lambda a, o, b: ["cmpLit", a, o, b], literal, _ops_strategy, literal),
        _relationships(schema, table).flatmap(
            lambda r: st.builds(
                lambda sub, opts: ["exists", r, sub, opts], _sub_program(schema, table, r, depth), _exists_opts()
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


def _ops(schema: Schema, table: str, depth: int) -> st.SearchStrategy[list[list[Any]]]:
    col = _columns(schema, table)
    rel = _relationships(schema, table)
    op = st.one_of(
        _comparison(schema, table, "where"),
        st.builds(lambda e: ["whereFn", e], _exprs(schema, table, depth)),
        rel.flatmap(
            lambda r: st.builds(
                lambda sub, opts: ["whereExists", r, sub, opts], _sub_program(schema, table, r, depth), _exists_opts()
            )
        ),
        rel.flatmap(lambda r: st.builds(lambda sub: ["related", r, sub], _sub_program(schema, table, r, depth))),
        st.builds(lambda c, d: ["orderBy", c, d], col, st.sampled_from(["asc", "desc"])),
        st.builds(
            lambda n: ["limit", n],
            _rarely(st.integers(0, 50), st.sampled_from([-1, 1.5, 2**31, None, True, "3", "-1"]), 20),
        ),
        st.builds(lambda row, inc: ["start", row, inc], _start_rows(schema, table), st.none() | st.booleans()),
        st.just(["one"]),
    )
    return st.lists(op, max_size=4)
