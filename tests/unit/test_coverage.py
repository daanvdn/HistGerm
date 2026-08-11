from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import pytest
from pydantic import ValidationError

from histgerm.models.annotation import AnnotationLayer
from histgerm.models.catalog import Catalog
from histgerm.models.common import (
    DateRange,
    GeographicCoverage,
    KnownValue,
    NotApplicableValue,
    SelectionScope,
    UnknownValue,
)
from histgerm.models.corpus import CorpusComponent, Document, SizeMeasurement
from histgerm.models.resource import Resource, ResourceVersion
from histgerm.query.coverage import CoverageRequest, coverage_matrix

UNKNOWN = UnknownValue(status="unknown")
NOT_APPLICABLE = NotApplicableValue(status="not_applicable")


def known[T](value: T) -> KnownValue[T]:
    return KnownValue[T](status="known", value=value)


def scope(**values: Iterable[str]) -> SelectionScope:
    return SelectionScope(
        resource_ids=frozenset(values.get("resource_ids", ())),
        version_ids=frozenset(values.get("version_ids", ())),
        component_ids=frozenset(values.get("component_ids", ())),
        document_ids=frozenset(values.get("document_ids", ())),
        annotation_ids=frozenset(values.get("annotation_ids", ())),
        filter=UNKNOWN,
    )


def measurement(
    value: int,
    *,
    unit: str = "token",
    method: str = "Whitespace tokens",
    selected_scope: SelectionScope,
    uncertain: bool = False,
) -> SizeMeasurement:
    return SizeMeasurement.model_construct(
        unit=unit,
        value=value,
        version_id="ver-one",
        scope=selected_scope,
        counting_method=method,
        origin="locally_computed",
        computed_on=known(date(2026, 8, 11)),
        evidence_ids=frozenset(),
        uncertainty_note=known("estimate") if uncertain else NOT_APPLICABLE,
    )


def fixture_catalog() -> Catalog:
    chronology = known(
        DateRange(
            earliest_year=known(1200),
            latest_year=known(1250),
            label=known("thirteenth century"),
            dating_method=known("editorial"),
            certainty=known("probable"),
        )
    )
    geography = known(
        GeographicCoverage(
            region_ids=known(frozenset({"rhein"})),
            dialect_ids=known(frozenset({"alemannic"})),
            certainty=known("probable"),
            note=UNKNOWN,
        )
    )
    document = Document.model_construct(
        id="doc-one",
        title=UNKNOWN,
        component_ids=frozenset({"comp-one"}),
        work_ids=frozenset({"work-one"}),
        witness_ids=frozenset({"wit-one"}),
        edition_witness_ids=frozenset(),
        external_identifiers=[],
        authorship=UNKNOWN,
        language_stage_ids=known(frozenset({"mhg"})),
        chronology=chronology,
        geography=geography,
        genres=known(frozenset({"sermon"})),
        text_types=UNKNOWN,
        language_mixture=UNKNOWN,
        stable_segment_identifier_types=UNKNOWN,
        size_measurements=[
            measurement(
                10,
                selected_scope=scope(
                    resource_ids={"res-one"},
                    version_ids={"ver-one"},
                    document_ids={"doc-one"},
                ),
            ),
            measurement(
                20,
                method="characters excluding spaces",
                selected_scope=scope(
                    resource_ids={"res-one"},
                    version_ids={"ver-one"},
                    document_ids={"doc-one"},
                ),
            ),
        ],
        extensions={},
    )
    component = CorpusComponent.model_construct(
        id="comp-one",
        name="One",
        description=UNKNOWN,
        parent_component_id=NOT_APPLICABLE,
        language_stage_ids=known(frozenset({"mhg"})),
        chronology=chronology,
        geography=geography,
        genres=known(frozenset({"sermon"})),
        text_types=UNKNOWN,
        document_ids=frozenset({"doc-one"}),
        size_measurements=[],
        extensions={},
    )
    annotation = AnnotationLayer.model_construct(
        id="ann-one",
        task="lemma",
        scheme=UNKNOWN,
        scope=scope(
            resource_ids={"res-one"},
            version_ids={"ver-one"},
            document_ids={"doc-one"},
            annotation_ids={"ann-one"},
        ),
        coverage_measurements=[
            measurement(
                7,
                selected_scope=scope(
                    resource_ids={"res-one"},
                    version_ids={"ver-one"},
                    document_ids={"doc-one"},
                    annotation_ids={"ann-one"},
                ),
                uncertain=True,
            )
        ],
        alignment_unit=UNKNOWN,
        production_method=UNKNOWN,
        quality="expert_gold",
        annotators=UNKNOWN,
        guidelines_url=UNKNOWN,
        inter_annotator_agreement=UNKNOWN,
        evaluation_results=UNKNOWN,
        missing_value_convention=UNKNOWN,
        scheme_mappings=UNKNOWN,
        extensions={},
    )
    version = ResourceVersion.model_construct(
        id="ver-one",
        version_label=UNKNOWN,
        release_date=UNKNOWN,
        superseded=UNKNOWN,
        changelog_url=UNKNOWN,
        language_stage_ids=known(frozenset({"mhg"})),
        chronology=chronology,
        components=[component],
        documents=[document],
        distributions=[],
        annotations=[annotation],
        size_measurements=[],
        extensions={},
    )
    resource = Resource.model_construct(
        id="res-one",
        canonical_name="One",
        alternative_names=[],
        categories=frozenset({"corpus"}),
        description=UNKNOWN,
        responsible_parties=UNKNOWN,
        homepage_url=UNKNOWN,
        repository_url=UNKNOWN,
        language_stage_ids=known(frozenset({"mhg"})),
        chronology=chronology,
        geography=geography,
        maintenance_status=UNKNOWN,
        publication_ids=frozenset(),
        versions=[version],
        corpus=UNKNOWN,
        tool=NOT_APPLICABLE,
        dictionary=NOT_APPLICABLE,
        record_reviewed_on=date(2026, 8, 11),
        evidence=[],
        claims={},
    )
    return Catalog.model_construct(
        schema_version="1.0.0",
        inventory_release="fixture",
        generated_on=date(2026, 8, 11),
        vocabularies={},
        registries={},
        resources=[resource],
        works=[],
        witnesses=[],
        publications=[],
        relationships=[],
        notes=UNKNOWN,
        extensions={},
    )


@pytest.mark.parametrize(
    ("dimension", "expected"),
    [
        ("language_stage", "mhg"),
        ("period", "editorial"),
        ("region", "rhein"),
        ("dialect", "alemannic"),
        ("genre", "sermon"),
        ("work", "work-one"),
        ("witness", "wit-one"),
        ("annotation_type", "lemma"),
    ],
)
def test_every_coverage_dimension(dimension: str, expected: str) -> None:
    matrix = coverage_matrix(
        fixture_catalog(), CoverageRequest(dimensions=(dimension,))
    )
    assert any(cell.coordinates[0].value_id == expected for cell in matrix.cells)


def test_combined_dimensions_preserve_uncertainty_and_identity() -> None:
    matrix = coverage_matrix(
        fixture_catalog(),
        CoverageRequest(
            dimensions=("period", "dialect", "work", "witness", "annotation_type")
        ),
    )
    assert len(matrix.cells) == 1
    cell = matrix.cells[0]
    assert [item.value_id for item in cell.coordinates] == [
        "editorial",
        "alemannic",
        "work-one",
        "wit-one",
        "lemma",
    ]
    assert cell.coordinates[0].certainty_id == "probable"
    assert cell.coordinates[1].certainty_id == "probable"
    assert cell.resource_ids == ("res-one",)
    assert cell.version_ids == ("ver-one",)
    assert cell.component_ids == ("comp-one",)
    assert cell.document_ids == ("doc-one",)
    assert cell.annotation_ids == ("ann-one",)


def test_unknown_buckets_are_explicit_or_counted_as_omitted() -> None:
    catalog = fixture_catalog()
    resource = catalog.resources[0]
    unknown_resource = resource.model_copy(
        update={"geography": UNKNOWN, "versions": []}
    )
    unknown_catalog = catalog.model_copy(update={"resources": [unknown_resource]})

    included = coverage_matrix(unknown_catalog, CoverageRequest(dimensions=("region",)))
    assert included.cells[0].coordinates[0].label == "[unknown]"
    excluded = coverage_matrix(
        unknown_catalog,
        CoverageRequest(dimensions=("region",), include_unknown_buckets=False),
    )
    assert excluded.cells == ()
    assert excluded.omitted_unknown_count == 1


def test_measurements_are_partitioned_by_compatible_signature() -> None:
    matrix = coverage_matrix(
        fixture_catalog(),
        CoverageRequest(dimensions=("work",), unit="token"),
    )
    cell = matrix.cells[0]
    assert len(cell.compatible_totals) == 2
    assert {total.counting_method for total in cell.compatible_totals} == {
        "whitespace tokens",
        "characters excluding spaces",
    }
    assert sorted(total.total for total in cell.compatible_totals if total.total) == [
        10,
        20,
    ]


def test_annotation_measurement_retains_identity_and_uncertainty() -> None:
    matrix = coverage_matrix(
        fixture_catalog(),
        CoverageRequest(
            dimensions=("annotation_type",),
            unit="token",
            counting_method="  WHITESPACE   TOKENS ",
        ),
    )
    cell = matrix.cells[0]
    annotation = next(
        item for item in cell.measurements if "/annotations/" in item.measurement_path
    )
    assert annotation.value == 7
    assert annotation.uncertainty_note == "estimate"
    assert any(total.exactness == "not_computable" for total in cell.compatible_totals)


def test_matrix_snapshot_and_order_are_deterministic() -> None:
    request = CoverageRequest(dimensions=("genre", "dialect"))
    first = coverage_matrix(fixture_catalog(), request)
    second = coverage_matrix(fixture_catalog(), request)
    assert first.model_dump_json() == second.model_dump_json()
    assert first.cells == tuple(
        sorted(
            first.cells,
            key=lambda cell: tuple(
                (coordinate.value_id or "", coordinate.label)
                for coordinate in cell.coordinates
            ),
        )
    )


def test_results_are_immutable_and_catalog_is_not_mutated() -> None:
    catalog = fixture_catalog()
    original_resources = tuple(catalog.resources)
    matrix = coverage_matrix(
        catalog, CoverageRequest(dimensions=("work",), unit="token")
    )

    with pytest.raises(ValidationError, match="frozen"):
        matrix.cells[0].warnings = ()  # type: ignore[misc]
    assert tuple(catalog.resources) == original_resources
    assert len(catalog.resources[0].versions[0].documents[0].size_measurements) == 2


@pytest.mark.parametrize(
    ("dimensions", "message"),
    [
        ((), "between one and eight"),
        (("period", "period"), "coverage dimensions must be unique"),
        (("bogus",), "unsupported coverage dimension"),
    ],
)
def test_invalid_and_duplicate_dimensions(
    dimensions: tuple[str, ...], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        CoverageRequest(dimensions=dimensions)


def test_unsupported_unit_and_empty_method_use_approved_errors() -> None:
    with pytest.raises(ValidationError, match="unsupported size unit"):
        CoverageRequest(dimensions=("period",), unit="pages")
    with pytest.raises(ValidationError, match="counting_method must not be empty"):
        CoverageRequest(dimensions=("period",), counting_method="  ")
