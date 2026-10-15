"""Nested, dot-named namespaces of queries or mutators, as `defineQueries` and `defineMutators` build them.

Ported from rocicorp/mono `packages/shared/src/object-traversal.ts` and `deep-merge.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp.
"""

import re
from collections.abc import Callable, Iterator, Mapping
from typing import Any


class Registry(Mapping[str, Any]):
    """`registry.issue.create` and `registry["issue"]["create"]` both work."""

    def __init__(self, entries: dict[str, Any]) -> None:
        self._entries = entries

    def __getitem__(self, key: str) -> Any:
        return self._entries[key]

    def __getattr__(self, key: str) -> Any:
        try:
            return self._entries[key]
        except KeyError:
            raise AttributeError(key) from None

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


def build[D, L](
    definitions: Mapping[str, Any], is_definition: Callable[[Any], bool], make: Callable[[str, Any], L]
) -> Registry:
    def process(defs: Mapping[str, Any], path: list[str]) -> Registry:
        return Registry(
            {
                key: make(".".join([*path, key]), value) if is_definition(value) else process(value, [*path, key])
                for key, value in defs.items()
            }
        )

    return process(definitions, [])


def merge(base: Registry, overrides: Registry) -> Registry:
    merged = dict(base._entries)
    for key, value in overrides._entries.items():
        current = merged.get(key)
        merged[key] = merge(current, value) if isinstance(current, Registry) and isinstance(value, Registry) else value
    return Registry(merged)


def value_at_path(root: object, path: str, separator: str | re.Pattern[str]) -> Any:
    """The value at a separated `path` through nested registries or mappings, or `None`."""
    parts = separator.split(path) if isinstance(separator, re.Pattern) else path.split(separator)
    current: Any = root
    for part in parts:
        if type(current) is Registry:
            current = current._entries.get(part, _ABSENT)
            if current is _ABSENT:
                return None
        elif isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return None
    return current


_ABSENT = object()
