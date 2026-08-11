"""Immutable public inputs, snapshots, and errors for catalog discovery."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from histgerm.models.access import AvailabilityState, PermissionState
from histgerm.models.common import HistGermModel, SelectionScope

type MatchMode = Literal["any", "all"]
type SortDirection = Literal["ascending", "descending"]


class HistGermQueryError(ValueError):
    """Base class for query contract failures."""


class InvalidQueryError(HistGermQueryError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"invalid query: {reason}")


class InvalidIdentifierError(HistGermQueryError):
    def __init__(self, value: str) -> None:
        super().__init__(f"invalid stable identifier: {value!r}")


class UnknownIdentifierError(HistGermQueryError):
    def __init__(self, value: str) -> None:
        super().__init__(f"no catalog entity has identifier {value!r}")


class AmbiguousNameError(HistGermQueryError):
    def __init__(self, normalized: str, identifiers: tuple[str, ...]) -> None:
        super().__init__(
            f"normalized name {normalized!r} matches multiple resources: "
            + ", ".join(identifiers)
        )


class UnknownVocabularyValueError(HistGermQueryError):
    def __init__(self, dimension: str, values: frozenset[str]) -> None:
        super().__init__(
            f"unknown {dimension} vocabulary value(s): " + ", ".join(sorted(values))
        )


class UnsupportedSortError(HistGermQueryError):
    def __init__(self, query: str, field: str) -> None:
        super().__init__(f"unsupported sort field for {query}: {field!r}")


class DimensionFilter(HistGermModel):
    values: frozenset[str] = Field(min_length=1)
    match: MatchMode = "any"

    @field_validator("values", mode="before")
    @classmethod
    def copy_values(cls, value: object) -> object:
        if isinstance(value, (set, list, tuple)):
            return frozenset(value)
        return value


class QueryFilter(HistGermModel):
    identifier_or_name: str | None = None
    categories: DimensionFilter | None = None
    language_stages: DimensionFilter | None = None
    annotation_types: DimensionFilter | None = None
    formats: DimensionFilter | None = None
    licenses: DimensionFilter | None = None
    availability: DimensionFilter | None = None
    annotation_qualities: DimensionFilter | None = None


class SortSpec(HistGermModel):
    field: Literal["identifier", "canonical_name"] = "identifier"
    direction: SortDirection = "ascending"


class ResourceMatch(HistGermModel):
    resource_id: str
    canonical_name: str
    matched_on: Literal["identifier", "canonical_name", "alternative_name", "filters"]
    matched_name: str | None
    category_ids: tuple[str, ...]
    language_stage_ids: tuple[str, ...]


class DistributionMatch(HistGermModel):
    resource_id: str
    version_id: str
    distribution_id: str
    component_ids: tuple[str, ...]
    availability: AvailabilityState
    model_training: PermissionState
    download_status: AvailabilityState
    external_urls: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    warnings: tuple[str, ...]


class AnnotationMatch(HistGermModel):
    resource_id: str
    version_id: str
    annotation_id: str
    task_id: str
    quality_id: str
    scope: SelectionScope
    evidence_ids: tuple[str, ...]


class PermissionReview(HistGermModel):
    resource_id: str
    version_id: str
    distribution_id: str
    permission: PermissionState
    availability: AvailabilityState
    reasons: tuple[str, ...]
    evidence_ids: tuple[str, ...]


__all__ = [
    "AmbiguousNameError",
    "AnnotationMatch",
    "DimensionFilter",
    "DistributionMatch",
    "HistGermQueryError",
    "InvalidIdentifierError",
    "InvalidQueryError",
    "MatchMode",
    "PermissionReview",
    "QueryFilter",
    "ResourceMatch",
    "SortSpec",
    "UnknownIdentifierError",
    "UnknownVocabularyValueError",
    "UnsupportedSortError",
]
