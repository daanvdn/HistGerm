"""Sparse, deterministic coverage matrices over explicit catalog metadata."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from decimal import Decimal
from itertools import product
from typing import Literal, cast

from pydantic import Field, field_validator

from histgerm.models.annotation import AnnotationLayer
from histgerm.models.catalog import Catalog
from histgerm.models.common import (
    DateRange,
    GeographicCoverage,
    HistGermModel,
    KnowledgeValue,
    KnownValue,
    SelectionScope,
)
from histgerm.models.corpus import CorpusComponent, Document, SizeMeasurement
from histgerm.models.resource import Resource, ResourceVersion
from histgerm.query.filters import (
    HistGermQueryError,
    InvalidQueryError,
    QueryFilter,
)

type CoverageDimension = Literal[
    "language_stage",
    "period",
    "region",
    "dialect",
    "genre",
    "work",
    "witness",
    "annotation_type",
]
type KnowledgeStatus = Literal[
    "known", "unknown", "not_applicable", "not_publicly_available"
]
type SizeUnit = Literal[
    "document", "sentence", "orthographic_word", "token", "character", "byte"
]
type Exactness = Literal["exact", "non_exact", "not_computable"]

_DIMENSIONS = frozenset(
    {
        "language_stage",
        "period",
        "region",
        "dialect",
        "genre",
        "work",
        "witness",
        "annotation_type",
    }
)
_SIZE_UNITS = frozenset(
    {"document", "sentence", "orthographic_word", "token", "character", "byte"}
)
_STATUS_ORDER = {
    "known": 0,
    "unknown": 1,
    "not_applicable": 2,
    "not_publicly_available": 3,
}


class UnsupportedCoverageDimensionError(HistGermQueryError):
    def __init__(self, dimensions: Iterable[str]) -> None:
        values = ", ".join(sorted(set(dimensions)))
        super().__init__(f"unsupported coverage dimension(s): {values}")


class CoverageRequest(HistGermModel):
    dimensions: tuple[str, ...]
    selection: QueryFilter = Field(default_factory=QueryFilter)
    unit: str | None = None
    counting_method: str | None = None
    include_unknown_buckets: bool = True

    @field_validator("dimensions")
    @classmethod
    def validate_dimensions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unsupported = set(value) - _DIMENSIONS
        if unsupported:
            raise UnsupportedCoverageDimensionError(unsupported)
        if not value or len(value) > 8:
            raise InvalidQueryError(
                "coverage dimensions must contain between one and eight values"
            )
        if len(value) != len(set(value)):
            raise InvalidQueryError("coverage dimensions must be unique")
        return value

    @field_validator("unit")
    @classmethod
    def validate_unit(cls, value: str | None) -> str | None:
        if value is not None and value not in _SIZE_UNITS:
            raise InvalidQueryError(f"unsupported size unit: {value!r}")
        return value

    @field_validator("counting_method")
    @classmethod
    def normalize_counting_method(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = _normalize_method(value)
        if not normalized:
            raise InvalidQueryError("counting_method must not be empty")
        return normalized


class DimensionValue(HistGermModel):
    dimension: str
    value_id: str | None
    label: str
    knowledge_status: KnowledgeStatus
    certainty_id: str | None = None
    range_start: int | None = None
    range_end: int | None = None


class MeasurementRef(HistGermModel):
    measurement_path: str
    unit: str
    value: Decimal
    counting_method: str
    origin: Literal["reported", "locally_computed"]
    scope: SelectionScope
    uncertainty_note: str | None
    evidence_ids: tuple[str, ...]


class CompatibleTotal(HistGermModel):
    unit: str
    counting_method: str
    measurement_paths: tuple[str, ...]
    total: Decimal | None
    exactness: Exactness
    unresolved_relationship_ids: tuple[str, ...] = ()


class CoverageCell(HistGermModel):
    coordinates: tuple[DimensionValue, ...]
    resource_ids: tuple[str, ...]
    version_ids: tuple[str, ...]
    component_ids: tuple[str, ...]
    document_ids: tuple[str, ...]
    annotation_ids: tuple[str, ...]
    measurements: tuple[MeasurementRef, ...]
    compatible_totals: tuple[CompatibleTotal, ...]
    warnings: tuple[str, ...]


class CoverageMatrix(HistGermModel):
    dimensions: tuple[str, ...]
    cells: tuple[CoverageCell, ...]
    omitted_unknown_count: int
    warnings: tuple[str, ...]


class _Contribution(HistGermModel):
    resource_id: str
    version_id: str | None = None
    component_ids: tuple[str, ...] = ()
    document_ids: tuple[str, ...] = ()
    annotation_ids: tuple[str, ...] = ()
    values: dict[str, tuple[DimensionValue, ...]]


class _Measurement(HistGermModel):
    resource_id: str
    version_id: str
    path: str
    value: SizeMeasurement


class _CellAccumulator:
    def __init__(self, coordinates: tuple[DimensionValue, ...]) -> None:
        self.coordinates = coordinates
        self.resource_ids: set[str] = set()
        self.version_ids: set[str] = set()
        self.component_ids: set[str] = set()
        self.document_ids: set[str] = set()
        self.annotation_ids: set[str] = set()

    def add(self, contribution: _Contribution) -> None:
        self.resource_ids.add(contribution.resource_id)
        if contribution.version_id is not None:
            self.version_ids.add(contribution.version_id)
        self.component_ids.update(contribution.component_ids)
        self.document_ids.update(contribution.document_ids)
        self.annotation_ids.update(contribution.annotation_ids)


def coverage_matrix(catalog: Catalog, request: CoverageRequest) -> CoverageMatrix:
    """Return a read-only sparse matrix without mutating or inferring catalog data."""
    from histgerm.query.catalog import CatalogQuery

    dimensions = cast(tuple[CoverageDimension, ...], request.dimensions)
    selected_ids = {
        match.resource_id
        for match in CatalogQuery(catalog=catalog).resources(request.selection)
    }
    contributions = _contributions(catalog, dimensions, selected_ids)
    cells: dict[tuple[tuple[object, ...], ...], _CellAccumulator] = {}
    omitted = 0

    for contribution in contributions:
        coordinate_sets: list[tuple[DimensionValue, ...]] = []
        skip = False
        for dimension in dimensions:
            values = contribution.values.get(dimension, ())
            if not values:
                skip = True
                break
            if not request.include_unknown_buckets:
                known = tuple(
                    value for value in values if value.knowledge_status == "known"
                )
                if not known:
                    omitted += 1
                    skip = True
                    break
                values = known
            coordinate_sets.append(values)
        if skip:
            continue
        for coordinates in product(*coordinate_sets):
            key = tuple(_dimension_key(value) for value in coordinates)
            accumulator = cells.setdefault(key, _CellAccumulator(coordinates))
            accumulator.add(contribution)

    measurements, measurement_warnings = _measurements(catalog, request, selected_ids)
    result_cells = tuple(
        _finish_cell(cells[key], measurements) for key in sorted(cells)
    )
    return CoverageMatrix(
        dimensions=request.dimensions,
        cells=result_cells,
        omitted_unknown_count=omitted,
        warnings=measurement_warnings,
    )


def _contributions(
    catalog: Catalog,
    dimensions: tuple[CoverageDimension, ...],
    selected_ids: set[str],
) -> tuple[_Contribution, ...]:
    result: list[_Contribution] = []
    annotation_requested = "annotation_type" in dimensions
    for resource in sorted(catalog.resources, key=lambda item: item.id):
        if resource.id not in selected_ids:
            continue
        result.extend(_resource_contributions(resource, dimensions))
        if annotation_requested:
            result.extend(_annotation_only_contributions(resource))
    return tuple(result)


def _resource_contributions(
    resource: Resource, dimensions: tuple[CoverageDimension, ...]
) -> list[_Contribution]:
    result = [
        _make_contribution(
            resource,
            resource_id=resource.id,
            dimensions=dimensions,
        )
    ]
    for version in sorted(resource.versions, key=lambda item: item.id):
        annotations = sorted(version.annotations, key=lambda item: item.id)
        result.append(
            _make_contribution(
                version,
                resource_id=resource.id,
                version_id=version.id,
                dimensions=dimensions,
                annotations=annotations,
            )
        )
        for component in sorted(version.components, key=lambda item: item.id):
            result.append(
                _make_contribution(
                    component,
                    resource_id=resource.id,
                    version_id=version.id,
                    component_ids=(component.id,),
                    dimensions=dimensions,
                    annotations=annotations,
                )
            )
        for document in sorted(version.documents, key=lambda item: item.id):
            result.append(
                _make_contribution(
                    document,
                    resource_id=resource.id,
                    version_id=version.id,
                    component_ids=tuple(sorted(document.component_ids)),
                    document_ids=(document.id,),
                    dimensions=dimensions,
                    annotations=annotations,
                )
            )
    return result


def _make_contribution(
    record: Resource | ResourceVersion | CorpusComponent | Document,
    *,
    resource_id: str,
    dimensions: tuple[CoverageDimension, ...],
    version_id: str | None = None,
    component_ids: tuple[str, ...] = (),
    document_ids: tuple[str, ...] = (),
    annotations: Sequence[AnnotationLayer] = (),
) -> _Contribution:
    values: dict[str, tuple[DimensionValue, ...]] = {}
    for dimension in dimensions:
        if dimension == "annotation_type":
            matching = tuple(
                annotation
                for annotation in annotations
                if _annotation_intersects(
                    annotation,
                    resource_id,
                    version_id,
                    component_ids,
                    document_ids,
                )
            )
            values[dimension] = tuple(
                _known_value(dimension, annotation.task) for annotation in matching
            )
        else:
            values[dimension] = _record_dimension(record, dimension)
    matching_ids = tuple(
        annotation.id
        for annotation in annotations
        if _annotation_intersects(
            annotation, resource_id, version_id, component_ids, document_ids
        )
    )
    return _Contribution(
        resource_id=resource_id,
        version_id=version_id,
        component_ids=component_ids,
        document_ids=document_ids,
        annotation_ids=matching_ids if "annotation_type" in dimensions else (),
        values=values,
    )


def _annotation_only_contributions(resource: Resource) -> list[_Contribution]:
    result: list[_Contribution] = []
    for version in sorted(resource.versions, key=lambda item: item.id):
        for annotation in sorted(version.annotations, key=lambda item: item.id):
            result.append(
                _Contribution(
                    resource_id=resource.id,
                    version_id=version.id,
                    component_ids=tuple(sorted(annotation.scope.component_ids)),
                    document_ids=tuple(sorted(annotation.scope.document_ids)),
                    annotation_ids=(annotation.id,),
                    values={
                        "annotation_type": (
                            _known_value("annotation_type", annotation.task),
                        )
                    },
                )
            )
    return result


def _record_dimension(
    record: Resource | ResourceVersion | CorpusComponent | Document,
    dimension: CoverageDimension,
) -> tuple[DimensionValue, ...]:
    if dimension == "language_stage":
        return _knowledge_ids(dimension, record.language_stage_ids)
    if dimension == "period":
        return (_period_value(record.chronology),)
    if dimension in {"region", "dialect"}:
        if not hasattr(record, "geography"):
            return ()
        return _geography_values(
            cast(Literal["region", "dialect"], dimension), record.geography
        )
    if dimension == "genre":
        if not hasattr(record, "genres"):
            return ()
        return _knowledge_ids(dimension, record.genres)
    if dimension == "work" and isinstance(record, Document):
        return tuple(_known_value(dimension, item) for item in sorted(record.work_ids))
    if dimension == "witness" and isinstance(record, Document):
        ids = record.witness_ids | record.edition_witness_ids
        return tuple(_known_value(dimension, item) for item in sorted(ids))
    return ()


def _knowledge_ids(
    dimension: CoverageDimension, value: KnowledgeValue[frozenset[str]]
) -> tuple[DimensionValue, ...]:
    if isinstance(value, KnownValue):
        return tuple(_known_value(dimension, item) for item in sorted(value.value))
    return (_sentinel_value(dimension, value.status),)


def _period_value(value: KnowledgeValue[DateRange]) -> DimensionValue:
    if not isinstance(value, KnownValue):
        return _sentinel_value("period", value.status)
    period = value.value
    start = (
        period.earliest_year.value
        if isinstance(period.earliest_year, KnownValue)
        else None
    )
    end = (
        period.latest_year.value if isinstance(period.latest_year, KnownValue) else None
    )
    label = (
        period.label.value
        if isinstance(period.label, KnownValue)
        else _range_label(start, end)
    )
    certainty = (
        period.certainty.value if isinstance(period.certainty, KnownValue) else None
    )
    dating_method = (
        period.dating_method.value
        if isinstance(period.dating_method, KnownValue)
        else period.dating_method.status
    )
    return DimensionValue(
        dimension="period",
        value_id=dating_method,
        label=label,
        knowledge_status="known",
        certainty_id=certainty,
        range_start=start,
        range_end=end,
    )


def _geography_values(
    dimension: Literal["region", "dialect"],
    value: KnowledgeValue[GeographicCoverage],
) -> tuple[DimensionValue, ...]:
    if not isinstance(value, KnownValue):
        return (_sentinel_value(dimension, value.status),)
    geography = value.value
    ids = geography.region_ids if dimension == "region" else geography.dialect_ids
    certainty = (
        geography.certainty.value
        if isinstance(geography.certainty, KnownValue)
        else None
    )
    if not isinstance(ids, KnownValue):
        return (_sentinel_value(dimension, ids.status),)
    return tuple(
        _known_value(dimension, item, certainty=certainty) for item in sorted(ids.value)
    )


def _known_value(
    dimension: str, value: str, *, certainty: str | None = None
) -> DimensionValue:
    return DimensionValue(
        dimension=dimension,
        value_id=value,
        label=value,
        knowledge_status="known",
        certainty_id=certainty,
    )


def _sentinel_value(dimension: str, status: KnowledgeStatus) -> DimensionValue:
    labels = {
        "unknown": "[unknown]",
        "not_applicable": "[not applicable]",
        "not_publicly_available": "[not publicly available]",
    }
    return DimensionValue(
        dimension=dimension,
        value_id=None,
        label=labels[status],
        knowledge_status=status,
    )


def _range_label(start: int | None, end: int | None) -> str:
    if start is not None and end is not None:
        return str(start) if start == end else f"{start}–{end}"
    if start is not None:
        return f"{start}–?"
    if end is not None:
        return f"?–{end}"
    return "[known period]"


def _annotation_intersects(
    annotation: AnnotationLayer,
    resource_id: str,
    version_id: str | None,
    component_ids: tuple[str, ...],
    document_ids: tuple[str, ...],
) -> bool:
    scope = annotation.scope
    if scope.resource_ids and resource_id not in scope.resource_ids:
        return False
    if scope.version_ids and (
        version_id is None or version_id not in scope.version_ids
    ):
        return False
    if scope.component_ids and (
        not component_ids or not scope.component_ids.intersection(component_ids)
    ):
        return False
    return not scope.document_ids or bool(scope.document_ids.intersection(document_ids))


def _measurements(
    catalog: Catalog, request: CoverageRequest, selected_ids: set[str]
) -> tuple[tuple[_Measurement, ...], tuple[str, ...]]:
    result: list[_Measurement] = []
    warnings: set[str] = set()
    for resource_index, resource in enumerate(catalog.resources):
        if resource.id not in selected_ids:
            continue
        for version_index, version in enumerate(resource.versions):
            prefix = f"/resources/{resource_index}/versions/{version_index}"
            candidates: list[tuple[str, SizeMeasurement]] = [
                (f"{prefix}/size_measurements/{index}", measurement)
                for index, measurement in enumerate(version.size_measurements)
            ]
            candidates.extend(
                (
                    f"{prefix}/components/{component_index}/size_measurements/{index}",
                    measurement,
                )
                for component_index, component in enumerate(version.components)
                for index, measurement in enumerate(component.size_measurements)
            )
            candidates.extend(
                (
                    f"{prefix}/documents/{document_index}/size_measurements/{index}",
                    measurement,
                )
                for document_index, document in enumerate(version.documents)
                for index, measurement in enumerate(document.size_measurements)
            )
            candidates.extend(
                (
                    f"{prefix}/annotations/{annotation_index}"
                    f"/coverage_measurements/{index}",
                    measurement,
                )
                for annotation_index, annotation in enumerate(version.annotations)
                for index, measurement in enumerate(annotation.coverage_measurements)
            )
            for path, measurement in candidates:
                method = _normalize_method(measurement.counting_method)
                if request.unit is not None and measurement.unit != request.unit:
                    warnings.add("measurement_unit_filtered")
                    continue
                if (
                    request.counting_method is not None
                    and method != request.counting_method
                ):
                    warnings.add("measurement_counting_method_filtered")
                    continue
                result.append(
                    _Measurement(
                        resource_id=resource.id,
                        version_id=version.id,
                        path=path,
                        value=measurement,
                    )
                )
    return (
        tuple(sorted(result, key=lambda item: item.path)),
        tuple(sorted(warnings)),
    )


def _finish_cell(
    cell: _CellAccumulator, measurements: tuple[_Measurement, ...]
) -> CoverageCell:
    refs: list[MeasurementRef] = []
    crossing: set[str] = set()
    versions: dict[tuple[str, str, str, str], list[MeasurementRef]] = defaultdict(list)
    for item in measurements:
        if not _measurement_intersects(cell, item):
            continue
        ref = _measurement_ref(item)
        refs.append(ref)
        signature = (
            item.resource_id,
            item.version_id,
            ref.unit,
            ref.counting_method,
        )
        versions[signature].append(ref)
        if not _measurement_aligned(cell, item.value.scope):
            crossing.add(item.path)

    totals: list[CompatibleTotal] = []
    for signature in sorted(versions):
        grouped = sorted(versions[signature], key=lambda item: item.measurement_path)
        paths = tuple(item.measurement_path for item in grouped)
        unaligned = any(path in crossing for path in paths)
        counted, ambiguous = _maximal_measurements(grouped)
        totals.append(
            CompatibleTotal(
                unit=signature[2],
                counting_method=signature[3],
                measurement_paths=paths,
                total=(
                    None
                    if unaligned or ambiguous
                    else sum((item.value for item in counted), Decimal())
                ),
                exactness=(
                    "not_computable"
                    if unaligned or ambiguous
                    else "non_exact"
                    if any(item.uncertainty_note is not None for item in counted)
                    else "exact"
                ),
            )
        )
    warnings = ("measurement_scope_crosses_cell_boundaries",) if crossing else ()
    return CoverageCell(
        coordinates=cell.coordinates,
        resource_ids=tuple(sorted(cell.resource_ids)),
        version_ids=tuple(sorted(cell.version_ids)),
        component_ids=tuple(sorted(cell.component_ids)),
        document_ids=tuple(sorted(cell.document_ids)),
        annotation_ids=tuple(sorted(cell.annotation_ids)),
        measurements=tuple(sorted(refs, key=lambda item: item.measurement_path)),
        compatible_totals=tuple(totals),
        warnings=warnings,
    )


def _measurement_ref(item: _Measurement) -> MeasurementRef:
    measurement = item.value
    uncertainty = (
        measurement.uncertainty_note.value
        if isinstance(measurement.uncertainty_note, KnownValue)
        else None
    )
    return MeasurementRef(
        measurement_path=item.path,
        unit=measurement.unit,
        value=Decimal(measurement.value),
        counting_method=_normalize_method(measurement.counting_method),
        origin=measurement.origin,
        scope=measurement.scope.model_copy(deep=True),
        uncertainty_note=uncertainty,
        evidence_ids=tuple(sorted(measurement.evidence_ids)),
    )


def _measurement_intersects(cell: _CellAccumulator, item: _Measurement) -> bool:
    if (
        item.resource_id not in cell.resource_ids
        or item.version_id not in cell.version_ids
    ):
        return False
    scope = item.value.scope
    checks = (
        (scope.component_ids, cell.component_ids),
        (scope.document_ids, cell.document_ids),
        (scope.annotation_ids, cell.annotation_ids),
    )
    return all(
        not selected or not identities or bool(selected & identities)
        for selected, identities in checks
    )


def _measurement_aligned(cell: _CellAccumulator, scope: SelectionScope) -> bool:
    if scope.resource_ids != cell.resource_ids:
        return False
    if scope.version_ids != cell.version_ids:
        return False
    if cell.annotation_ids:
        return scope.annotation_ids == cell.annotation_ids and (
            not cell.document_ids or scope.document_ids == cell.document_ids
        )
    if cell.document_ids:
        return scope.document_ids == cell.document_ids
    if cell.component_ids:
        return scope.component_ids == cell.component_ids
    return True


def _normalize_method(value: str) -> str:
    return " ".join(value.split()).casefold()


def _maximal_measurements(
    measurements: list[MeasurementRef],
) -> tuple[list[MeasurementRef], bool]:
    counted: list[MeasurementRef] = []
    ambiguous = False
    for candidate in measurements:
        equivalent = [
            other
            for other in measurements
            if other is not candidate and other.scope == candidate.scope
        ]
        if equivalent:
            ambiguous = True
            continue
        if any(
            other is not candidate and _scope_contains(other.scope, candidate.scope)
            for other in measurements
        ):
            continue
        counted.append(candidate)
    for index, left in enumerate(counted):
        for right in counted[index + 1 :]:
            if not _scopes_explicitly_disjoint(left.scope, right.scope):
                ambiguous = True
    return counted, ambiguous


def _scope_contains(container: SelectionScope, contained: SelectionScope) -> bool:
    fields = (
        "resource_ids",
        "version_ids",
        "component_ids",
        "document_ids",
        "annotation_ids",
    )
    strict = False
    for field in fields:
        outer = getattr(container, field)
        inner = getattr(contained, field)
        if outer and (not inner or not inner.issubset(outer)):
            return False
        if outer != inner:
            strict = True
    return strict


def _scopes_explicitly_disjoint(left: SelectionScope, right: SelectionScope) -> bool:
    for field in ("resource_ids", "version_ids", "document_ids"):
        left_ids = getattr(left, field)
        right_ids = getattr(right, field)
        if left_ids and right_ids and left_ids.isdisjoint(right_ids):
            return True
    return False


def _dimension_key(value: DimensionValue) -> tuple[object, ...]:
    return (
        value.dimension,
        _STATUS_ORDER[value.knowledge_status],
        value.range_start if value.range_start is not None else -(10**20),
        value.range_end if value.range_end is not None else -(10**20),
        value.label,
        value.value_id or "",
        value.certainty_id or "",
    )


__all__ = [
    "CompatibleTotal",
    "CoverageCell",
    "CoverageMatrix",
    "CoverageRequest",
    "DimensionValue",
    "HistGermQueryError",
    "InvalidQueryError",
    "MeasurementRef",
    "UnsupportedCoverageDimensionError",
    "coverage_matrix",
]
