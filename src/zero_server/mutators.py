"""`define_mutator` and `define_mutators`: named server mutators the mutate endpoint dispatches to.

Ported from rocicorp/mono `packages/zql/src/mutate/mutator.ts` and `mutator-registry.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.

A mutator here is the server half of the TypeScript mutator of the same name: the client runs its own
optimistically, and this one runs authoritatively inside the mutation's transaction.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, overload

from zero_server.query.registry import is_schema_validator, validate_input
from zero_server.shared.json import JSONValue
from zero_server.shared.registry import Registry, build, merge, value_at_path

type MutatorFn = Callable[..., Awaitable[None]]
"""Called as `await fn(args=..., ctx=..., tx=...)`."""


@dataclass(frozen=True)
class MutatorDefinition:
    fn: MutatorFn
    validator: Any = None


@overload
def define_mutator(fn_or_validator: MutatorFn, fn: None = None) -> MutatorDefinition: ...
@overload
def define_mutator(fn_or_validator: Any, fn: MutatorFn) -> MutatorDefinition: ...
@overload
def define_mutator(fn_or_validator: Any, fn: None = None) -> Callable[[MutatorFn], MutatorDefinition]: ...
def define_mutator(fn_or_validator: Any, fn: MutatorFn | None = None) -> Any:
    """Define a mutator, called as `await fn(args=..., ctx=..., tx=...)`, in the three forms of
    `define_query`: `define_mutator(fn)` or `@define_mutator`, `define_mutator(validator, fn)`, and
    `@define_mutator(ArgsModel)`."""
    if fn is not None:
        return MutatorDefinition(fn=fn, validator=fn_or_validator)
    if is_schema_validator(fn_or_validator):
        return lambda decorated: MutatorDefinition(fn=decorated, validator=fn_or_validator)
    return MutatorDefinition(fn=fn_or_validator)


@dataclass(frozen=True)
class MutateRequest:
    mutator: "Mutator"
    args: JSONValue | None


class Mutator:
    def __init__(self, name: str, definition: MutatorDefinition) -> None:
        self.mutator_name = name
        self._definition = definition

    async def fn(self, *, args: JSONValue | None = None, ctx: Any = None, tx: Any) -> None:
        validated = validate_input(self.mutator_name, args, self._definition.validator, "mutator")
        await self._definition.fn(args=validated, ctx=ctx, tx=tx)

    def __call__(self, args: JSONValue | None = None) -> MutateRequest:
        return MutateRequest(mutator=self, args=args)


type MutatorDefinitions = Mapping[str, "MutatorDefinition | MutatorDefinitions"]


def define_mutators(
    definitions: "MutatorDefinitions | Registry", overrides: "MutatorDefinitions | None" = None
) -> Registry:
    """Name each mutator by its dot-separated path through `definitions`, as `define_queries` does."""
    is_definition = lambda value: isinstance(value, MutatorDefinition)  # noqa: E731
    base = definitions if isinstance(definitions, Registry) else build(definitions, is_definition, Mutator)
    if overrides is None:
        return base
    return merge(base, build(overrides, is_definition, Mutator))


def get_mutator(mutators: Registry, name: str) -> Mutator | None:
    found = value_at_path(mutators, name, ".")
    return found if isinstance(found, Mutator) else None


def must_get_mutator(mutators: Registry, name: str) -> Mutator:
    mutator = get_mutator(mutators, name)
    if mutator is None:
        raise Exception(f"Mutator not found: {name}")
    return mutator
