"""The Python twin of `harness/query.ts`: the same queries, interpreting the same builder programs."""

from typing import Any

from hypothesis import strategies as st

from zero_python.protocol.ast import Parameter
from zero_python.query.builder import Query, create_builder
from zero_python.query.expression import ExpressionBuilder
from zero_python.query.registry import QueryParseError, define_queries, define_query, must_get_query
from zero_python.schema import Schema


def _sub(program: dict[str, Any] | None) -> Any:
    return (lambda q: apply(q, program["ops"])) if program else None


def _opts(opts: dict[str, Any] | None) -> dict[str, Any]:
    return opts or {}


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
        case _:
            return eb.exists(e[1], _sub(e[2]), **_opts(e[3]))


def apply(q: Query, ops: list[list[Any]]) -> Query:
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
            case _:
                q = q.one()
    return q


def is_program(value: Any) -> Any:
    if isinstance(value, dict) and isinstance(value.get("table"), str):
        return value
    raise ValueError("expected a program")


def make_handler(schema: Schema) -> Any:
    zql = create_builder(schema)

    def run(*, args: Any, ctx: Any) -> Query:
        return apply(zql.table(args["table"]), args["ops"])

    def parse_error(*, args: Any, ctx: Any) -> Query:
        raise QueryParseError(Exception("bad args"))

    def app_error(*, args: Any, ctx: Any) -> Query:
        raise Exception("nope")

    queries = define_queries(
        {
            "prog": define_query(run),
            "nested": {"prog": define_query(run)},
            "validated": define_query(is_program, run),
            "parseError": define_query(parse_error),
            "appError": define_query(app_error),
        }
    )
    return lambda name, args: must_get_query(queries, name).fn(args=args, ctx=None)


# ---------------------------------------------------------------------------------------------------
# Programs: what Hypothesis generates.

OPS = ["=", "!=", "IS", "IS NOT", "<", ">", "<=", ">=", "LIKE", "NOT LIKE", "ILIKE", "NOT ILIKE", "IN", "NOT IN"]
scalars = st.none() | st.text(max_size=3) | st.integers(-5, 5) | st.floats(-10, 10) | st.booleans()
params = st.builds(
    lambda anchor, field: {"$param": [anchor, field]},
    st.sampled_from(["authData", "preMutationRow"]),
    st.sampled_from(["sub", "role"]),
)
values = scalars | st.lists(st.text(max_size=2) | st.integers(-3, 3) | st.booleans(), max_size=3) | params


def programs(schema: Schema, depth: int = 2) -> st.SearchStrategy[dict[str, Any]]:
    # Mostly tables with relationships, which have the most to build.
    tables = sorted(schema.relationships) * 4 + sorted(schema.tables)
    return st.sampled_from(tables).flatmap(lambda t: _ops(schema, t, depth).map(lambda ops: {"table": t, "ops": ops}))


def _rarely[T](usual: st.SearchStrategy[T], rare: st.SearchStrategy[T], one_in: int) -> st.SearchStrategy[T]:
    return st.sampled_from([False] * (one_in - 1) + [True]).flatmap(lambda r: rare if r else usual)


def _columns(schema: Schema, table: str) -> st.SearchStrategy[str]:
    # The odd unknown column fails the whole batch when the AST is mapped.
    return _rarely(st.sampled_from(sorted(schema.tables[table].columns)), st.just("nope"), 60)


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
    return st.none() | st.fixed_dictionaries({}, optional={"flip": st.booleans(), "scalar": st.booleans()})


def _exprs(schema: Schema, table: str, depth: int) -> st.SearchStrategy[list[Any]]:
    col = _columns(schema, table)
    leaves = st.one_of(
        st.builds(lambda c, v: ["cmp", c, v], col, values),
        st.builds(lambda c, o, v: ["cmp", c, o, v], col, st.sampled_from(OPS), values),
        st.builds(lambda a, o, b: ["cmpLit", a, o, b], scalars | params, st.sampled_from(OPS), scalars | params),
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
        st.builds(lambda c, v: ["where", c, v], col, values),
        st.builds(lambda c, o, v: ["where", c, o, v], col, st.sampled_from(OPS), values),
        st.builds(lambda e: ["whereFn", e], _exprs(schema, table, depth)),
        rel.flatmap(
            lambda r: st.builds(
                lambda sub, opts: ["whereExists", r, sub, opts], _sub_program(schema, table, r, depth), _exists_opts()
            )
        ),
        rel.flatmap(lambda r: st.builds(lambda sub: ["related", r, sub], _sub_program(schema, table, r, depth))),
        st.builds(lambda c, d: ["orderBy", c, d], col, st.sampled_from(["asc", "desc"])),
        st.builds(lambda n: ["limit", n], _rarely(st.integers(0, 50), st.sampled_from([-1, 1.5, 2**31]), 20)),
        st.builds(
            lambda row, inc: ["start", row, inc], st.dictionaries(col, scalars, max_size=2), st.none() | st.booleans()
        ),
        st.just(["one"]),
    )
    return st.lists(op, max_size=4)
