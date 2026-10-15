"""Hypothesis strategies for wire values, valid and nearly valid."""

from typing import Any

from hypothesis import strategies as st

MAX_SAFE = 2**53 - 1

_common_names = st.sampled_from(["id", "a", "b", "title", "owner_id", "ownerID", "é", "x.y", ""])
names = st.one_of(_common_names, _common_names, _common_names, st.text(max_size=4))
numbers = st.integers(-MAX_SAFE, MAX_SAFE) | st.floats(allow_nan=False, allow_infinity=False)
scalars = st.text(max_size=5) | numbers | st.booleans()
json_values: st.SearchStrategy[Any] = st.recursive(
    st.none() | scalars,
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=4), inner, max_size=3),
    max_leaves=8,
)

SIMPLE_OPS = [
    "=", "!=", "IS", "IS NOT", "<", ">", "<=", ">=", "LIKE", "NOT LIKE", "ILIKE", "NOT ILIKE", "IN", "NOT IN",
]  # fmt: skip


def literals() -> st.SearchStrategy[dict[str, Any]]:
    value = st.none() | scalars | st.lists(scalars, max_size=3)
    return st.builds(lambda v: {"type": "literal", "value": v}, value)


def parameters() -> st.SearchStrategy[dict[str, Any]]:
    return st.builds(
        lambda anchor, field: {"type": "static", "anchor": anchor, "field": field},
        st.sampled_from(["authData", "preMutationRow"]),
        names | st.lists(names, max_size=2),
    )


def columns() -> st.SearchStrategy[dict[str, Any]]:
    return st.builds(lambda n: {"type": "column", "name": n}, names)


def simple_conditions(static: bool) -> st.SearchStrategy[dict[str, Any]]:
    right = (literals() | parameters()) if static else literals()
    left = (literals() | columns() | parameters()) if static else (literals() | columns())
    return st.builds(
        lambda op, left, right: {"type": "simple", "op": op, "left": left, "right": right},
        st.sampled_from(SIMPLE_OPS),
        left,
        right,
    )


def _optional(d: dict[str, Any], **maybe: Any) -> dict[str, Any]:
    return {**d, **{k: v for k, v in maybe.items() if v is not None}}


def keys() -> st.SearchStrategy[list[str]]:
    return st.lists(names, min_size=1, max_size=2)


def asts(static: bool = False, depth: int = 2) -> st.SearchStrategy[dict[str, Any]]:
    """Valid ASTs. Every subquery has an alias, as every builder-made one does."""

    def build(sub: st.SearchStrategy[dict[str, Any]] | None) -> st.SearchStrategy[dict[str, Any]]:
        def subquery() -> st.SearchStrategy[dict[str, Any]]:
            assert sub is not None
            return st.builds(
                lambda corr, sq, alias, system, hidden: _optional(
                    {"correlation": corr, "subquery": {**sq, "alias": alias}}, system=system, hidden=hidden
                ),
                st.builds(lambda p, c: {"parentField": p, "childField": c}, keys(), keys()),
                sub,
                names,
                st.none() | st.sampled_from(["permissions", "client", "test"]),
                st.none() | st.booleans(),
            )

        leaf = simple_conditions(static)
        if sub is not None:
            leaf = leaf | st.builds(
                lambda related, op, flip, scalar: _optional(
                    {"type": "correlatedSubquery", "related": related, "op": op}, flip=flip, scalar=scalar
                ),
                subquery(),
                st.sampled_from(["EXISTS", "NOT EXISTS"]),
                st.none() | st.booleans(),
                st.none() | st.booleans(),
            )
        conditions = st.recursive(
            leaf,
            lambda inner: st.builds(
                lambda t, cs: {"type": t, "conditions": cs}, st.sampled_from(["and", "or"]), st.lists(inner, max_size=3)
            ),
            max_leaves=5,
        )
        return st.builds(
            lambda table, schema, alias, where, related, limit, order_by, start: _optional(
                {"table": table},
                schema=schema,
                alias=alias,
                where=where,
                related=related,
                limit=limit,
                orderBy=order_by,
                start=start,
            ),
            names,
            st.none() | names,
            st.none() | names,
            st.none() | conditions,
            st.none() if sub is None else (st.none() | st.lists(subquery(), max_size=2)),
            st.none() | st.integers(0, 100),
            st.none() | st.lists(st.tuples(names, st.sampled_from(["asc", "desc"])).map(list), max_size=2),
            st.none()
            | st.builds(
                lambda row, exclusive: {"row": row, "exclusive": exclusive},
                st.dictionaries(names, json_values, max_size=2),
                st.booleans(),
            ),
        )

    strategy = build(None)
    for _ in range(depth):
        strategy = build(strategy)
    return strategy


def _paths(value: Any, path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    found = [path]
    if isinstance(value, dict):
        for k, v in value.items():
            found += _paths(v, (*path, k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            found += _paths(v, (*path, i))
    return found


def _replace(value: Any, path: tuple[Any, ...], fn: Any) -> Any:
    if not path:
        return fn(value)
    head, *rest = path
    if isinstance(value, dict):
        out = dict(value)
        out[head] = _replace(value[head], tuple(rest), fn)
        return out
    out = list(value)
    out[head] = _replace(value[head], tuple(rest), fn)
    return out


@st.composite
def corrupted(draw: st.DrawFn, valid: st.SearchStrategy[Any]) -> Any:
    """A valid value, or one with a single defect: a missing key, a null, an extra key or a wrong type."""
    value = draw(valid)
    if draw(st.booleans()):
        return value
    path = draw(st.sampled_from(_paths(value)))

    def damage(node: Any) -> Any:
        if isinstance(node, dict) and node and draw(st.booleans()):
            key = draw(st.sampled_from(sorted(node)))
            match draw(st.sampled_from(["drop", "null", "extra"])):
                case "drop":
                    return {k: v for k, v in node.items() if k != key}
                case "null":
                    return {**node, key: None}
                case _:
                    return {**node, draw(st.text(min_size=1, max_size=4)): draw(json_values)}
        return draw(json_values)

    return _replace(value, path, damage)


def as_js(value: Any) -> Any:
    """`value` as JavaScript holds it, every number a double, for comparing JSON across the two runtimes."""
    match value:
        case bool() | None | str():
            return value
        case int() | float():
            return float(value)
        case list() | tuple():
            return [as_js(v) for v in value]
        case dict():
            return {k: as_js(v) for k, v in value.items()}
        case _:
            raise TypeError(type(value))


client_ids = st.sampled_from(["c1", "c2", "c3"])
_small_ids = st.integers(0, 6)
# The odd one is no ID a client would send: past 2^53, where JavaScript rounds and Python does not, past
# Postgres's bigint, fractional, or negative.
_odd_ids = (
    st.sampled_from([-1, 1.5, 2.0, 2**53, 2**53 + 1, 2**63, 1e20, 1e21])
    | st.integers(-(2**64), 2**64)
    | st.floats(-1e22, 1e22)
)
mutation_ids = st.one_of(_small_ids, _small_ids, _small_ids, _odd_ids)


def custom_mutations(names: st.SearchStrategy[str] = names) -> st.SearchStrategy[dict[str, Any]]:
    return st.builds(
        lambda id, client_id, name, args, ts: {
            "type": "custom",
            "id": id,
            "clientID": client_id,
            "name": name,
            "args": args,
            "timestamp": ts,
        },
        mutation_ids,
        client_ids,
        names,
        st.lists(json_values, max_size=2),
        numbers,
    )


def crud_ops() -> st.SearchStrategy[dict[str, Any]]:
    def op(kind: str, table: str, pk: list[str], row: dict[str, Any], pk_row: dict[str, Any]) -> dict[str, Any]:
        value = pk_row if kind == "delete" else row
        return {"op": kind, "tableName": table, "primaryKey": pk, "value": value}

    return st.builds(
        op,
        st.sampled_from(["insert", "upsert", "update", "delete"]),
        names,
        keys(),
        st.dictionaries(names, json_values, max_size=2),
        st.dictionaries(names, scalars, max_size=2),
    )


def crud_mutations() -> st.SearchStrategy[dict[str, Any]]:
    return st.builds(
        lambda id, client_id, ops, ts: {
            "type": "crud",
            "id": id,
            "clientID": client_id,
            "name": "_zero_crud",
            "args": [{"ops": ops}],
            "timestamp": ts,
        },
        mutation_ids,
        client_ids,
        st.lists(crud_ops(), max_size=2),
        numbers,
    )


def push_bodies(mutations: st.SearchStrategy[dict[str, Any]] | None = None) -> st.SearchStrategy[dict[str, Any]]:
    mutations = mutations if mutations is not None else (custom_mutations() | crud_mutations())
    return st.builds(
        lambda group, ms, version, schema_version, ts, request_id, auth, traceparent: _optional(
            {
                "clientGroupID": group,
                "mutations": ms,
                "pushVersion": version,
                "timestamp": ts,
                "requestID": request_id,
            },
            schemaVersion=schema_version,
            auth=auth,
            traceparent=traceparent,
        ),
        st.just("cg1"),
        st.lists(mutations, max_size=4),
        st.sampled_from([1, 1, 1, 2, 0]),
        st.none() | st.integers(0, 3),
        numbers,
        st.text(max_size=3),
        st.none() | st.text(max_size=3),
        st.none() | st.text(max_size=3),
    )


def cleanup_args() -> st.SearchStrategy[dict[str, Any]]:
    single = st.builds(
        lambda type_, client_id, up_to: _optional(
            {"clientGroupID": "cg1", "clientID": client_id, "upToMutationID": up_to}, type=type_
        ),
        st.none() | st.just("single"),
        client_ids,
        mutation_ids,
    )
    bulk = st.builds(
        lambda ids: {"type": "bulk", "clientGroupID": "cg1", "clientIDs": ids},
        st.lists(client_ids, min_size=1, max_size=3, unique=True),
    )
    return single | bulk


def transform_requests() -> st.SearchStrategy[Any]:
    query = st.builds(
        lambda id, name, args: {"id": id, "name": name, "args": args},
        st.text(max_size=3),
        names,
        st.lists(json_values, max_size=2),
    )
    return st.builds(lambda qs: ["transform", qs], st.lists(query, max_size=3))


def mutate_params() -> st.SearchStrategy[dict[str, Any]]:
    return st.builds(
        lambda schema, app, extra: {"schema": schema, "appID": app, **extra},
        st.text(max_size=4),
        st.text(max_size=4),
        st.dictionaries(st.sampled_from(["x", "y"]), st.text(max_size=2), max_size=1),
    )
