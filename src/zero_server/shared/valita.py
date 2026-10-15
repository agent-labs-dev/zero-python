"""The message upstream's `v.parse` throws when a request does not match its wire schema.

Ported from rocicorp/mono `packages/shared/src/valita.ts` at zero/v1.10.0
(905d08516ba0caaf314cd28e93a5a47e8acdd482). Apache-2.0, Copyright Rocicorp. The validators underneath are
ported from `@badrap/valita` 0.3.11 (`src/index.ts`), the version `@rocicorp/zero` 1.10.0 installs. MIT,
Copyright (c) 2021 Badrap Oy.

Pydantic decides whether a request parses, and is fast. When it refuses one, the same value runs through
these validators, which reproduce valita's issue tree: which issue comes first, and how a union reports
its failure. The message is built from that first issue, as upstream builds it.

Only what `zero_server.protocol.schemas` uses is here, for values decoded from JSON: objects, arrays and
tuples, unions of objects told apart by a literal key or tried in turn, unions of primitives, literals,
and optional properties. An assertion marks each shape valita handles that those schemas never build.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import cmp_to_key
from typing import Any, Literal

from zero_server.shared.js import js_keys, js_number, js_stringify

type Key = str | int
type Mode = Literal["passthrough", "strict"]

FORBID_EXTRA_KEYS = 0x1
MISSING_VALUE = 0x4


@dataclass(frozen=True, slots=True)
class Leaf:
    code: str
    expected: tuple[Any, ...] = ()
    keys: tuple[Key, ...] = ()
    min_length: int = 0
    max_length: int | None = None


@dataclass(frozen=True, slots=True)
class Prepend:
    key: Key
    tree: "Tree"


@dataclass(frozen=True, slots=True)
class Join:
    left: "Tree"
    right: "Tree"


type Tree = Leaf | Prepend | Join
type Result = Tree | None
"""`None` for a value that parses; upstream's `Ok` carries a transformed value, which no message needs."""


def _join(left: Tree | None, right: Tree) -> Tree:
    return right if left is None else Join(left, right)


def _first_issue(tree: Tree) -> tuple[Leaf, tuple[Key, ...]]:
    """`err.issues[0]`: the leftmost leaf, with its path."""
    path: list[Key] = []
    while not isinstance(tree, Leaf):
        if isinstance(tree, Join):
            tree = tree.left
        else:
            path.append(tree.key)
            tree = tree.tree
    return tree, tuple(path)


def input_type(value: object) -> str:
    """valita's `toInputType`, for a JSON value."""
    match value:
        case None:
            return "null"
        case bool():
            return "boolean"
        case int() | float():
            return "number"
        case str():
            return "string"
        case list() | tuple():
            return "array"
        case _:
            return "object"


def _same(a: object, b: object) -> bool:
    """`===` between a literal and a JSON value."""
    return input_type(a) == input_type(b) and a == b


class Type:
    name: str = ""

    def func(self, value: Any, flags: int) -> Result:
        raise NotImplementedError

    def optional(self) -> "Type":
        return Optional(self)

    def try_(self, value: object, mode: Mode | None) -> Result:
        return self.func(value, 0 if mode == "passthrough" else FORBID_EXTRA_KEYS)


class _Primitive(Type):
    def __init__(self, name: str) -> None:
        self.name = name
        self._issue = Leaf("invalid_type", expected=(name,))

    def func(self, value: Any, flags: int) -> Result:
        return None if input_type(value) == self.name else self._issue


class _Unknown(Type):
    name = "unknown"

    def func(self, value: Any, flags: int) -> Result:
        return None


class LiteralType(Type):
    name = "literal"

    def __init__(self, value: str) -> None:
        self.value = value
        self._issue = Leaf("invalid_literal", expected=(value,))

    def func(self, value: Any, flags: int) -> Result:
        return None if _same(value, self.value) else self._issue


class Optional(Type):
    """A property that may be absent. Valid only as an object's property."""

    name = "optional"

    def __init__(self, inner: Type) -> None:
        self.inner = inner

    def func(self, value: Any, flags: int) -> Result:
        return None if flags & MISSING_VALUE else self.inner.func(value, flags)


class ObjectType(Type):
    name = "object"
    _invalid = Leaf("invalid_type", expected=("object",))

    def __init__(self, shape: Mapping[str, Type], rest: Type | None = None) -> None:
        self.shape = dict(shape)
        self.rest = rest
        required = [k for k, t in self.shape.items() if not isinstance(t, Optional)]
        optional = [k for k, t in self.shape.items() if isinstance(t, Optional)]
        self._keys = [*required, *optional]
        self._types = [self.shape[k] for k in self._keys]
        self._required = len(required)
        self._index = {k: i for i, k in enumerate(self._keys)}
        self._missing = [Prepend(k, Leaf("missing_value")) for k in required]

    def func(self, value: Any, flags: int) -> Result:
        if input_type(value) != "object":
            return self._invalid
        issues: Tree | None = None
        unrecognized: list[Key] = []
        seen: set[int] = set()
        # In passthrough mode with no rest type, valita skips straight to the declared keys.
        if flags & FORBID_EXTRA_KEYS or self.rest is not None:
            for key in js_keys(value):
                index = self._index.get(key)
                if index is not None:
                    seen.add(index)
                    result = self._types[index].func(value[key], flags)
                elif self.rest is not None:
                    result = self.rest.func(value[key], flags)
                else:
                    unrecognized.append(key)
                    continue
                if result is not None:
                    issues = _join(issues, Prepend(key, result))
        for i, key in enumerate(self._keys):
            if i in seen:
                continue
            key_flags = flags
            if key not in value:
                if i < self._required:
                    issues = _join(issues, self._missing[i])
                    continue
                key_flags |= MISSING_VALUE
            result = self._types[i].func(value.get(key), key_flags)
            if result is not None:
                issues = _join(issues, Prepend(key, result))
        if unrecognized:
            issues = _join(issues, Leaf("unrecognized_keys", keys=tuple(unrecognized)))
        return issues


class ArrayType(Type):
    """An array, a tuple, or a tuple followed by an array (`tuple([...]).concat(array(...))`)."""

    name = "array"
    _invalid = Leaf("invalid_type", expected=("array",))

    def __init__(self, prefix: Sequence[Type], rest: Type | None) -> None:
        self.prefix = list(prefix)
        self.rest = rest
        min_length = len(self.prefix)
        self._invalid_length = Leaf(
            "invalid_length", min_length=min_length, max_length=None if rest is not None else min_length
        )

    def concat(self, other: "ArrayType") -> "ArrayType":
        assert self.rest is None
        assert not other.prefix
        return ArrayType(self.prefix, other.rest)

    def func(self, value: Any, flags: int) -> Result:
        if not isinstance(value, list | tuple):
            return self._invalid
        if len(value) < len(self.prefix) or (self.rest is None and len(value) > len(self.prefix)):
            return self._invalid_length
        issues: Tree | None = None
        for i, item in enumerate(value):
            type_ = self.prefix[i] if i < len(self.prefix) else self.rest
            assert type_ is not None
            result = type_.func(item, flags)
            if result is not None:
                issues = _join(issues, Prepend(i, result))
        return issues


def _by_type(options: Sequence[Type]) -> tuple[dict[str, list[Type]], list[str]]:
    """Options grouped by the input type each takes, and those types in order (`groupTerminals`)."""
    types: dict[str, list[Type]] = {}
    for option in options:
        assert not isinstance(option, LiteralType | Optional | _Unknown)
        types.setdefault(option.name, []).append(option)
    return types, list(types)


def _key_matcher(objects: Sequence[ObjectType], key: str) -> Callable[[Any, int], Result] | None:
    """`createObjectKeyMatcher`: the union's options told apart by a literal at `key`, if they can be."""
    terminals = [o.shape[key] for o in objects]
    if not all(isinstance(t, LiteralType) for t in terminals):
        types, _ = _by_type(terminals)
        assert any(len(group) > 1 for group in types.values())
        return None
    literals = [t.value for t in terminals if isinstance(t, LiteralType)]
    assert len(set(literals)) == len(literals)
    missing = Prepend(key, Leaf("missing_value"))
    issue = Prepend(key, Leaf("invalid_literal", expected=tuple(literals)))

    def match(value: Any, flags: int) -> Result:
        if key not in value:
            return missing
        for literal, option in zip(literals, objects, strict=True):
            if _same(value[key], literal):
                return option.func(value, flags)
        return issue

    return match


class UnionType(Type):
    name = "union"

    def __init__(self, options: Sequence[Type]) -> None:
        self.options = list(options)
        self._types, expected = _by_type(self.options)
        self._issue = Leaf("invalid_type", expected=tuple(expected))
        self._objects: Callable[[Any, int], Result] | None = None
        objects = [o for o in self.options if isinstance(o, ObjectType)]
        if len(objects) >= 2:
            common = [k for k in objects[0].shape if all(k in o.shape for o in objects[1:])]
            for key in common:
                if (matcher := _key_matcher(objects, key)) is not None:
                    self._objects = matcher
                    break

    def func(self, value: Any, flags: int) -> Result:
        if self._objects is not None and input_type(value) == "object":
            return self._objects(value, flags)
        tree: Tree = self._issue
        options = self._types.get(input_type(value), [])
        for count, option in enumerate(options):
            result = option.func(value, flags)
            if result is None:
                return None
            tree = _join(tree, result) if count > 0 else result
        return Leaf("invalid_union") if len(options) > 1 else tree


string: Type = _Primitive("string")
number: Type = _Primitive("number")
boolean: Type = _Primitive("boolean")
unknown: Type = _Unknown()


def literal(value: str) -> Type:
    return LiteralType(value)


def object_(shape: Mapping[str, Type]) -> ObjectType:
    return ObjectType(shape)


def record(value: Type) -> Type:
    return ObjectType({}, value)


def array(item: Type) -> ArrayType:
    return ArrayType([], item)


def tuple_(items: Sequence[Type]) -> ArrayType:
    return ArrayType(items, None)


def union(*options: Type) -> UnionType:
    return UnionType(options)


# Upstream's message formatting, from `shared/src/valita.ts`.


def _display(value: object) -> str:
    match input_type(value):
        case "string" | "boolean" | "number":
            return js_stringify(value)
        case "null" | "array" as kind:
            return kind
        case _:
            return "object"


def _at_path(value: Any, path: Sequence[Key]) -> object:
    for key in path:
        value = value[key]
    return value


def _display_list(word: str, expected: Sequence[Any], display: Callable[[Any], str] = str) -> str:
    if len(expected) == 1:
        return display(expected[0])
    suffix = f"{display(expected[-2])} {word} {display(expected[-1])}"
    if len(expected) == 2:
        return suffix
    return ", ".join(display(x) for x in expected[:-2]) + ", " + suffix


def _join_path(path: Sequence[Key]) -> str:
    return ".".join(js_number(k) if isinstance(k, int) else k for k in path)


def message(tree: Tree, value: object, schema: Type, mode: Mode | None) -> str:
    """Upstream's `getMessage`, for the issue tree `schema` produced for `value`."""
    leaf, path = _first_issue(tree)
    at_path = f" at {_join_path(path)}" if path else ""
    match leaf.code:
        case "invalid_type":
            return f"Expected {_display_list('or', leaf.expected)}{at_path}. Got {_display(_at_path(value, path))}"
        case "missing_value":
            parent = f" at {_join_path(path[:-1])}" if len(path) > 1 else ""
            return f"Missing property {path[-1]}{parent}"
        case "invalid_literal":
            expected = _display_list("or", leaf.expected, _display)
            return f"Expected literal value {expected}{at_path} Got {_display(_at_path(value, path))}"
        case "invalid_length":
            length = (
                leaf.min_length if leaf.min_length == leaf.max_length else f"between {leaf.min_length} and undefined"
            )
            # Upstream reads the length off the whole value, not the array at the path.
            got = len(value) if isinstance(value, list | tuple) else "undefined"
            return f"Expected array with length {length}{at_path}. Got array with length {got}"
        case "unrecognized_keys":
            if len(leaf.keys) == 1:
                return f"Unexpected property {leaf.keys[0]}{at_path}"
            return f"Unexpected properties {_display_list('and', leaf.keys)}{at_path}"
        case _:
            # An `invalid_union`, which only a union at the top of the schema produces here.
            assert isinstance(schema, UnionType)
            return _deepest_union_error(value, schema, mode or "strict")


def _path_cmp(a: tuple[Type, Tree], b: tuple[Type, Tree]) -> int:
    """`pathCmp`: the longer path first, then the later key. JavaScript compares strings by UTF-16 unit."""
    a_path, b_path = _first_issue(a[1])[1], _first_issue(b[1])[1]
    if len(a_path) != len(b_path):
        return len(b_path) - len(a_path)
    for x, y in zip(a_path, b_path, strict=True):
        x_key, y_key = (k.encode("utf-16-be") if isinstance(k, str) else k for k in (x, y))
        assert type(x_key) is type(y_key)
        if y_key > x_key:  # ty: ignore[unsupported-operator]
            return -1
        if y_key < x_key:  # ty: ignore[unsupported-operator]
            return 1
    return 0


def _deepest_union_error(value: object, schema: UnionType, mode: Mode) -> str:
    failures = [(option, tree) for option in schema.options if (tree := option.try_(value, mode)) is not None]
    failures.sort(key=cmp_to_key(_path_cmp))
    if len(failures) == 1 or _path_cmp(failures[0], failures[1]) < 0:
        return message(failures[0][1], value, failures[0][0], mode)
    return f"Invalid union value: {js_stringify(value)}"


def parse_error(value: object, schema: Type, mode: Mode | None) -> str | None:
    """Upstream's `v.test`: `None` when `value` parses, otherwise the message `v.parse` would throw."""
    tree = schema.try_(value, mode)
    return None if tree is None else message(tree, value, schema, mode)
