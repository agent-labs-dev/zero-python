"""The base every wire model shares.

Upstream's optional fields are absent when unset, never `null`. zero-cache hashes the ASTs it receives,
so `"limit": null` and no `limit` are two different queries to it. A model therefore drops a `None`
field when it serializes, except a field listed in `nullable`, where `None` is a real JSON `null` and
survives whenever the caller set it. For the same reason keys come out in the order upstream writes
them: fields are declared in that order, and `_ordered` covers a model upstream writes more than one way.

Parsing is the mirror image: an optional field upstream may be absent but not `null`, so a `null` is
refused unless the field is `nullable`. A model only ever parsed declares such a field `Omittable`; a
model also built in Python, where `None` means unset, extends `RefusesNullOffWire` instead. Off the
wire only the upstream (alias) keys count; a field's Python name is for building a model in Python,
and on the wire it is just another unknown key.
"""

import functools
import typing
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    GetCoreSchemaHandler,
    PlainSerializer,
    SerializerFunctionWrapHandler,
    StrictFloat,
    StrictInt,
    TypeAdapter,
    ValidationInfo,
    ValidatorFunctionWrapHandler,
    WrapValidator,
    model_serializer,
    model_validator,
)
from pydantic_core import CoreSchema, core_schema

from zero_server.shared.json import JSONValue
from zero_server.shared.json_parse import parse_body

WIRE: Any = {"source": "wire"}
"""Validation context marking a value as arriving off the wire rather than built in Python."""

_DECODED: Any = {"source": "wire", "decoded": True}
"""`WIRE` for a body decoded here from its bytes, whose every part is therefore plain JSON."""


def parse_wire[T](adapter: TypeAdapter[T], value: object) -> T:
    """Validate `value` off the wire. Raw bytes are decoded first, raising what `JSON.parse` would throw."""
    if isinstance(value, bytes):
        return adapter.validate_python(parse_body(value), context=_DECODED, by_alias=True, by_name=False)
    return adapter.validate_python(value, context=WIRE, by_alias=True, by_name=False)


def _off_the_wire(info: ValidationInfo) -> bool:
    return info.context is WIRE or info.context is _DECODED


def _validate_off_the_wire(value: Any, handler: ValidatorFunctionWrapHandler, info: ValidationInfo) -> Any:
    return handler(value) if _off_the_wire(info) else value


type WireChecked[T] = Annotated[
    T, WrapValidator(_validate_off_the_wire), PlainSerializer(lambda value: value, return_type=Any)
]
"""A `T` when parsed off the wire, anything when built in Python and serialized as given.

Upstream's query builder puts whatever the caller passed into the AST and only its wire schema checks
it, so a value the schema refuses can still reach SQL or the query endpoint's response.
"""

_JSON_SCALARS = frozenset({str, int, float, bool, type(None)})


def _is_plain_json(value: Any) -> bool:
    """Whether `value` is built only of the exact types `json.loads` returns."""
    kind = type(value)
    if kind is list:
        for item in value:  # noqa: SIM110 - a loop runs faster than all() over a generator
            if type(item) not in _JSON_SCALARS and not _is_plain_json(item):  # python-only: a tuple, enum, ...
                return False
        return True
    if kind is dict:
        for key, item in value.items():
            if type(key) is not str or (type(item) not in _JSON_SCALARS and not _is_plain_json(item)):
                return False  # python-only: a key that is not a str, or a value no JSON decodes to
        return True
    return kind in _JSON_SCALARS


class _TakePlainJSON:
    def __get_pydantic_core_schema__(self, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        container = typing.get_origin(source)
        kinds = {container} if container in (list, dict) else {*_JSON_SCALARS, list, dict}

        def take(value: Any, validate: ValidatorFunctionWrapHandler, info: ValidationInfo) -> Any:
            if type(value) in kinds and (info.context is _DECODED or _is_plain_json(value)):
                return value
            return validate(value)

        return core_schema.with_info_wrap_validator_function(take, handler(source))


type PlainJSON[T] = Annotated[T, _TakePlainJSON()]
"""A JSON-typed `T` that takes plain JSON as is, unchecked if decoded here, and validates anything else.

Pydantic's `JsonValue` check rebuilds every container and costs most of a parse, though what
`json.loads` returns always passes it unchanged.
"""


class _NotNull:
    def __get_pydantic_core_schema__(self, source: Any, handler: GetCoreSchemaHandler) -> CoreSchema:
        (present, _none) = typing.get_args(source)
        return handler.generate_schema(present)


type Omittable[T] = Annotated[T | None, _NotNull()]
"""A field that may be absent, read as `None`, but never `null`: give it a `None` default."""


@functools.cache
def _optional_keys(cls: type["WireModel"]) -> tuple[str, ...]:
    """The wire keys (aliases) of the fields that default to `None` and may not be `null` off the wire."""
    return tuple(
        field.alias or name
        for name, field in cls.model_fields.items()
        if field.default is None and name not in cls.nullable and typing.get_origin(field.annotation) is not Omittable
    )


_MODELS: set[type] = set()
"""Every `WireModel` class, which `isinstance` would find more slowly through Pydantic's `ABCMeta`."""


class WireModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        validate_by_alias=True,
        validate_by_name=True,
        serialize_by_alias=True,
        extra="ignore",
    )

    nullable: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        _MODELS.add(cls)
        if _optional_keys(cls) and not issubclass(cls, RefusesNullOffWire):  # python-only: a guard on our models
            raise TypeError(f"{cls.__name__} has optional fields: declare them Omittable or extend RefusesNullOffWire")

    @model_serializer(mode="wrap")
    def _omit_unset(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:  # python-only: `model_dump`
        # `model_dump`, which honours `include` and `exclude`. `to_json` walks the fields itself.
        dumped: dict[str, Any] = handler(self)
        out: dict[str, Any] = {}
        for name, key, nullable in _plan(type(self)):
            if key not in dumped:
                continue
            value = dumped[key]
            if value is None and not (nullable and name in self.model_fields_set):
                continue
            out[key] = value
        return self._ordered(out)

    def _ordered(self, dumped: dict[str, Any]) -> dict[str, Any]:
        return dumped

    def to_json(self) -> dict[str, JSONValue]:
        """The JSON object upstream would send for this model: what `model_dump` gives, built by hand.

        Pydantic 2.14's JSON mode replaces a lone surrogate in a dict key with U+FFFD, and its Python
        mode leaves tuples and enums for a second pass, so this walks the fields itself.
        """
        return _dump_model(self)

    @classmethod
    def from_json(cls, value: object) -> Self:
        """Validate `value` off the wire, as `parse_wire` does."""
        return parse_wire(_adapter(cls), value)


class RefusesNullOffWire(WireModel):
    """A model also built in Python, where `None` means unset, so `null` is refused only off the wire."""

    @model_validator(mode="before")
    @classmethod
    def _refuse_null_for_optional(cls, data: Any, info: ValidationInfo) -> Any:
        if _off_the_wire(info) and isinstance(data, dict):
            for key in _optional_keys(cls):
                if key in data and data[key] is None:
                    raise ValueError(f"{key} may be absent but not null")
        return data


@functools.cache
def _adapter[M: WireModel](cls: type[M]) -> TypeAdapter[M]:
    return TypeAdapter(cls)


@functools.cache
def _plan(cls: type[WireModel]) -> tuple[tuple[str, str, bool], ...]:
    """Each field's name, its wire key, and whether it is `nullable`."""
    return tuple((name, field.alias or name, name in cls.nullable) for name, field in cls.model_fields.items())


def _dump_model(model: WireModel) -> dict[str, Any]:
    values = model.__dict__
    out: dict[str, Any] = {}
    for name, key, nullable in _plan(type(model)):
        value = values[name]
        if value is None:
            if nullable and name in model.__pydantic_fields_set__:
                out[key] = None
        else:
            out[key] = _dump(value)
    return model._ordered(out)


_ANY: TypeAdapter[Any] = TypeAdapter(Any)


def _dump(value: Any) -> Any:
    """`value` as JSON: tuples become lists, string enums their values, models what `to_json` gives."""
    kind = type(value)
    if kind in _JSON_SCALARS:
        return value
    if kind is list or kind is tuple:
        return [_dump(item) for item in value]
    if kind is dict:
        return {key: _dump(item) for key, item in value.items()}
    if kind in _MODELS:
        return value.to_json()
    if isinstance(value, StrEnum):
        return value.value
    # Anything else only reaches a model through a `WireChecked` field: serialize it as Pydantic does.
    return _jsonable(_ANY.dump_python(value))  # python-only: what only a Python caller hands a builder


def _jsonable(value: Any) -> Any:  # python-only: a Python object a builder was handed, such as a dataclass
    match value:
        case StrEnum():
            return value.value
        case list() | tuple():
            return [_jsonable(item) for item in value]
        case dict():
            return {key: _jsonable(item) for key, item in value.items()}
        case _:
            return value


type Number = StrictInt | StrictFloat
"""A JSON number. `bool` is not one, though Python says otherwise."""


class StrictWireModel(WireModel):
    """Refuses keys it does not declare, as upstream's default (strict) parse mode does."""

    model_config = ConfigDict(extra="forbid")
