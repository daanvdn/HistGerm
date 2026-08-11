from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal
from typing import Any, cast

import pytest

from histgerm.models.catalog import Catalog
from histgerm.models.common import (
    KnownValue,
    NotApplicableValue,
    SelectionScope,
    UnknownValue,
)
from histgerm.models.corpus import CorpusComponent, SizeMeasurement
from histgerm.models.relationships import OverlapMeasurement, Relationship
from histgerm.models.resource import Resource, ResourceVersion
from histgerm.query.overlap import (
    CatalogQuery,
    RelationshipCycleError,
    RelationshipRequest,
    SizeSummaryRequest,
)

NA = NotApplicableValue(status="not_applicable")
UNKNOWN = UnknownValue(status="unknown")


def scope(
    resource: str,
    version: str,
    *,
    component: str | None = None,
) -> SelectionScope:
    return SelectionScope(
        resource_ids=frozenset({resource}),
        version_ids=frozenset({version}),
        component_ids=frozenset({component}) if component else frozenset(),
        document_ids=frozenset(),
        annotation_ids=frozenset(),
        filter=NA,
    )


def measurement(
    selected_scope: SelectionScope,
    value: int,
    *,
    unit: str = "token",
    method: str = "tei-token-elements",
    uncertain: bool = False,
) -> SizeMeasurement:
    return SizeMeasurement.model_validate(
        {
            "unit": unit,
            "value": value,
            "version_id": next(iter(selected_scope.version_ids)),
            "scope": selected_scope,
            "counting_method": method,
            "origin": "locally_computed",
            "computed_on": {"status": "known", "value": date(2026, 1, 1)},
            "evidence_ids": [],
            "uncertainty_note": (
                {"status": "known", "value": "estimated boundary"}
                if uncertain
                else {"status": "not_applicable"}
            ),
        }
    )


def resource(
    resource_id: str,
    version_id: str,
    version_measurements: list[SizeMeasurement],
    components: list[tuple[str, list[SizeMeasurement]]] | None = None,
) -> Resource:
    component_models = [
        cast(Any, CorpusComponent).model_construct(
            id=component_id, size_measurements=values
        )
        for component_id, values in (components or [])
    ]
    version = cast(Any, ResourceVersion).model_construct(
        id=version_id,
        size_measurements=version_measurements,
        components=component_models,
        documents=[],
        annotations=[],
    )
    built_resource = cast(Any, Resource).model_construct(
        id=resource_id, versions=[version]
    )
    return cast(Resource, built_resource)


def relationship(
    relationship_id: str,
    kind: str,
    source_id: str,
    target_id: str,
    source_scope: SelectionScope,
    target_scope: SelectionScope,
    *,
    extent: str,
    overlap: OverlapMeasurement | None = None,
    canonical: SelectionScope | None = None,
) -> Relationship:
    entity_type = "component" if source_id.startswith("comp-") else "version"
    target_type = "component" if target_id.startswith("comp-") else "version"
    return Relationship.model_validate(
        {
            "record_type": "relationship",
            "id": relationship_id,
            "source": {"entity_type": entity_type, "id": source_id},
            "target": {"entity_type": target_type, "id": target_id},
            "kind": kind,
            "directional": kind in {"contains", "part_of", "derived_from"},
            "source_scope": source_scope,
            "target_scope": target_scope,
            "overlap_extent": extent,
            "overlap_measurement": (
                {"status": "known", "value": overlap}
                if overlap is not None
                else {"status": "not_applicable"}
            ),
            "certainty": {"status": "known", "value": "certain"},
            "note": {"status": "not_applicable"},
            "duplicate_group_id": (
                {"status": "known", "value": "duplicate-group"}
                if kind == "duplicates"
                else {"status": "not_applicable"}
            ),
            "canonical_scope": (
                {"status": "known", "value": canonical}
                if canonical is not None
                else {"status": "not_applicable"}
            ),
            "evidence": [],
            "claims": {},
            "extensions": {},
        }
    )


def catalog(resources: list[Resource], relationships: list[Relationship]) -> Catalog:
    built_catalog = cast(Any, Catalog).model_construct(
        resources=resources, relationships=relationships
    )
    return cast(Catalog, built_catalog)


def selection(*scopes: SelectionScope) -> SelectionScope:
    return SelectionScope(
        resource_ids=frozenset().union(*(item.resource_ids for item in scopes)),
        version_ids=frozenset().union(*(item.version_ids for item in scopes)),
        component_ids=frozenset().union(*(item.component_ids for item in scopes)),
        document_ids=frozenset(),
        annotation_ids=frozenset(),
        filter=NA,
    )


def test_exact_duplicate_counts_explicit_canonical_once() -> None:
    alpha = scope("res-alpha", "ver-alpha", component="comp-alpha")
    gamma = scope("res-gamma", "ver-gamma", component="comp-gamma")
    rel = relationship(
        "rel-duplicate",
        "duplicates",
        "comp-alpha",
        "comp-gamma",
        alpha,
        gamma,
        extent="exact",
        canonical=alpha,
    )
    query = CatalogQuery(
        catalog=catalog(
            [
                resource(
                    "res-alpha",
                    "ver-alpha",
                    [],
                    [("comp-alpha", [measurement(alpha, 400)])],
                ),
                resource(
                    "res-gamma",
                    "ver-gamma",
                    [],
                    [("comp-gamma", [measurement(gamma, 400)])],
                ),
            ],
            [rel],
        )
    )

    result = query.size_summary(
        SizeSummaryRequest(selection=selection(alpha, gamma), unit="token")
    )

    assert result.total == Decimal(400)
    assert result.exactness == "exact"
    assert len(result.counted_measurement_paths) == 1
    assert result.exclusions[0].reason == "exact_duplicate"


def test_containment_excludes_selected_child_without_subtracting_it() -> None:
    parent = scope("res-alpha", "ver-alpha")
    child = scope("res-alpha", "ver-alpha", component="comp-alpha")
    rel = relationship(
        "rel-contains",
        "contains",
        "ver-alpha",
        "comp-alpha",
        parent,
        child,
        extent="contains",
    )
    query = CatalogQuery(
        catalog=catalog(
            [
                resource(
                    "res-alpha",
                    "ver-alpha",
                    [measurement(parent, 1000)],
                    [("comp-alpha", [measurement(child, 400)])],
                )
            ],
            [rel],
        )
    )

    result = query.size_summary(
        SizeSummaryRequest(selection=selection(parent, child), unit="token")
    )

    assert result.total == Decimal(1000)
    assert result.adjustments == ()
    assert result.exclusions[0].reason == "contained_scope"


def test_compatible_partial_overlap_is_subtracted_transparently() -> None:
    alpha = scope("res-alpha", "ver-alpha")
    beta = scope("res-beta", "ver-beta")
    overlap = OverlapMeasurement(
        unit="token",
        value=100,
        counting_method="tei-token-elements",
        source_version_id="ver-alpha",
        target_version_id="ver-beta",
        source_scope=alpha,
        target_scope=beta,
        origin="locally_computed",
        computed_on=KnownValue(status="known", value=date(2026, 1, 1)),
        evidence_ids=frozenset(),
        uncertainty_note=NA,
    )
    rel = relationship(
        "rel-overlap",
        "overlaps",
        "ver-alpha",
        "ver-beta",
        alpha,
        beta,
        extent="partial",
        overlap=overlap,
    )
    query = CatalogQuery(
        catalog=catalog(
            [
                resource("res-alpha", "ver-alpha", [measurement(alpha, 1000)]),
                resource("res-beta", "ver-beta", [measurement(beta, 600)]),
            ],
            [rel],
        )
    )

    result = query.size_summary(
        SizeSummaryRequest(selection=selection(alpha, beta), unit="token")
    )

    assert result.total == Decimal(1500)
    assert result.adjustments[0].subtracted_value == Decimal(100)
    assert result.exactness == "exact"


def test_incompatible_units_and_methods_never_create_a_total() -> None:
    alpha = scope("res-alpha", "ver-alpha")
    delta = scope("res-delta", "ver-delta")
    rel = relationship(
        "rel-overlap",
        "overlaps",
        "ver-alpha",
        "ver-delta",
        alpha,
        delta,
        extent="partial",
    )
    query = CatalogQuery(
        catalog=catalog(
            [
                resource(
                    "res-alpha",
                    "ver-alpha",
                    [
                        measurement(alpha, 1000),
                        measurement(alpha, 5000, unit="character"),
                    ],
                ),
                resource(
                    "res-delta",
                    "ver-delta",
                    [measurement(delta, 300, method="whitespace-tokenization")],
                ),
            ],
            [rel],
        )
    )

    result = query.size_summary(
        SizeSummaryRequest(selection=selection(alpha, delta), unit="token")
    )

    assert result.total is None
    assert result.exactness == "not_computable"
    assert {item.reason for item in result.exclusions} == {"incompatible_unit"}
    assert {item.counting_method for item in result.selected_measurements} == {
        "tei-token-elements",
        "whitespace-tokenization",
    }


def test_unquantified_partial_and_unknown_extent_are_unresolved() -> None:
    alpha = scope("res-alpha", "ver-alpha")
    beta = scope("res-beta", "ver-beta")
    resources = [
        resource("res-alpha", "ver-alpha", [measurement(alpha, 1000)]),
        resource("res-beta", "ver-beta", [measurement(beta, 600)]),
    ]
    for extent in ("partial", "unknown"):
        rel = relationship(
            "rel-overlap",
            "overlaps",
            "ver-alpha",
            "ver-beta",
            alpha,
            beta,
            extent=extent,
        )
        result = CatalogQuery(catalog=catalog(resources, [rel])).size_summary(
            SizeSummaryRequest(selection=selection(alpha, beta), unit="token")
        )
        assert result.total is None
        assert result.exactness == "not_computable"
        assert result.unresolved_overlaps[0].reason == (
            "partial_overlap_unquantified" if extent == "partial" else "extent_unknown"
        )


def test_containment_and_derivation_cycles_raise_clear_errors() -> None:
    scopes = [
        scope("res-a", "ver-a", component="comp-a"),
        scope("res-b", "ver-b", component="comp-b"),
        scope("res-c", "ver-c", component="comp-c"),
    ]
    contains = [
        relationship(
            f"rel-{source[-1]}-{target[-1]}",
            "contains",
            source,
            target,
            source_scope,
            target_scope,
            extent="contains",
        )
        for source, target, source_scope, target_scope in (
            ("comp-a", "comp-b", scopes[0], scopes[1]),
            ("comp-b", "comp-c", scopes[1], scopes[2]),
            ("comp-c", "comp-a", scopes[2], scopes[0]),
        )
    ]
    query = CatalogQuery(catalog=catalog([], contains))
    with pytest.raises(
        RelationshipCycleError,
        match=r"containment relationship cycle detected: comp-a -> comp-b -> "
        r"comp-c -> comp-a",
    ):
        query.relationships(RelationshipRequest(resource_id="res-a"))

    derived = [
        relationship(
            "rel-a-b",
            "derived_from",
            "ver-a",
            "ver-b",
            scopes[0],
            scopes[1],
            extent="unknown",
        ),
        relationship(
            "rel-b-a",
            "derived_from",
            "ver-b",
            "ver-a",
            scopes[1],
            scopes[0],
            extent="unknown",
        ),
    ]
    with pytest.raises(
        RelationshipCycleError,
        match=r"derivation relationship cycle detected: ver-a -> ver-b -> ver-a",
    ):
        CatalogQuery(catalog=catalog([], derived)).relationships(
            RelationshipRequest(resource_id="res-a")
        )


def test_results_are_deterministic_and_catalog_is_not_mutated() -> None:
    alpha = scope("res-alpha", "ver-alpha")
    beta = scope("res-beta", "ver-beta")
    resources = [
        resource("res-beta", "ver-beta", [measurement(beta, 600)]),
        resource("res-alpha", "ver-alpha", [measurement(alpha, 1000)]),
    ]
    rel = relationship(
        "rel-overlap",
        "overlaps",
        "ver-alpha",
        "ver-beta",
        alpha,
        beta,
        extent="unknown",
    )
    source_catalog = catalog(resources, [rel])
    before = deepcopy(source_catalog.model_dump(mode="json"))
    query = CatalogQuery(catalog=source_catalog)

    first = query.size_summary(
        SizeSummaryRequest(selection=selection(alpha, beta), unit="token")
    )
    second = query.size_summary(
        SizeSummaryRequest(selection=selection(alpha, beta), unit="token")
    )
    traversal = query.relationships(RelationshipRequest(resource_id="res-alpha"))

    assert first == second
    assert tuple(
        item.measurement_path for item in first.selected_measurements
    ) == tuple(sorted(item.measurement_path for item in first.selected_measurements))
    assert traversal.relationships[0].relationship_id == "rel-overlap"
    assert source_catalog.model_dump(mode="json") == before
