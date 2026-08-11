from __future__ import annotations

from histgerm.models.common import KnownValue
from histgerm.packaging import load_verified_bundled_catalog
from histgerm.query import CatalogQuery, DimensionFilter, QueryFilter
from histgerm.query.coverage import CoverageRequest, coverage_matrix


def _ids(values: tuple[object, ...], attribute: str = "resource_id") -> list[str]:
    return [getattr(value, attribute) for value in values]


def test_public_discovery_contract_exercises_all_basic_and_compound_filters() -> None:
    query = CatalogQuery(catalog=load_verified_bundled_catalog())

    assert _ids(query.by_category(frozenset({"dictionary"}))) == ["res-mwb"]
    assert _ids(query.by_language_stage(frozenset({"enhg"}))) == ["res-rnntagger"]
    assert _ids(query.by_annotation(frozenset({"lemma"})), "annotation_id") == [
        "ann-rem-lemma"
    ]
    assert "dist-rem-2-1-zenodo" in _ids(
        query.by_format(frozenset({"tei_xml"})), "distribution_id"
    )
    assert _ids(query.by_license(frozenset({"cc_by_sa_4_0"})), "distribution_id") == [
        "dist-rem-2-1-zenodo"
    ]
    assert set(
        _ids(query.by_availability(frozenset({"available"})), "distribution_id")
    ) == {
        "dist-mwb-online",
        "dist-mwb-woerterbuchnetz-api",
        "dist-rem-2-1-zenodo",
        "dist-rnntagger-1-5-0-project",
    }
    assert set(
        _ids(query.by_annotation_quality(frozenset({"unknown"})), "annotation_id")
    ) == {
        "ann-rem-char-align",
        "ann-rem-lemma",
        "ann-rem-morphology",
        "ann-rem-pos",
        "ann-rem-tokenization",
    }

    matches = query.resources(
        QueryFilter(
            identifier_or_name="Reference Corpus of Middle High German",
            categories=DimensionFilter(values=frozenset({"corpus"})),
            language_stages=DimensionFilter(values=frozenset({"mhg"})),
            annotation_types=DimensionFilter(values=frozenset({"lemma", "pos"})),
            formats=DimensionFilter(values=frozenset({"tei_xml"})),
            licenses=DimensionFilter(values=frozenset({"cc_by_sa_4_0"})),
            availability=DimensionFilter(values=frozenset({"available"})),
            annotation_qualities=DimensionFilter(values=frozenset({"unknown"})),
        )
    )
    assert _ids(matches) == ["res-rem"]


def test_annotation_subsets_and_qualities_remain_independently_queryable() -> None:
    catalog = load_verified_bundled_catalog()
    rem = next(resource for resource in catalog.resources if resource.id == "res-rem")
    version = rem.versions[0]
    lemma, pos, *remaining = version.annotations
    component_id = version.components[0].id
    scoped_lemma = lemma.model_copy(
        update={
            "quality": "expert_gold",
            "scope": lemma.scope.model_copy(
                update={"component_ids": frozenset({component_id})}
            ),
        }
    )
    scoped_pos = pos.model_copy(update={"quality": "silver"})
    changed_version = version.model_copy(
        update={"annotations": [scoped_lemma, scoped_pos, *remaining]}
    )
    changed_resource = rem.model_copy(update={"versions": [changed_version]})
    synthetic = catalog.model_copy(
        update={
            "resources": [
                changed_resource if item.id == rem.id else item
                for item in catalog.resources
            ]
        }
    )
    query = CatalogQuery(catalog=synthetic)

    gold = query.by_annotation_quality(frozenset({"expert_gold"}))
    silver = query.by_annotation_quality(frozenset({"silver"}))
    assert gold[0].annotation_id == "ann-rem-lemma"
    assert gold[0].scope.component_ids == {component_id}
    assert silver[0].annotation_id == "ann-rem-pos"
    assert silver[0].scope.component_ids == frozenset()


def test_access_and_permission_combinations_are_reported_without_inference() -> None:
    catalog = load_verified_bundled_catalog()
    rem = next(resource for resource in catalog.resources if resource.id == "res-rem")
    version = rem.versions[0]
    distribution = version.distributions[0]
    access = distribution.access.model_copy(
        update={
            "authentication_or_agreement": "registration",
            "automated_access": "prohibited",
            "model_training": "unclear",
        }
    )
    changed_distribution = distribution.model_copy(
        update={"availability": "authentication_required", "access": access}
    )
    changed_version = version.model_copy(
        update={"distributions": [changed_distribution]}
    )
    changed_resource = rem.model_copy(update={"versions": [changed_version]})
    synthetic = catalog.model_copy(
        update={
            "resources": [
                changed_resource if item.id == rem.id else item
                for item in catalog.resources
            ]
        }
    )
    query = CatalogQuery(catalog=synthetic)

    match = query.by_availability(frozenset({"authentication_required"}))[0]
    assert match.distribution_id == distribution.id
    assert match.availability == "authentication_required"
    assert match.model_training == "unclear"
    assert match.warnings == ("access_requirement=registration",)
    review = query.training_permission_review()[0]
    assert review.permission == "unclear"
    assert review.availability == "authentication_required"
    assert review.reasons == ("model_training=unclear",)


def test_coverage_contract_spans_period_dialect_genre_work_witness_annotation() -> None:
    catalog = load_verified_bundled_catalog()
    matrices = {
        dimension: coverage_matrix(catalog, CoverageRequest(dimensions=(dimension,)))
        for dimension in (
            "period",
            "dialect",
            "genre",
            "work",
            "witness",
            "annotation_type",
        )
    }

    assert matrices["period"].cells
    assert matrices["dialect"].cells
    assert matrices["genre"].cells
    assert matrices["work"].cells == ()
    assert matrices["witness"].cells == ()
    annotation_values = {
        cell.coordinates[0].value_id for cell in matrices["annotation_type"].cells
    }
    assert {"lemma", "pos", "morphology"} <= annotation_values
    assert catalog == load_verified_bundled_catalog()


def test_query_operations_do_not_mutate_catalog() -> None:
    catalog = load_verified_bundled_catalog()
    before = catalog.model_dump_json()
    query = CatalogQuery(catalog=catalog)

    query.resources()
    query.by_availability(frozenset({"available"}))
    query.by_annotation(frozenset({"lemma"}))
    query.training_permission_review()
    coverage_matrix(catalog, CoverageRequest(dimensions=("language_stage",)))

    assert catalog.model_dump_json() == before
    assert isinstance(catalog.resources[0].language_stage_ids, KnownValue)
