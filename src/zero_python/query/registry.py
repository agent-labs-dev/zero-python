"""`define_query` and `define_queries`: named queries the query endpoint resolves by name.

Ported from rocicorp/mono `packages/zql/src/query/query-registry.ts`, `validate-input.ts` and
`error.ts` at zero/v1.10.0 (905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.

A validator is anything with Pydantic's `validate_python` (a `TypeAdapter`, or a model class), or a
plain callable that returns the parsed value and raises `ValueError` on bad input. Upstream takes a
Standard Schema validator; the error it raises has the same shape.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from zero_python.query.builder import Query
from zero_python.shared.json import JSONValue
from zero_python.shared.registry import Registry, build, merge, value_at_path

type QueryFn = Callable[..., Query]
"""Called as `fn(args=..., ctx=...)`."""

_SEPARATOR = re.compile(r"[.|]")


class QueryParseError(Exception):
    """The query endpoint reports this as a `parse` error rather than an `app` error."""

    def __init__(self, cause: BaseException | None = None) -> None:
        message = (
            f"Failed to parse arguments for query: {cause}"
            if isinstance(cause, Exception) and str(cause)
            else "Failed to parse arguments for query"
        )
        super().__init__(message)
        self.__cause__ = cause


class InputValidationError(Exception):
    def __init__(self, message: str, issues: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = {"type": "InputValidationError", "result": {"issues": issues}}


def _issues(error: Exception) -> list[dict[str, Any]]:
    if isinstance(error, ValidationError):
        return [{"message": e["msg"], "path": list(e["loc"])} for e in error.errors()]
    return [{"message": str(error)}]


def validate_input(name: str, value: Any, validator: Any, kind: str) -> Any:
    if validator is None:
        return value
    try:
        if hasattr(validator, "validate_python"):
            return validator.validate_python(value)
        if hasattr(validator, "model_validate"):
            return validator.model_validate(value)
        return validator(value)
    except (ValidationError, ValueError, TypeError) as error:
        issues = _issues(error)
        joined = ", ".join(issue["message"] for issue in issues)
        raise InputValidationError(f"Validation failed for {kind} {name}: {joined}", issues) from error


@dataclass(frozen=True)
class QueryDefinition:
    fn: QueryFn
    validator: Any = None


def define_query(fn_or_validator: Any, fn: QueryFn | None = None) -> QueryDefinition:
    """`define_query(fn)` or `define_query(validator, fn)`. `fn` is called as `fn(args=..., ctx=...)`."""
    if fn is None:
        return QueryDefinition(fn=fn_or_validator)
    return QueryDefinition(fn=fn, validator=fn_or_validator)


@dataclass(frozen=True)
class QueryRequest:
    query: "CustomQuery"
    args: JSONValue | None


class CustomQuery:
    def __init__(self, name: str, definition: QueryDefinition) -> None:
        self.query_name = name
        self._definition = definition

    def fn(self, *, args: JSONValue | None = None, ctx: Any = None) -> Query:
        validated = validate_input(self.query_name, args, self._definition.validator, "query")
        return self._definition.fn(args=validated, ctx=ctx)

    def __call__(self, args: JSONValue | None = None) -> QueryRequest:
        return QueryRequest(query=self, args=args)


type QueryRegistry = Registry
type QueryDefinitions = Mapping[str, "QueryDefinition | QueryDefinitions"]


def define_queries(definitions: "QueryDefinitions | Registry", overrides: "QueryDefinitions | None" = None) -> Registry:
    """Name each query by its path through `definitions`, dot-separated: `{"issue": {"byID": ...}}` is
    `issue.byID`. `overrides` replaces or adds entries in a base registry."""
    is_definition = lambda value: isinstance(value, QueryDefinition)  # noqa: E731
    base = definitions if isinstance(definitions, Registry) else build(definitions, is_definition, CustomQuery)
    if overrides is None:
        return base
    return merge(base, build(overrides, is_definition, CustomQuery))


def get_query(queries: Registry, name: str) -> CustomQuery | None:
    found = value_at_path(queries, name, _SEPARATOR)
    return found if isinstance(found, CustomQuery) else None


def must_get_query(queries: Registry, name: str) -> CustomQuery:
    query = get_query(queries, name)
    if query is None:
        raise Exception(f"Query not found: {name}")
    return query
