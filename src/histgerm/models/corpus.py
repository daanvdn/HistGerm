"""Corpus hierarchy, text-description, and size-measurement models."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, Literal

from pydantic import Field, field_serializer, field_validator, model_validator

from histgerm.models.access import FormatDescription
from histgerm.models.common import (
    DateRange,
    ExtensionData,
    ExternalIdentifier,
    GeographicCoverage,
    HistGermModel,
    KnowledgeValue,
    KnownValue,
    NonEmptyStr,
    NotApplicableValue,
    RegistryId,
    SelectionScope,
    StableId,
    VocabularyId,
)
from histgerm.models.entities import Authorship

_SIZE_UNITS = frozenset(
    {"document", "sentence", "orthographic_word", "token", "character", "byte"}
)


def _as_frozenset(value: Any) -> Any:
    if isinstance(value, list):
        return frozenset(value)
    return value


def _known_set(value: Any) -> Any:
    if isinstance(value, Mapping) and value.get("status") == "known":
        return {**value, "value": _as_frozenset(value.get("value"))}
    return value


def _require_prefix(value: StableId, prefix: str, label: str) -> StableId:
    if not value.startswith(prefix):
        raise ValueError(f"{label} must use the {prefix!r} prefix")
    return value


class SizeMeasurement(HistGermModel):
    unit: VocabularyId
    value: int = Field(gt=0)
    version_id: StableId
    scope: SelectionScope
    counting_method: NonEmptyStr
    origin: Literal["reported", "locally_computed"]
    computed_on: KnowledgeValue[date]
    evidence_ids: frozenset[StableId] = Field(default_factory=frozenset)
    uncertainty_note: KnowledgeValue[NonEmptyStr]

    @field_validator("unit")
    @classmethod
    def validate_unit(cls, value: VocabularyId) -> VocabularyId:
        if value not in _SIZE_UNITS:
            raise ValueError(f"unsupported size unit: {value!r}")
        return value

    @field_validator("version_id")
    @classmethod
    def validate_version_id(cls, value: StableId) -> StableId:
        return _require_prefix(value, "ver-", "version_id")

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def accept_yaml_evidence_ids(cls, value: Any) -> Any:
        return _as_frozenset(value)

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, value: frozenset[StableId]) -> frozenset[StableId]:
        invalid = sorted(item for item in value if not item.startswith("evidence-"))
        if invalid:
            raise ValueError(
                "evidence_ids entries must use the 'evidence-' prefix; invalid: "
                + ", ".join(invalid)
            )
        return value

    @model_validator(mode="after")
    def validate_origin(self) -> SizeMeasurement:
        if self.origin == "reported":
            if not isinstance(self.computed_on, NotApplicableValue):
                raise ValueError(
                    "reported measurement requires computed_on to be not_applicable"
                )
            if not self.evidence_ids:
                raise ValueError("reported measurement requires evidence_ids")
        elif not isinstance(self.computed_on, KnownValue):
            raise ValueError(
                "locally_computed measurement requires a known computation date"
            )
        return self

    @field_serializer("evidence_ids", when_used="json")
    def serialize_evidence_ids(self, value: frozenset[StableId]) -> list[StableId]:
        return sorted(value)


class LanguageMixture(HistGermModel):
    primary_language_ids: KnowledgeValue[frozenset[RegistryId]]
    mixed_language_ids: KnowledgeValue[frozenset[RegistryId]]
    code_switching: KnowledgeValue[bool]
    note: KnowledgeValue[NonEmptyStr]

    @field_validator("primary_language_ids", "mixed_language_ids", mode="before")
    @classmethod
    def accept_yaml_sets(cls, value: Any) -> Any:
        return _known_set(value)


class EditorialIntervention(HistGermModel):
    kind: VocabularyId
    description: KnowledgeValue[NonEmptyStr]


class TextLayer(HistGermModel):
    id: NonEmptyStr
    layer_type: VocabularyId
    description: KnowledgeValue[NonEmptyStr]
    scope: SelectionScope


class Document(HistGermModel):
    id: StableId
    title: KnowledgeValue[NonEmptyStr]
    component_ids: frozenset[StableId] = Field(min_length=1)
    work_ids: frozenset[StableId] = Field(default_factory=frozenset)
    witness_ids: frozenset[StableId] = Field(default_factory=frozenset)
    edition_witness_ids: frozenset[StableId] = Field(default_factory=frozenset)
    external_identifiers: list[ExternalIdentifier] = Field(default_factory=list)
    authorship: KnowledgeValue[list[Authorship]]
    language_stage_ids: KnowledgeValue[frozenset[VocabularyId]]
    chronology: KnowledgeValue[DateRange]
    geography: KnowledgeValue[GeographicCoverage]
    genres: KnowledgeValue[frozenset[RegistryId]]
    text_types: KnowledgeValue[frozenset[RegistryId]]
    language_mixture: KnowledgeValue[LanguageMixture]
    stable_segment_identifier_types: KnowledgeValue[frozenset[NonEmptyStr]]
    size_measurements: list[SizeMeasurement] = Field(default_factory=list)
    extensions: ExtensionData = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: StableId) -> StableId:
        return _require_prefix(value, "doc-", "document id")

    @field_validator(
        "component_ids",
        "work_ids",
        "witness_ids",
        "edition_witness_ids",
        mode="before",
    )
    @classmethod
    def accept_yaml_references(cls, value: Any) -> Any:
        return _as_frozenset(value)

    @field_validator(
        "language_stage_ids",
        "genres",
        "text_types",
        "stable_segment_identifier_types",
        mode="before",
    )
    @classmethod
    def accept_yaml_known_sets(cls, value: Any) -> Any:
        return _known_set(value)

    @model_validator(mode="after")
    def validate_reference_prefixes(self) -> Document:
        prefixes = {
            "component_ids": "comp-",
            "work_ids": "work-",
            "witness_ids": "wit-",
            "edition_witness_ids": "wit-",
        }
        for field, prefix in prefixes.items():
            invalid = sorted(
                item for item in getattr(self, field) if not item.startswith(prefix)
            )
            if invalid:
                raise ValueError(
                    f"{field} entries must use the {prefix!r} prefix; invalid: "
                    + ", ".join(invalid)
                )
        return self

    @field_serializer(
        "component_ids",
        "work_ids",
        "witness_ids",
        "edition_witness_ids",
        when_used="json",
    )
    def serialize_references(self, value: frozenset[StableId]) -> list[StableId]:
        return sorted(value)


class CorpusComponent(HistGermModel):
    id: StableId
    name: NonEmptyStr
    description: KnowledgeValue[NonEmptyStr]
    parent_component_id: KnowledgeValue[StableId]
    language_stage_ids: KnowledgeValue[frozenset[VocabularyId]]
    chronology: KnowledgeValue[DateRange]
    geography: KnowledgeValue[GeographicCoverage]
    genres: KnowledgeValue[frozenset[RegistryId]]
    text_types: KnowledgeValue[frozenset[RegistryId]]
    document_ids: frozenset[StableId] = Field(default_factory=frozenset)
    size_measurements: list[SizeMeasurement] = Field(default_factory=list)
    extensions: ExtensionData = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: StableId) -> StableId:
        return _require_prefix(value, "comp-", "component id")

    @field_validator("document_ids", mode="before")
    @classmethod
    def accept_yaml_document_ids(cls, value: Any) -> Any:
        return _as_frozenset(value)

    @field_validator("language_stage_ids", "genres", "text_types", mode="before")
    @classmethod
    def accept_yaml_known_sets(cls, value: Any) -> Any:
        return _known_set(value)

    @model_validator(mode="after")
    def validate_references(self) -> CorpusComponent:
        if isinstance(self.parent_component_id, KnownValue):
            parent = self.parent_component_id.value
            _require_prefix(parent, "comp-", "parent_component_id")
            if parent == self.id:
                raise ValueError("component cannot be its own parent")
        invalid = sorted(
            item for item in self.document_ids if not item.startswith("doc-")
        )
        if invalid:
            raise ValueError(
                "document_ids entries must use the 'doc-' prefix; invalid: "
                + ", ".join(invalid)
            )
        return self

    @field_serializer("document_ids", when_used="json")
    def serialize_document_ids(self, value: frozenset[StableId]) -> list[StableId]:
        return sorted(value)


class CorpusProfile(HistGermModel):
    transcription_methods: KnowledgeValue[frozenset[VocabularyId]]
    editorial_interventions: KnowledgeValue[list[EditorialIntervention]]
    unicode_normalization: KnowledgeValue[VocabularyId]
    character_repertoire: KnowledgeValue[NonEmptyStr]
    tokenization_convention: KnowledgeValue[NonEmptyStr]
    sentence_segmentation_convention: KnowledgeValue[NonEmptyStr]
    text_layers: KnowledgeValue[list[TextLayer]]
    stable_document_identifiers: KnowledgeValue[bool]
    stable_token_identifiers: KnowledgeValue[bool]
    corpus_schema: KnowledgeValue[list[FormatDescription]]
    access_restrictions: KnowledgeValue[NonEmptyStr]

    @field_validator("transcription_methods", mode="before")
    @classmethod
    def accept_yaml_methods(cls, value: Any) -> Any:
        return _known_set(value)


__all__ = [
    "CorpusComponent",
    "CorpusProfile",
    "Document",
    "EditorialIntervention",
    "LanguageMixture",
    "SizeMeasurement",
    "TextLayer",
]
