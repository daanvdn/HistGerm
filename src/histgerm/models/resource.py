"""Resource identity, version hierarchy, and category-profile integration."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, Literal

from pydantic import Field, field_serializer, field_validator, model_validator

from histgerm.models.access import Distribution
from histgerm.models.annotation import AnnotationLayer
from histgerm.models.common import (
    DateRange,
    ExtensionData,
    GeographicCoverage,
    HistGermModel,
    HttpUrlValue,
    KnowledgeValue,
    KnownValue,
    LocalizedName,
    NonEmptyStr,
    NotApplicableValue,
    ResponsibleParty,
    StableId,
    VocabularyId,
)
from histgerm.models.corpus import (
    CorpusComponent,
    CorpusProfile,
    Document,
    SizeMeasurement,
)
from histgerm.models.dictionary import DictionaryProfile
from histgerm.models.provenance import ProvenancedRecord
from histgerm.models.tool import ToolProfile

_CATEGORIES = frozenset(
    {
        "corpus",
        "pos_tagger",
        "morphological_tagger",
        "lemmatizer",
        "syntactic_parser",
        "dictionary",
        "lexicon",
    }
)
_TOOL_CATEGORIES = frozenset(
    {"pos_tagger", "morphological_tagger", "lemmatizer", "syntactic_parser"}
)


def _as_frozenset(value: Any) -> Any:
    if isinstance(value, list):
        return frozenset(value)
    return value


def _known_set(value: Any) -> Any:
    if isinstance(value, Mapping) and value.get("status") == "known":
        return {**value, "value": _as_frozenset(value.get("value"))}
    return value


def _validate_scope(
    scope: Any,
    *,
    resource_id: StableId,
    version_id: StableId,
    component_ids: set[StableId],
    document_ids: set[StableId],
    annotation_ids: set[StableId],
    label: str,
) -> None:
    if scope.resource_ids and scope.resource_ids != frozenset({resource_id}):
        raise ValueError(f"{label} resource_ids must identify the containing resource")
    if scope.version_ids and scope.version_ids != frozenset({version_id}):
        raise ValueError(f"{label} version_ids must identify the containing version")
    checks = (
        ("component_ids", scope.component_ids, component_ids),
        ("document_ids", scope.document_ids, document_ids),
        ("annotation_ids", scope.annotation_ids, annotation_ids),
    )
    for field, selected, available in checks:
        missing = sorted(set(selected) - available)
        if missing:
            raise ValueError(
                f"{label} {field} must resolve within the containing version; "
                f"unknown: {', '.join(missing)}"
            )


class ResourceVersion(HistGermModel):
    """A stable, locally coherent state of a resource."""

    id: StableId
    version_label: KnowledgeValue[NonEmptyStr]
    release_date: KnowledgeValue[date]
    superseded: KnowledgeValue[bool]
    changelog_url: KnowledgeValue[HttpUrlValue]
    language_stage_ids: KnowledgeValue[frozenset[VocabularyId]]
    chronology: KnowledgeValue[DateRange]
    components: list[CorpusComponent] = Field(default_factory=list)
    documents: list[Document] = Field(default_factory=list)
    distributions: list[Distribution] = Field(default_factory=list)
    annotations: list[AnnotationLayer] = Field(default_factory=list)
    size_measurements: list[SizeMeasurement] = Field(default_factory=list)
    extensions: ExtensionData = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: StableId) -> StableId:
        if not value.startswith("ver-"):
            raise ValueError("version id must use the 'ver-' prefix")
        return value

    @field_validator("language_stage_ids", mode="before")
    @classmethod
    def accept_yaml_stages(cls, value: Any) -> Any:
        return _known_set(value)

    @model_validator(mode="after")
    def validate_local_hierarchy(self) -> ResourceVersion:
        collections = {
            "component": [item.id for item in self.components],
            "document": [item.id for item in self.documents],
            "distribution": [item.id for item in self.distributions],
            "annotation": [item.id for item in self.annotations],
        }
        for label, identifiers in collections.items():
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"{label} IDs must be unique within a version")

        components = {item.id: item for item in self.components}
        documents = {item.id: item for item in self.documents}
        for component in self.components:
            if isinstance(component.parent_component_id, KnownValue):
                parent = component.parent_component_id.value
                if parent not in components:
                    raise ValueError(
                        "parent_component_id must resolve within the containing version"
                    )
            missing_documents = sorted(set(component.document_ids) - documents.keys())
            if missing_documents:
                raise ValueError(
                    "component document_ids must resolve within the containing "
                    "version; unknown: " + ", ".join(missing_documents)
                )
        for component_id in components:
            seen: set[StableId] = set()
            current = component_id
            while current in components:
                if current in seen:
                    raise ValueError("component parent hierarchy must be acyclic")
                seen.add(current)
                parent_value = components[current].parent_component_id
                if not isinstance(parent_value, KnownValue):
                    break
                current = parent_value.value

        for document in self.documents:
            missing_components = sorted(set(document.component_ids) - components.keys())
            if missing_components:
                raise ValueError(
                    "document component_ids must resolve within the containing "
                    "version; unknown: " + ", ".join(missing_components)
                )
            for component_id in document.component_ids:
                if document.id not in components[component_id].document_ids:
                    raise ValueError(
                        "component document_ids and document component_ids must "
                        "be reciprocal"
                    )
        for component in self.components:
            for document_id in component.document_ids:
                if component.id not in documents[document_id].component_ids:
                    raise ValueError(
                        "component document_ids and document component_ids must "
                        "be reciprocal"
                    )

        for measurement in [
            *self.size_measurements,
            *(
                item
                for component in self.components
                for item in component.size_measurements
            ),
            *(
                item
                for document in self.documents
                for item in document.size_measurements
            ),
            *(
                item
                for annotation in self.annotations
                for item in annotation.coverage_measurements
            ),
        ]:
            if measurement.version_id != self.id:
                raise ValueError(
                    "size measurement version_id must match the containing version"
                )
        return self


class Resource(ProvenancedRecord):
    record_type: Literal["resource"] = "resource"
    id: StableId
    canonical_name: NonEmptyStr
    alternative_names: list[LocalizedName] = Field(default_factory=list)
    categories: frozenset[VocabularyId] = Field(min_length=1)
    description: KnowledgeValue[NonEmptyStr]
    responsible_parties: KnowledgeValue[list[ResponsibleParty]]
    homepage_url: KnowledgeValue[HttpUrlValue]
    repository_url: KnowledgeValue[HttpUrlValue]
    language_stage_ids: KnowledgeValue[frozenset[VocabularyId]]
    chronology: KnowledgeValue[DateRange]
    geography: KnowledgeValue[GeographicCoverage]
    maintenance_status: KnowledgeValue[VocabularyId]
    publication_ids: frozenset[StableId] = Field(default_factory=frozenset)
    versions: list[ResourceVersion] = Field(default_factory=list)
    corpus: KnowledgeValue[CorpusProfile]
    tool: KnowledgeValue[ToolProfile]
    dictionary: KnowledgeValue[DictionaryProfile]
    record_reviewed_on: date

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: StableId) -> StableId:
        if not value.startswith("res-"):
            raise ValueError("resource id must use the 'res-' prefix")
        return value

    @field_validator("categories", "publication_ids", mode="before")
    @classmethod
    def accept_yaml_sets(cls, value: Any) -> Any:
        return _as_frozenset(value)

    @field_validator("language_stage_ids", mode="before")
    @classmethod
    def accept_yaml_stages(cls, value: Any) -> Any:
        return _known_set(value)

    @field_validator("categories")
    @classmethod
    def validate_categories(
        cls, value: frozenset[VocabularyId]
    ) -> frozenset[VocabularyId]:
        unsupported = sorted(value - _CATEGORIES)
        if unsupported:
            raise ValueError(
                "unsupported resource categories: " + ", ".join(unsupported)
            )
        return value

    @field_validator("publication_ids")
    @classmethod
    def validate_publication_ids(
        cls, value: frozenset[StableId]
    ) -> frozenset[StableId]:
        invalid = sorted(item for item in value if not item.startswith("pub-"))
        if invalid:
            raise ValueError(
                "publication_ids entries must use the 'pub-' prefix; invalid: "
                + ", ".join(invalid)
            )
        return value

    @model_validator(mode="after")
    def validate_resource(self) -> Resource:
        expected = {
            "corpus": "corpus" in self.categories,
            "tool": bool(self.categories & _TOOL_CATEGORIES),
            "dictionary": bool(self.categories & {"dictionary", "lexicon"}),
        }
        for field, required in expected.items():
            value = getattr(self, field)
            if required and not isinstance(value, KnownValue):
                raise ValueError(f"resource categories require a known {field} profile")
            if not required and not isinstance(value, NotApplicableValue):
                raise ValueError(
                    f"{field} profile must be not_applicable for these categories"
                )

        version_ids = [version.id for version in self.versions]
        if len(version_ids) != len(set(version_ids)):
            raise ValueError("version IDs must be unique within a resource")

        for version in self.versions:
            component_ids = {item.id for item in version.components}
            document_ids = {item.id for item in version.documents}
            annotation_ids = {item.id for item in version.annotations}
            for distribution in version.distributions:
                _validate_scope(
                    distribution.scope,
                    resource_id=self.id,
                    version_id=version.id,
                    component_ids=component_ids,
                    document_ids=document_ids,
                    annotation_ids=annotation_ids,
                    label=f"distribution {distribution.id} scope",
                )
            for annotation in version.annotations:
                _validate_scope(
                    annotation.scope,
                    resource_id=self.id,
                    version_id=version.id,
                    component_ids=component_ids,
                    document_ids=document_ids,
                    annotation_ids=annotation_ids,
                    label=f"annotation {annotation.id} scope",
                )
            for measurement in [
                *version.size_measurements,
                *(
                    item
                    for component in version.components
                    for item in component.size_measurements
                ),
                *(
                    item
                    for document in version.documents
                    for item in document.size_measurements
                ),
                *(
                    item
                    for annotation in version.annotations
                    for item in annotation.coverage_measurements
                ),
            ]:
                _validate_scope(
                    measurement.scope,
                    resource_id=self.id,
                    version_id=version.id,
                    component_ids=component_ids,
                    document_ids=document_ids,
                    annotation_ids=annotation_ids,
                    label="size measurement scope",
                )

        if isinstance(self.corpus, KnownValue) and isinstance(
            self.corpus.value.text_layers, KnownValue
        ):
            for layer in self.corpus.value.text_layers.value:
                matching_versions = [
                    version
                    for version in self.versions
                    if not layer.scope.version_ids
                    or version.id in layer.scope.version_ids
                ]
                if not matching_versions:
                    raise ValueError("text layer scope must identify a local version")
                for version in matching_versions:
                    _validate_scope(
                        layer.scope,
                        resource_id=self.id,
                        version_id=version.id,
                        component_ids={item.id for item in version.components},
                        document_ids={item.id for item in version.documents},
                        annotation_ids={item.id for item in version.annotations},
                        label=f"text layer {layer.id} scope",
                    )
        if isinstance(self.dictionary, KnownValue) and isinstance(
            self.dictionary.value.supervision_suitability, KnownValue
        ):
            for assessment in self.dictionary.value.supervision_suitability.value:
                assessment_scope = assessment.scope
                if assessment_scope.resource_ids and assessment_scope.resource_ids != {
                    self.id
                }:
                    raise ValueError(
                        "dictionary suitability scope must identify the containing "
                        "resource"
                    )
                matching_versions = [
                    version
                    for version in self.versions
                    if not assessment_scope.version_ids
                    or version.id in assessment_scope.version_ids
                ]
                if assessment_scope.version_ids and not matching_versions:
                    raise ValueError(
                        "dictionary suitability scope must identify a local version"
                    )
                for version in matching_versions:
                    _validate_scope(
                        assessment_scope,
                        resource_id=self.id,
                        version_id=version.id,
                        component_ids={item.id for item in version.components},
                        document_ids={item.id for item in version.documents},
                        annotation_ids={item.id for item in version.annotations},
                        label="dictionary suitability scope",
                    )
        return self

    @field_serializer("categories", "publication_ids", when_used="json")
    def serialize_sets(self, value: frozenset[Any]) -> list[Any]:
        return sorted(value)


__all__ = ["Resource", "ResourceVersion"]
