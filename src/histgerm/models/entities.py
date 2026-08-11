"""Normalized intellectual-work, witness, and publication models."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import Field, field_serializer, field_validator

from histgerm.models.common import (
    DateRange,
    ExternalIdentifier,
    GeographicCoverage,
    HistGermModel,
    HttpUrlValue,
    KnowledgeValue,
    LocalizedName,
    NonEmptyStr,
    RegistryId,
    ResponsibleParty,
    StableId,
    VocabularyId,
)
from histgerm.models.provenance import ProvenancedRecord


def _as_frozenset(value: Any) -> Any:
    if isinstance(value, list):
        return frozenset(value)
    return value


def _require_prefix(identifier: StableId, prefix: str, entity: str) -> StableId:
    if not identifier.startswith(prefix):
        raise ValueError(f"{entity} id must use the {prefix!r} prefix")
    return identifier


class Authorship(HistGermModel):
    """One source-supported attribution of a work or document."""

    party: ResponsibleParty
    attribution_type: NonEmptyStr
    certainty: KnowledgeValue[VocabularyId]
    note: KnowledgeValue[NonEmptyStr]


class Work(ProvenancedRecord):
    """A reusable normalized intellectual work."""

    record_type: Literal["work"] = "work"
    id: StableId
    canonical_name: NonEmptyStr
    alternative_names: list[LocalizedName] = Field(default_factory=list)
    external_identifiers: list[ExternalIdentifier] = Field(default_factory=list)
    authorship: KnowledgeValue[list[Authorship]]
    chronology: KnowledgeValue[DateRange]
    language_stage_ids: KnowledgeValue[frozenset[VocabularyId]]
    genres: KnowledgeValue[frozenset[RegistryId]]
    description: KnowledgeValue[NonEmptyStr]

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: StableId) -> StableId:
        return _require_prefix(value, "work-", "work")

    @field_validator("language_stage_ids", "genres", mode="before")
    @classmethod
    def accept_yaml_sets(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("status") == "known":
            return {**value, "value": _as_frozenset(value.get("value"))}
        return value


class Witness(ProvenancedRecord):
    """A manuscript, print, edition, or other textual witness."""

    record_type: Literal["witness"] = "witness"
    id: StableId
    canonical_name: NonEmptyStr
    alternative_names: list[LocalizedName] = Field(default_factory=list)
    witness_type: VocabularyId
    work_ids: frozenset[StableId] = Field(default_factory=frozenset)
    external_identifiers: list[ExternalIdentifier] = Field(default_factory=list)
    chronology: KnowledgeValue[DateRange]
    geography: KnowledgeValue[GeographicCoverage]
    holding_institution: KnowledgeValue[ResponsibleParty]
    description: KnowledgeValue[NonEmptyStr]

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: StableId) -> StableId:
        return _require_prefix(value, "wit-", "witness")

    @field_validator("work_ids", mode="before")
    @classmethod
    def accept_yaml_sets(cls, value: Any) -> Any:
        return _as_frozenset(value)

    @field_validator("work_ids")
    @classmethod
    def validate_work_ids(cls, value: frozenset[StableId]) -> frozenset[StableId]:
        invalid = sorted(item for item in value if not item.startswith("work-"))
        if invalid:
            raise ValueError(
                "work_ids entries must use the 'work-' prefix; invalid: "
                + ", ".join(invalid)
            )
        return value

    @field_serializer("work_ids", when_used="json")
    def serialize_work_ids(self, value: frozenset[StableId]) -> list[StableId]:
        return sorted(value)


class Publication(ProvenancedRecord):
    """A normalized scholarly or project publication."""

    record_type: Literal["publication"] = "publication"
    id: StableId
    title: NonEmptyStr
    authors: KnowledgeValue[list[ResponsibleParty]]
    publication_date: KnowledgeValue[date]
    publication_type: NonEmptyStr
    identifiers: list[ExternalIdentifier] = Field(default_factory=list)
    url: KnowledgeValue[HttpUrlValue]
    citation: KnowledgeValue[NonEmptyStr]

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: StableId) -> StableId:
        return _require_prefix(value, "pub-", "publication")


__all__ = ["Authorship", "Publication", "Witness", "Work"]
