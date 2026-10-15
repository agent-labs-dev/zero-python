"""Query and mutator registries: the same definitions, merged the same way, looked up by the same names
as `harness/registry.ts` looks them up upstream."""

import asyncio
from typing import Any

import pytest

from tests.harness import Harness
from tests.strategies import as_js
from zero_server.mutators import Mutator, define_mutator, define_mutators, get_mutator, must_get_mutator
from zero_server.query.builder import create_builder
from zero_server.query.registry import CustomQuery, define_queries, define_query, get_query, must_get_query
from zero_server.schema import Schema, load_schema
from zero_server.shared.registry import Registry


def upper(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("expected a string")
    return value.upper()


async def later(value: Any) -> Any:
    return value


def _queries(schema: Schema) -> dict[str, Registry]:
    zql = create_builder(schema)
    base = define_queries(
        {
            "issue": {
                "all": define_query(lambda *, args, ctx: zql.issue),
                "byTitle": define_query(lambda *, args, ctx: zql.issue.where("title", args)),
                "checked": define_query(upper, lambda *, args, ctx: zql.issue.where("title", args)),
                "later": define_query(later, lambda *, args, ctx: zql.issue),
            },
            "plain": define_query(lambda *, args, ctx: zql.user),
        }
    )
    return {
        "base": base,
        "merged": define_queries(
            base,
            {
                "issue": {"open": define_query(lambda *, args, ctx: zql.issue.where("open", True))},
                "plain": define_query(lambda *, args, ctx: zql.comment),
            },
        ),
        "fromDefinitions": define_queries(
            {"a": define_query(lambda *, args, ctx: zql.issue)},
            {"a": {"b": define_query(lambda *, args, ctx: zql.user)}},
        ),
    }


async def _noop(*, tx: Any, args: Any, ctx: Any) -> None:
    return None


_BASE_MUTATORS = define_mutators(
    {
        "item": {
            "put": define_mutator(_noop),
            "checked": define_mutator(upper, _noop),
            "later": define_mutator(later, _noop),
        },
        "top": define_mutator(_noop),
    }
)
MUTATORS = {
    "base": _BASE_MUTATORS,
    "merged": define_mutators(_BASE_MUTATORS, {"item": {"extra": define_mutator(_noop)}, "top": define_mutator(_noop)}),
    "fromDefinitions": define_mutators({"a": define_mutator(_noop)}, {"b": define_mutator(_noop)}),
}


def _attempt(fn: Any) -> Any:
    try:
        return fn()
    except Exception as error:
        return {"error": str(error)}


def _describe_query(value: Any, args: Any) -> Any:
    if value is None:
        return {"kind": "undefined"}
    if isinstance(value, CustomQuery):
        request = value(args)
        return {
            "kind": "query",
            "name": value.query_name,
            "ast": _attempt(lambda: value.fn(args=args, ctx={}).ast.to_json()),
            "request": {"name": request.query.query_name, "args": request.args},
        }
    assert isinstance(value, Registry)
    return {"kind": "namespace", "keys": list(value)}


async def _describe_mutator(value: Any, args: Any) -> Any:
    if value is None:
        return {"kind": "undefined"}
    if isinstance(value, Mutator):
        run: Any = "ok"
        try:
            await value.fn(args=args, ctx={}, tx=None)
        except Exception as error:
            run = {"error": str(error), "details": getattr(error, "details", None)}
        request = value(args)
        return {
            "kind": "mutator",
            "name": value.mutator_name,
            "run": run,
            "request": {"name": request.mutator.mutator_name, "args": request.args},
        }
    assert isinstance(value, Registry)
    return {"kind": "namespace", "keys": list(value)}


# Leaves, namespaces, overrides, separators, and names that resolve to nothing. Upstream also resolves
# `~`, its registries' marker, and the names objects inherit, such as `toString`: JavaScript's object
# model, which a Python registry does not have.
QUERY_NAMES = [
    "issue.all", "issue.byTitle", "issue.checked", "issue.later", "issue.open", "plain", "issue", "issue|all",
    "a", "a.b", "nope", "issue.nope", "issue.all.x", "", ".", "issue.",
]  # fmt: skip
MUTATOR_NAMES = [
    "item.put",
    "item.checked",
    "item.later",
    "item.extra",
    "top",
    "item",
    "a",
    "b",
    "nope",
    "item|put",
    "",
]
ARGS = ["x", 5, None]


@pytest.fixture(scope="session")
def schema(harness: Harness) -> Schema:
    return load_schema(harness.call("schema"))


@pytest.mark.parametrize("registry", ["base", "merged", "fromDefinitions"])
@pytest.mark.parametrize("name", QUERY_NAMES)
def test_query_lookups_match_upstream(harness: Harness, schema: Schema, registry: str, name: str) -> None:
    queries = _queries(schema)[registry]
    for args in ARGS:
        expected = harness.call("registryLookup", kind="query", registry=registry, name=name, args=args)
        actual = {
            "get": _describe_query(get_query(queries, name), args),
            "mustGet": _attempt(lambda args=args: _describe_query(must_get_query(queries, name), args)),
        }
        assert as_js(actual) == as_js(expected), args


@pytest.mark.parametrize("registry", ["base", "merged", "fromDefinitions"])
@pytest.mark.parametrize("name", MUTATOR_NAMES)
def test_mutator_lookups_match_upstream(harness: Harness, registry: str, name: str) -> None:
    mutators = MUTATORS[registry]

    async def describe(args: Any) -> Any:
        try:
            must_get = await _describe_mutator(must_get_mutator(mutators, name), args)
        except Exception as error:
            must_get = {"error": str(error)}
        return {"get": await _describe_mutator(get_mutator(mutators, name), args), "mustGet": must_get}

    for args in ARGS:
        expected = harness.call("registryLookup", kind="mutator", registry=registry, name=name, args=args)
        assert as_js(asyncio.run(describe(args))) == as_js(expected), args
