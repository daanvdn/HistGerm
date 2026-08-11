"""Strict shared value models for HistGerm metadata."""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    AnyHttpUrl,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainValidator,
    StringConstraints,
    TypeAdapter,
    field_serializer,
    field_validator,
    model_validator,
)

_STABLE_ID_PATTERN = re.compile(
    r"^(res|ver|dist|comp|work|wit|doc|ann|rel|pub|evidence)-"
    r"[a-z0-9]+(?:-[a-z0-9]+)*$"
)
_VOCABULARY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_EXTENSION_NAMESPACE_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$"
)
_JSON_POINTER_PATTERN = re.compile(r"^(?:|/(?:[^~]|~[01])*)$")
_FORBIDDEN_EXTENSION_KEYS = frozenset(
    {"path", "local_path", "payload", "data", "content"}
)
_URL_ADAPTER = TypeAdapter(AnyHttpUrl)


class HistGermModel(BaseModel):
    """Base for immutable, strict models with a closed core schema."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


def _reject_surrounding_whitespace(value: Any) -> Any:
    if isinstance(value, str) and value != value.strip():
        raise ValueError("must not contain leading or trailing whitespace")
    return value


StableId = Annotated[
    str,
    BeforeValidator(_reject_surrounding_whitespace),
    StringConstraints(strict=True, pattern=_STABLE_ID_PATTERN.pattern),
]
NonEmptyStr = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1),
]
VocabularyId = Annotated[
    str,
    BeforeValidator(_reject_surrounding_whitespace),
    StringConstraints(strict=True, pattern=_VOCABULARY_ID_PATTERN.pattern),
]
RegistryId = VocabularyId
ExtensionNamespace = Annotated[
    str,
    BeforeValidator(_reject_surrounding_whitespace),
    StringConstraints(strict=True, pattern=_EXTENSION_NAMESPACE_PATTERN.pattern),
]


def _validate_http_url(value: str) -> str:
    if any(character.isspace() for character in value):
        raise ValueError("URL must not contain whitespace")
    try:
        parsed = _URL_ADAPTER.validate_python(value, strict=True)
    except ValueError as error:
        raise ValueError(
            "must be an absolute http or https URL with a valid host"
        ) from error
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("URL scheme must be http or https")
    return value


HttpUrlValue = Annotated[
    str,
    BeforeValidator(_reject_surrounding_whitespace),
    StringConstraints(strict=True, min_length=1),
    AfterValidator(_validate_http_url),
]


def _validate_json_pointer(value: str) -> str:
    if not _JSON_POINTER_PATTERN.fullmatch(value):
        raise ValueError(
            "must be an RFC 6901 absolute JSON Pointer; start with '/' and "
            "escape '~' as '~0' or '~1' (the empty root pointer is also valid)"
        )
    return value


JsonPointer = Annotated[
    str,
    StringConstraints(strict=True),
    AfterValidator(_validate_json_pointer),
]


def _validate_json_string(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("JSON strings and object keys must be strings")
    return value


JsonString = Annotated[str, PlainValidator(_validate_json_string)]

type JsonValue = (
    None
    | bool
    | int
    | Annotated[float, Field(allow_inf_nan=False)]
    | JsonString
    | list[JsonValue]
    | dict[JsonString, JsonValue]
)


def _validate_extension_payload(
    value: Mapping[str, JsonValue], *, location: str
) -> None:
    for key, child in value.items():
        if key in _FORBIDDEN_EXTENSION_KEYS:
            raise ValueError(
                f"extension key {key!r} is forbidden at {location}; "
                "extensions may contain metadata only, not paths or payloads"
            )
        if isinstance(child, Mapping):
            _validate_extension_payload(child, location=f"{location}.{key}")
        elif isinstance(child, list):
            _validate_extension_list(child, location=f"{location}.{key}")
        elif isinstance(child, float) and not math.isfinite(child):
            raise ValueError(
                f"extension number at {location}.{key} must be finite JSON"
            )


def _validate_extension_list(value: list[JsonValue], *, location: str) -> None:
    for index, child in enumerate(value):
        child_location = f"{location}[{index}]"
        if isinstance(child, Mapping):
            _validate_extension_payload(child, location=child_location)
        elif isinstance(child, list):
            _validate_extension_list(child, location=child_location)
        elif isinstance(child, float) and not math.isfinite(child):
            raise ValueError(
                f"extension number at {child_location} must be finite JSON"
            )


def _validate_extension_data(
    value: dict[ExtensionNamespace, dict[JsonString, JsonValue]],
) -> dict[ExtensionNamespace, dict[JsonString, JsonValue]]:
    for namespace, payload in value.items():
        _validate_extension_payload(payload, location=namespace)
    return value


ExtensionData = Annotated[
    dict[ExtensionNamespace, dict[JsonString, JsonValue]],
    AfterValidator(_validate_extension_data),
]


class KnownValue[T](HistGermModel):
    status: Literal["known"]
    value: T

    @field_validator("value")
    @classmethod
    def value_must_not_be_empty(cls, value: T) -> T:
        if (
            isinstance(value, Collection)
            and not isinstance(value, (str, bytes, bytearray))
            and len(value) == 0
        ):
            raise ValueError("known collection value must not be empty")
        return value

    @field_serializer("value", when_used="json")
    def serialize_value(self, value: T) -> Any:
        if isinstance(value, (set, frozenset)):
            return sorted(value)
        return value


class UnknownValue(HistGermModel):
    status: Literal["unknown"]


class NotApplicableValue(HistGermModel):
    status: Literal["not_applicable"]


class NotPubliclyAvailableValue(HistGermModel):
    status: Literal["not_publicly_available"]


type KnowledgeValue[T] = Annotated[
    KnownValue[T] | UnknownValue | NotApplicableValue | NotPubliclyAvailableValue,
    Field(discriminator="status"),
]


class LocalizedName(HistGermModel):
    text: NonEmptyStr
    language: KnowledgeValue[RegistryId]
    name_type: VocabularyId


class ExternalIdentifier(HistGermModel):
    scheme: NonEmptyStr
    value: NonEmptyStr
    resolver_url: KnowledgeValue[HttpUrlValue]


class ResponsibleParty(HistGermModel):
    name: NonEmptyStr
    party_type: VocabularyId
    role: VocabularyId
    identifier: KnowledgeValue[ExternalIdentifier]


class DateRange(HistGermModel):
    earliest_year: KnowledgeValue[int]
    latest_year: KnowledgeValue[int]
    label: KnowledgeValue[NonEmptyStr]
    dating_method: KnowledgeValue[VocabularyId]
    certainty: KnowledgeValue[VocabularyId]

    @model_validator(mode="after")
    def validate_known_range(self) -> DateRange:
        if (
            isinstance(self.earliest_year, KnownValue)
            and isinstance(self.latest_year, KnownValue)
            and self.earliest_year.value > self.latest_year.value
        ):
            raise ValueError(
                "earliest_year must be less than or equal to latest_year "
                "when both endpoints are known"
            )
        return self


def _sequence_to_frozenset(value: Any) -> Any:
    if isinstance(value, list):
        return frozenset(value)
    return value


class GeographicCoverage(HistGermModel):
    region_ids: KnowledgeValue[frozenset[RegistryId]]
    dialect_ids: KnowledgeValue[frozenset[RegistryId]]
    certainty: KnowledgeValue[VocabularyId]
    note: KnowledgeValue[NonEmptyStr]

    @field_validator("region_ids", "dialect_ids", mode="before")
    @classmethod
    def accept_yaml_sequences(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and value.get("status") == "known":
            return {**value, "value": _sequence_to_frozenset(value.get("value"))}
        return value


_ENTITY_PREFIXES = {
    "resource": "res-",
    "version": "ver-",
    "distribution": "dist-",
    "component": "comp-",
    "work": "work-",
    "witness": "wit-",
    "document": "doc-",
    "annotation": "ann-",
    "publication": "pub-",
}


class EntityReference(HistGermModel):
    entity_type: Literal[
        "resource",
        "version",
        "distribution",
        "component",
        "work",
        "witness",
        "document",
        "annotation",
        "publication",
    ]
    id: StableId

    @model_validator(mode="after")
    def validate_prefix(self) -> EntityReference:
        expected = _ENTITY_PREFIXES[self.entity_type]
        if not self.id.startswith(expected):
            raise ValueError(
                f"id for entity_type {self.entity_type!r} must use the "
                f"{expected!r} prefix"
            )
        return self


_SELECTION_PREFIXES = {
    "resource_ids": "res-",
    "version_ids": "ver-",
    "component_ids": "comp-",
    "document_ids": "doc-",
    "annotation_ids": "ann-",
}


class SelectionScope(HistGermModel):
    resource_ids: frozenset[StableId] = Field(default_factory=frozenset)
    version_ids: frozenset[StableId] = Field(default_factory=frozenset)
    component_ids: frozenset[StableId] = Field(default_factory=frozenset)
    document_ids: frozenset[StableId] = Field(default_factory=frozenset)
    annotation_ids: frozenset[StableId] = Field(default_factory=frozenset)
    filter: KnowledgeValue[dict[str, JsonValue]]

    @field_validator(*_SELECTION_PREFIXES, mode="before")
    @classmethod
    def accept_yaml_sequences(cls, value: Any) -> Any:
        return _sequence_to_frozenset(value)

    @field_validator(*_SELECTION_PREFIXES)
    @classmethod
    def validate_selected_prefixes(
        cls, value: frozenset[StableId], info: Any
    ) -> frozenset[StableId]:
        expected = _SELECTION_PREFIXES[info.field_name]
        invalid = sorted(item for item in value if not item.startswith(expected))
        if invalid:
            raise ValueError(
                f"{info.field_name} entries must use the {expected!r} prefix; "
                f"invalid: {', '.join(invalid)}"
            )
        return value

    @model_validator(mode="after")
    def validate_non_empty_selection(self) -> SelectionScope:
        has_ids = any(getattr(self, field) for field in _SELECTION_PREFIXES)
        if not has_ids and not isinstance(self.filter, KnownValue):
            raise ValueError(
                "selection scope requires at least one selected ID or a known "
                "non-empty filter"
            )
        return self

    @field_serializer(*_SELECTION_PREFIXES, when_used="json")
    def serialize_ids(self, value: frozenset[StableId]) -> list[StableId]:
        return sorted(value)


__all__ = [
    "DateRange",
    "EntityReference",
    "ExtensionData",
    "ExtensionNamespace",
    "ExternalIdentifier",
    "GeographicCoverage",
    "HistGermModel",
    "HttpUrlValue",
    "JsonPointer",
    "JsonValue",
    "KnowledgeValue",
    "KnownValue",
    "LocalizedName",
    "NonEmptyStr",
    "NotApplicableValue",
    "NotPubliclyAvailableValue",
    "RegistryId",
    "ResponsibleParty",
    "SelectionScope",
    "StableId",
    "UnknownValue",
    "VocabularyId",
]
