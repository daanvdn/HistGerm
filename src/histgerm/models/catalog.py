"""Root catalog and versioned vocabulary/registry models."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import Field, RootModel, field_serializer, field_validator

from histgerm.models.common import (
    ExtensionData,
    HistGermModel,
    KnowledgeValue,
    NonEmptyStr,
    RegistryId,
    VocabularyId,
)
from histgerm.models.entities import Publication, Witness, Work
from histgerm.models.provenance import ProvenancedRecord
from histgerm.models.relationships import Relationship
from histgerm.models.resource import Resource


class VocabularyDefinition(HistGermModel):
    """One closed vocabulary release used by a catalog."""

    schema_version: NonEmptyStr
    ids: frozenset[VocabularyId] = Field(min_length=1)

    @field_validator("ids", mode="before")
    @classmethod
    def accept_yaml_ids(cls, value: object) -> object:
        if isinstance(value, list):
            return frozenset(value)
        return value

    @field_serializer("ids", when_used="json")
    def serialize_ids(self, value: frozenset[VocabularyId]) -> list[VocabularyId]:
        return sorted(value)


class VocabularyRegistry(RootModel[dict[VocabularyId, VocabularyDefinition]]):
    """Closed vocabularies keyed by their singular registry name."""

    model_config = {"frozen": True, "strict": True}


class RegistryTerm(ProvenancedRecord):
    """One normalized term in an explicitly open registry."""

    id: RegistryId
    canonical_label: NonEmptyStr
    aliases: list[NonEmptyStr] = Field(default_factory=list)
    description: KnowledgeValue[NonEmptyStr]


class OpenRegistryDefinition(HistGermModel):
    """One versioned open registry."""

    schema_version: NonEmptyStr
    terms: list[RegistryTerm] = Field(default_factory=list)


class OpenRegistryRegistry(RootModel[dict[VocabularyId, OpenRegistryDefinition]]):
    """Open registries keyed by their plural catalog name."""

    model_config = {"frozen": True, "strict": True}


type InventoryRecord = Annotated[
    Resource | Work | Witness | Publication | Relationship,
    Field(discriminator="record_type"),
]


class Catalog(HistGermModel):
    """Validated in-memory root for one inventory release."""

    record_type: Literal["catalog"] = "catalog"
    schema_version: NonEmptyStr
    inventory_release: NonEmptyStr
    generated_on: date
    vocabularies: VocabularyRegistry
    registries: OpenRegistryRegistry
    resources: list[Resource] = Field(default_factory=list)
    works: list[Work] = Field(default_factory=list)
    witnesses: list[Witness] = Field(default_factory=list)
    publications: list[Publication] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    notes: KnowledgeValue[list[NonEmptyStr]]
    extensions: ExtensionData = Field(default_factory=dict)


__all__ = [
    "Catalog",
    "InventoryRecord",
    "OpenRegistryRegistry",
    "RegistryTerm",
    "VocabularyRegistry",
]
