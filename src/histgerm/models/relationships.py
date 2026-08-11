"""Strict catalog-level relationship and overlap models."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import Field, field_serializer, field_validator, model_validator

from histgerm.models.common import (
    EntityReference,
    HistGermModel,
    KnowledgeValue,
    KnownValue,
    NonEmptyStr,
    NotApplicableValue,
    SelectionScope,
    StableId,
    VocabularyId,
)
from histgerm.models.provenance import ProvenancedRecord

type RelationshipKind = Literal[
    "derived_from",
    "contains",
    "part_of",
    "overlaps",
    "duplicates",
    "supersedes",
    "same_work",
    "same_witness",
    "same_edition",
    "same_passage",
    "trained_on",
    "evaluated_on",
    "annotates",
]
type OverlapExtent = Literal["exact", "contains", "partial", "unknown"]
type MeasurementOrigin = Literal["reported", "locally_computed"]

_DIRECTIONAL_KINDS = frozenset(
    {
        "derived_from",
        "contains",
        "part_of",
        "supersedes",
        "trained_on",
        "evaluated_on",
        "annotates",
    }
)
_SYMMETRIC_KINDS = frozenset(
    {
        "overlaps",
        "duplicates",
        "same_work",
        "same_witness",
        "same_edition",
        "same_passage",
    }
)
_UNKNOWN_EXTENT_KINDS = frozenset(
    {
        "derived_from",
        "supersedes",
        "same_work",
        "same_witness",
        "same_edition",
        "same_passage",
        "trained_on",
        "evaluated_on",
        "annotates",
    }
)
_SAME_ENTITY_TYPES: dict[RelationshipKind, frozenset[str]] = {
    "same_work": frozenset({"work"}),
    "same_witness": frozenset({"witness"}),
    "same_edition": frozenset({"witness"}),
}
_SIZE_UNITS = frozenset(
    {
        "document",
        "sentence",
        "orthographic_word",
        "token",
        "character",
        "byte",
    }
)


def _as_frozenset(value: Any) -> Any:
    if isinstance(value, list):
        return frozenset(value)
    return value


class OverlapMeasurement(HistGermModel):
    """An explicit two-sided overlap quantity."""

    unit: VocabularyId
    value: int = Field(gt=0)
    counting_method: NonEmptyStr
    source_version_id: StableId
    target_version_id: StableId
    source_scope: SelectionScope
    target_scope: SelectionScope
    origin: MeasurementOrigin
    computed_on: KnowledgeValue[date]
    evidence_ids: frozenset[StableId] = Field(default_factory=frozenset)
    uncertainty_note: KnowledgeValue[NonEmptyStr]

    @field_validator("unit")
    @classmethod
    def validate_unit(cls, value: VocabularyId) -> VocabularyId:
        if value not in _SIZE_UNITS:
            raise ValueError(f"unsupported overlap size unit: {value!r}")
        return value

    @field_validator("source_version_id", "target_version_id")
    @classmethod
    def validate_version_id(cls, value: StableId) -> StableId:
        if not value.startswith("ver-"):
            raise ValueError("overlap version IDs must use the 'ver-' prefix")
        return value

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
    def validate_origin(self) -> OverlapMeasurement:
        if self.origin == "reported":
            if not isinstance(self.computed_on, NotApplicableValue):
                raise ValueError(
                    "reported overlap requires computed_on to be not_applicable"
                )
            if not self.evidence_ids:
                raise ValueError("reported overlap requires at least one evidence ID")
        elif not isinstance(self.computed_on, KnownValue):
            raise ValueError(
                "locally_computed overlap requires a known computation date"
            )
        return self

    @field_serializer("evidence_ids", when_used="json")
    def serialize_evidence_ids(self, value: frozenset[StableId]) -> list[StableId]:
        return sorted(value)


class Relationship(ProvenancedRecord):
    """A validated relationship between two distinct catalog entities."""

    record_type: Literal["relationship"] = "relationship"
    id: StableId
    source: EntityReference
    target: EntityReference
    kind: RelationshipKind
    directional: bool
    source_scope: SelectionScope
    target_scope: SelectionScope
    overlap_extent: OverlapExtent
    overlap_measurement: KnowledgeValue[OverlapMeasurement]
    certainty: KnowledgeValue[VocabularyId]
    note: KnowledgeValue[NonEmptyStr]
    duplicate_group_id: KnowledgeValue[NonEmptyStr]
    canonical_scope: KnowledgeValue[SelectionScope]

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: StableId) -> StableId:
        if not value.startswith("rel-"):
            raise ValueError("relationship id must use the 'rel-' prefix")
        return value

    @model_validator(mode="after")
    def validate_relationship(self) -> Relationship:
        if self.source == self.target:
            raise ValueError("relationship source and target must differ")

        expected_direction = self.kind in _DIRECTIONAL_KINDS
        if self.directional != expected_direction:
            label = "directional" if expected_direction else "symmetric"
            raise ValueError(f"{self.kind!r} relationships must be {label}")

        if self.kind in _SYMMETRIC_KINDS and self.source.id > self.target.id:
            raise ValueError("symmetric relationships require source.id < target.id")

        allowed_types = _SAME_ENTITY_TYPES.get(self.kind)
        if allowed_types is not None and (
            self.source.entity_type not in allowed_types
            or self.target.entity_type not in allowed_types
        ):
            expected = ", ".join(sorted(allowed_types))
            raise ValueError(
                f"{self.kind!r} endpoints must both have entity type {expected}"
            )

        if self.kind in {"contains", "part_of"}:
            if self.overlap_extent != "contains":
                raise ValueError(f"{self.kind!r} requires overlap_extent 'contains'")
        elif self.kind == "duplicates":
            if self.overlap_extent != "exact":
                raise ValueError("'duplicates' requires overlap_extent 'exact'")
        elif self.kind in _UNKNOWN_EXTENT_KINDS and self.overlap_extent != "unknown":
            raise ValueError(f"{self.kind!r} requires overlap_extent 'unknown'")

        if self.kind == "duplicates":
            if not isinstance(self.duplicate_group_id, KnownValue):
                raise ValueError("'duplicates' requires a known duplicate_group_id")
        elif not isinstance(self.duplicate_group_id, NotApplicableValue):
            raise ValueError(
                "duplicate_group_id must be not_applicable unless kind is 'duplicates'"
            )

        canonical_allowed = self.kind == "duplicates" or (
            self.kind == "overlaps" and self.overlap_extent == "exact"
        )
        if not canonical_allowed:
            if not isinstance(self.canonical_scope, NotApplicableValue):
                raise ValueError(
                    "canonical_scope must be not_applicable unless the "
                    "relationship is duplicates or exact overlap"
                )
        elif (
            isinstance(self.canonical_scope, KnownValue)
            and self.canonical_scope.value != self.source_scope
            and self.canonical_scope.value != self.target_scope
        ):
            raise ValueError(
                "known canonical_scope must equal source_scope or target_scope"
            )

        if isinstance(self.overlap_measurement, KnownValue):
            self._validate_measurement(self.overlap_measurement.value)
        elif not isinstance(
            self.overlap_measurement, NotApplicableValue
        ) and self.overlap_extent not in {"partial", "unknown"}:
            raise ValueError(
                "unquantified exact/contained overlap must be "
                "not_applicable, not unknown"
            )
        return self

    def _validate_measurement(self, measurement: OverlapMeasurement) -> None:
        if self.kind not in {"overlaps", "duplicates"}:
            raise ValueError(
                "overlap_measurement is supported only for overlaps or duplicates"
            )
        if self.overlap_extent not in {"partial", "exact"}:
            raise ValueError("quantified overlap requires extent 'partial' or 'exact'")
        if measurement.source_scope != self.source_scope:
            raise ValueError(
                "overlap measurement source_scope must equal relationship source_scope"
            )
        if measurement.target_scope != self.target_scope:
            raise ValueError(
                "overlap measurement target_scope must equal relationship target_scope"
            )
        if (
            self.source.entity_type == "version"
            and measurement.source_version_id != self.source.id
        ):
            raise ValueError("source_version_id must match a version source endpoint")
        if (
            self.target.entity_type == "version"
            and measurement.target_version_id != self.target.id
        ):
            raise ValueError("target_version_id must match a version target endpoint")
        if (
            self.source_scope.version_ids
            and measurement.source_version_id not in self.source_scope.version_ids
        ):
            raise ValueError("source_version_id must occur in source_scope.version_ids")
        if (
            self.target_scope.version_ids
            and measurement.target_version_id not in self.target_scope.version_ids
        ):
            raise ValueError("target_version_id must occur in target_scope.version_ids")


__all__ = ["OverlapMeasurement", "Relationship"]
