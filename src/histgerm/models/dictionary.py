"""Dictionary and lexicon category profile."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field, field_serializer, field_validator, model_validator

from histgerm.models.access import (
    AccessUrl,
    ApiInterface,
    AvailabilityState,
    FormatDescription,
    PermissionState,
)
from histgerm.models.common import (
    HistGermModel,
    HttpUrlValue,
    KnowledgeValue,
    KnownValue,
    NonEmptyStr,
    RegistryId,
    SelectionScope,
    StableId,
    VocabularyId,
)


def _as_frozenset(value: Any) -> Any:
    if isinstance(value, list):
        return frozenset(value)
    return value


def _known_set(value: Any) -> Any:
    if isinstance(value, Mapping) and value.get("status") == "known":
        return {**value, "value": _as_frozenset(value.get("value"))}
    return value


class LexicalFeatures(HistGermModel):
    headwords: KnowledgeValue[bool]
    lemmas: KnowledgeValue[bool]
    spelling_variants: KnowledgeValue[bool]
    part_of_speech: KnowledgeValue[bool]
    morphology: KnowledgeValue[bool]
    senses: KnowledgeValue[bool]
    etymology: KnowledgeValue[bool]


class CitationStructure(HistGermModel):
    works: KnowledgeValue[bool]
    editions: KnowledgeValue[bool]
    pages: KnowledgeValue[bool]
    lines: KnowledgeValue[bool]
    passages: KnowledgeValue[bool]
    note: KnowledgeValue[NonEmptyStr]


class CorpusOccurrenceLinks(HistGermModel):
    available: KnowledgeValue[bool]
    resource_ids: frozenset[StableId] = Field(default_factory=frozenset)
    link_unit: KnowledgeValue[VocabularyId]

    @field_validator("resource_ids", mode="before")
    @classmethod
    def accept_yaml_resource_ids(cls, value: Any) -> Any:
        return _as_frozenset(value)

    @field_validator("resource_ids")
    @classmethod
    def validate_resource_ids(cls, value: frozenset[StableId]) -> frozenset[StableId]:
        invalid = sorted(item for item in value if not item.startswith("res-"))
        if invalid:
            raise ValueError(
                "resource_ids entries must use the 'res-' prefix; invalid: "
                + ", ".join(invalid)
            )
        return value

    @model_validator(mode="after")
    def validate_availability(self) -> CorpusOccurrenceLinks:
        if (
            isinstance(self.available, KnownValue)
            and not self.available.value
            and self.resource_ids
        ):
            raise ValueError(
                "unavailable corpus occurrence links cannot name resource_ids"
            )
        return self

    @field_serializer("resource_ids", when_used="json")
    def serialize_resource_ids(self, value: frozenset[StableId]) -> list[StableId]:
        return sorted(value)


class SuitabilityAssessment(HistGermModel):
    task: VocabularyId
    status: VocabularyId
    scope: SelectionScope
    reasons: KnowledgeValue[list[NonEmptyStr]]
    limitations: KnowledgeValue[list[NonEmptyStr]]
    quality: KnowledgeValue[VocabularyId]
    model_training_permission: PermissionState
    evidence_ids: frozenset[StableId] = Field(default_factory=frozenset)

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

    @field_serializer("evidence_ids", when_used="json")
    def serialize_evidence_ids(self, value: frozenset[StableId]) -> list[StableId]:
        return sorted(value)


class DictionaryProfile(HistGermModel):
    lexical_coverage: KnowledgeValue[NonEmptyStr]
    source_language_stage_ids: KnowledgeValue[frozenset[VocabularyId]]
    target_language_ids: KnowledgeValue[frozenset[RegistryId]]
    search_interface: KnowledgeValue[AccessUrl]
    api: KnowledgeValue[ApiInterface]
    download_formats: KnowledgeValue[list[FormatDescription]]
    machine_readable_availability: AvailabilityState
    machine_readable_download_url: KnowledgeValue[HttpUrlValue]
    query_capabilities: KnowledgeValue[frozenset[NonEmptyStr]]
    lexical_features: LexicalFeatures
    citation_structure: KnowledgeValue[CitationStructure]
    corpus_occurrence_links: KnowledgeValue[CorpusOccurrenceLinks]
    transcription_convention: KnowledgeValue[NonEmptyStr]
    normalization_convention: KnowledgeValue[NonEmptyStr]
    supervision_suitability: KnowledgeValue[list[SuitabilityAssessment]]
    source_publication_ids: frozenset[StableId] = Field(default_factory=frozenset)
    source_edition: KnowledgeValue[NonEmptyStr]
    access_restrictions: KnowledgeValue[NonEmptyStr]
    reuse_restrictions: KnowledgeValue[NonEmptyStr]

    @field_validator(
        "source_language_stage_ids",
        "target_language_ids",
        "query_capabilities",
        mode="before",
    )
    @classmethod
    def accept_yaml_known_sets(cls, value: Any) -> Any:
        return _known_set(value)

    @field_validator("source_publication_ids", mode="before")
    @classmethod
    def accept_yaml_publication_ids(cls, value: Any) -> Any:
        return _as_frozenset(value)

    @field_validator("source_publication_ids")
    @classmethod
    def validate_publication_ids(
        cls, value: frozenset[StableId]
    ) -> frozenset[StableId]:
        invalid = sorted(item for item in value if not item.startswith("pub-"))
        if invalid:
            raise ValueError(
                "source_publication_ids entries must use the 'pub-' prefix; invalid: "
                + ", ".join(invalid)
            )
        return value

    @model_validator(mode="after")
    def validate_machine_readable_access(self) -> DictionaryProfile:
        has_download = isinstance(self.machine_readable_download_url, KnownValue)
        has_api = isinstance(self.api, KnownValue)
        if has_download and self.machine_readable_availability not in {
            "available",
            "partially_available",
            "request_only",
            "authentication_required",
        }:
            raise ValueError(
                "known machine-readable download URL requires an accessible "
                "machine_readable_availability state"
            )
        if self.machine_readable_availability == "available" and not (
            has_download or has_api
        ):
            raise ValueError(
                "available machine-readable data requires a known download URL or API"
            )
        return self

    @field_serializer("source_publication_ids", when_used="json")
    def serialize_publication_ids(self, value: frozenset[StableId]) -> list[StableId]:
        return sorted(value)


__all__ = [
    "CitationStructure",
    "CorpusOccurrenceLinks",
    "DictionaryProfile",
    "LexicalFeatures",
    "SuitabilityAssessment",
]
