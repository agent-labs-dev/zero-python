"""The base every wire model shares.

Upstream's optional fields are absent when unset, never `null`. zero-cache hashes the ASTs it receives,
so `"limit": null` and no `limit` are two different queries to it. A model therefore drops a `None`
field when it serializes, except a field listed in `nullable`, where `None` is a real JSON `null` and
survives whenever the caller set it.

Parsing is the mirror image: an optional field upstream may be absent but not `null`, so a `null` is
refused unless the field is `nullable`.
"""

import functools
from typing import Annotated, Any, ClassVar, Self

from pydantic import (
    BaseModel,
    ConfigDict,
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

from zero_server.shared.json import JSONValue

WIRE: Any = {"source": "wire"}
"""Validation context marking a value as arriving off the wire rather than built in Python."""


def parse_wire[T](adapter: TypeAdapter[T], value: object) -> T:
    return adapter.validate_python(value, context=WIRE)


def _validate_off_the_wire(value: Any, handler: ValidatorFunctionWrapHandler, info: ValidationInfo) -> Any:
    return handler(value) if info.context == WIRE else value


type WireChecked[T] = Annotated[
    T, WrapValidator(_validate_off_the_wire), PlainSerializer(lambda value: value, return_type=Any)
]
"""A `T` when parsed off the wire, anything when built in Python and serialized as given.

Upstream's query builder puts whatever the caller passed into the AST and only its wire schema checks
it, so a value the schema refuses can still reach SQL or the query endpoint's response.
"""


class WireModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        extra="ignore",
    )

    nullable: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def _refuse_null_for_optional(cls, data: Any, info: ValidationInfo) -> Any:
        if info.context == WIRE and isinstance(data, dict):
            for key in _optional_keys(cls):
                if key in data and data[key] is None:
                    raise ValueError(f"{key} may be absent but not null")
        return data

    @model_serializer(mode="wrap")
    def _omit_unset(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        dumped: dict[str, Any] = handler(self)
        out: dict[str, Any] = {}
        for name, info in type(self).model_fields.items():
            key = info.serialization_alias or info.alias or name
            if key not in dumped:
                continue
            value = dumped[key]
            if value is None and not (name in self.nullable and name in self.model_fields_set):
                continue
            out[key] = value
        return out

    def to_json(self) -> dict[str, JSONValue]:
        """The JSON object upstream would send for this model."""
        return self.model_dump(mode="json")

    @classmethod
    def from_json(cls, value: object) -> Self:
        return cls.model_validate(value, context=WIRE)


@functools.cache
def _optional_keys(cls: type[WireModel]) -> tuple[str, ...]:
    """The keys, by name or alias, of the fields that may be absent but not null."""
    return tuple(
        dict.fromkeys(
            key
            for name, field in cls.model_fields.items()
            if not (name in cls.nullable or field.is_required())
            for key in (name, field.alias or name)
        )
    )


type Number = StrictInt | StrictFloat
"""A JSON number. `bool` is not one, though Python says otherwise."""


class StrictWireModel(WireModel):
    """Refuses keys it does not declare, as upstream's default (strict) parse mode does."""

    model_config = ConfigDict(extra="forbid")
