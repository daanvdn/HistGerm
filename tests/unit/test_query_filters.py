from __future__ import annotations

from datetime import date
from types import SimpleNamespace as NS

import pytest
from pydantic import ValidationError

from histgerm.models.catalog import (
    Catalog,
    OpenRegistryDefinition,
    OpenRegistryRegistry,
    RegistryTerm,
    VocabularyDefinition,
    VocabularyRegistry,
)
from histgerm.models.common import KnownValue, SelectionScope, UnknownValue
from histgerm.query import (
    AmbiguousNameError,
    CatalogQuery,
    DimensionFilter,
    InvalidIdentifierError,
    InvalidQueryError,
    QueryFilter,
    SortSpec,
    UnknownVocabularyValueError,
)

UNKNOWN = UnknownValue(status="unknown")
SCOPE = SelectionScope.model_validate(
    {
        "resource_ids": ["res-scope"],
        "version_ids": [],
        "component_ids": [],
        "document_ids": [],
        "annotation_ids": [],
        "filter": {"status": "not_applicable"},
    }
)


def known(value: object) -> KnownValue[object]:
    return KnownValue[object](status="known", value=value)


def access(
    *,
    download: str = "available",
    api: str = "inaccessible",
    training: str = "permitted",
    automated: str = "permitted",
    requirement: str = "none",
) -> NS:
    return NS(
        download=download,
        api_access=api,
        request_only="inaccessible",
        model_training=training,
        automated_access=automated,
        authentication_or_agreement=requirement,
    )


def distribution(
    identifier: str,
    *,
    formats: tuple[str, ...] = ("tei_xml",),
    license_id: str | None = "cc_by_4_0",
    availability: str = "available",
    url_kind: str = "download",
    policy: NS | None = None,
) -> NS:
    format_items = [NS(format_id=item, inner_format_ids=UNKNOWN) for item in formats]
    return NS(
        id=identifier,
        formats=known(format_items),
        license=NS(license_id=known(license_id) if license_id is not None else UNKNOWN),
        availability=availability,
        access=policy or access(),
        access_urls=known(
            [NS(kind=url_kind, url=f"https://example.invalid/{identifier}")]
        ),
        scope=NS(component_ids=frozenset({"comp-core"})),
    )


def annotation(identifier: str, task: str, quality: str) -> NS:
    return NS(id=identifier, task=task, quality=quality, scope=SCOPE)


def resource(
    identifier: str,
    name: str,
    *,
    aliases: tuple[str, ...] = (),
    categories: frozenset[str] = frozenset({"corpus"}),
    resource_stages: frozenset[str] = frozenset({"mhg"}),
    version_stages: frozenset[str] = frozenset(),
    distributions: tuple[NS, ...] = (),
    annotations: tuple[NS, ...] = (),
) -> NS:
    version = NS(
        id=f"ver-{identifier.removeprefix('res-')}",
        language_stage_ids=known(version_stages) if version_stages else UNKNOWN,
        distributions=list(distributions),
        annotations=list(annotations),
    )
    return NS(
        id=identifier,
        canonical_name=name,
        alternative_names=[
            NS(text=alias, language=UNKNOWN, name_type="short_name")
            for alias in aliases
        ],
        categories=categories,
        language_stage_ids=known(resource_stages),
        versions=[version],
        claims={
            "/versions/0/distributions/0/access/model_training": frozenset(
                {"evidence-access"}
            ),
            "/versions/0/annotations/0/quality": frozenset({"evidence-annotation"}),
        },
    )


def catalog(*resources: NS) -> Catalog:
    vocabulary_ids = {
        "resource_category": {"corpus", "dictionary", "pos_tagger"},
        "language_stage": {"ohg", "mhg", "enhg"},
        "annotation_task": {"lemma", "pos"},
        "annotation_quality": {
            "expert_gold",
            "silver",
            "automatically_predicted",
            "unknown",
        },
        "data_format": {"tei_xml", "json", "zip"},
        "availability": {
            "available",
            "partially_available",
            "request_only",
            "authentication_required",
            "temporarily_unavailable",
            "discontinued",
            "inaccessible",
            "not_publicly_available",
            "unknown",
        },
    }
    vocabularies = VocabularyRegistry(
        root={
            name: VocabularyDefinition(schema_version="1", ids=frozenset(values))
            for name, values in vocabulary_ids.items()
        }
    )
    license_term = RegistryTerm.model_construct(
        id="cc_by_4_0",
        canonical_label="CC BY 4.0",
        aliases=[],
        description=UNKNOWN,
        evidence=[],
        claims={},
    )
    registries = OpenRegistryRegistry(
        root={
            "licenses": OpenRegistryDefinition(schema_version="1", terms=[license_term])
        }
    )
    return Catalog.model_construct(
        schema_version="1",
        inventory_release="test",
        generated_on=date(2026, 8, 11),
        vocabularies=vocabularies,
        registries=registries,
        resources=list(resources),  # type: ignore[arg-type]
        works=[],
        witnesses=[],
        publications=[],
        relationships=[],
        notes=UNKNOWN,
        extensions={},
    )


@pytest.fixture
def query() -> CatalogQuery:
    alpha = resource(
        "res-zeta",
        "Ａlpha  Corpus",
        aliases=("A Corpus",),
        categories=frozenset({"corpus", "dictionary"}),
        resource_stages=frozenset({"mhg", "enhg"}),
        distributions=(
            distribution("dist-alpha", formats=("zip", "tei_xml")),
            distribution(
                "dist-split",
                formats=("json",),
                license_id=None,
                availability="partially_available",
            ),
        ),
        annotations=(
            annotation("ann-lemma", "lemma", "silver"),
            annotation("ann-pos", "pos", "expert_gold"),
        ),
    )
    beta = resource(
        "res-alpha",
        "Beta Corpus",
        aliases=("A-Corpus",),
        resource_stages=frozenset({"ohg"}),
        version_stages=frozenset({"mhg"}),
        distributions=(
            distribution(
                "dist-beta",
                availability="available",
                url_kind="api",
                policy=access(
                    download="inaccessible",
                    api="available",
                    training="unclear",
                ),
            ),
        ),
        annotations=(annotation("ann-beta", "lemma", "expert_gold"),),
    )
    return CatalogQuery(catalog=catalog(alpha, beta))


def ids(matches: tuple[object, ...], attribute: str = "resource_id") -> list[str]:
    return [getattr(match, attribute) for match in matches]


def test_exact_identifier_canonical_alias_and_normalization(
    query: CatalogQuery,
) -> None:
    assert query.find("res-zeta")[0].matched_on == "identifier"
    assert query.find(" alpha corpus ")[0].matched_on == "canonical_name"
    assert query.find("A   Corpus")[0].matched_on == "alternative_name"
    assert query.find("A-Corpus")[0].resource_id == "res-alpha"
    assert query.find("A Corpus!") == ()
    assert query.find("res-missing") == ()
    with pytest.raises(InvalidIdentifierError, match="invalid stable identifier"):
        query.find("res-Bad")
    with pytest.raises(InvalidQueryError, match="must not be empty"):
        query.find("\u2003")


def test_ambiguous_normalized_names_are_errors() -> None:
    left = resource("res-left", "Same")
    right = resource("res-right", "Other", aliases=("Ｓame",))
    with pytest.raises(
        AmbiguousNameError,
        match="normalized name 'same' matches multiple resources: res-left, res-right",
    ):
        CatalogQuery(catalog=catalog(left, right)).find(" SAME ")


def test_every_basic_resource_and_distribution_filter(query: CatalogQuery) -> None:
    assert ids(query.by_category(frozenset({"dictionary"}))) == ["res-zeta"]
    assert ids(query.by_language_stage(frozenset({"ohg"}))) == ["res-alpha"]
    assert ids(query.by_format(frozenset({"json"})), "distribution_id") == [
        "dist-split"
    ]
    assert ids(query.by_license(frozenset({"cc_by_4_0"})), "distribution_id") == [
        "dist-beta",
        "dist-alpha",
    ]
    assert ids(
        query.by_availability(frozenset({"partially_available"})),
        "distribution_id",
    ) == ["dist-split"]
    assert ids(query.by_annotation_quality(frozenset({"silver"})), "annotation_id") == [
        "ann-lemma"
    ]


def test_any_all_and_and_across_dimensions(query: CatalogQuery) -> None:
    assert ids(query.by_category(frozenset({"corpus", "dictionary"}), match="all")) == [
        "res-zeta"
    ]
    compound = QueryFilter(
        categories=DimensionFilter(values=frozenset({"dictionary"})),
        language_stages=DimensionFilter(values=frozenset({"mhg"})),
        formats=DimensionFilter(values=frozenset({"tei_xml"})),
        licenses=DimensionFilter(values=frozenset({"cc_by_4_0"})),
        availability=DimensionFilter(values=frozenset({"available"})),
    )
    assert ids(query.resources(compound)) == ["res-zeta"]
    assert (
        query.by_availability(
            frozenset({"available", "partially_available"}), match="all"
        )
        == ()
    )


def test_same_layer_and_same_distribution_semantics(query: CatalogQuery) -> None:
    assert (
        query.by_annotation(frozenset({"lemma"}), qualities=frozenset({"expert_gold"}))[
            0
        ].annotation_id
        == "ann-beta"
    )
    zeta_only = QueryFilter(
        identifier_or_name="res-zeta",
        annotation_types=DimensionFilter(values=frozenset({"lemma"})),
        annotation_qualities=DimensionFilter(values=frozenset({"expert_gold"})),
    )
    assert query.resources(zeta_only) == ()
    split_distribution = QueryFilter(
        formats=DimensionFilter(values=frozenset({"json"})),
        licenses=DimensionFilter(values=frozenset({"cc_by_4_0"})),
    )
    assert query.resources(split_distribution) == ()


def test_helpers_exclude_unclear_and_keep_review_distinct(query: CatalogQuery) -> None:
    assert ids(query.downloadable_corpora("mhg"), "distribution_id") == ["dist-alpha"]
    assert ids(
        query.downloadable_corpora("mhg", include_partially_available=True),
        "distribution_id",
    ) == ["dist-alpha", "dist-split"]
    assert ids(query.training_compatible_corpora("mhg"), "distribution_id") == [
        "dist-alpha"
    ]
    assert (
        query.training_compatible_corpora("mhg", require_downloadable=False)[
            0
        ].model_training
        == "permitted"
    )
    review = query.training_permission_review(language_stage="mhg")
    assert ids(review, "distribution_id") == ["dist-beta"]
    assert review[0].permission == "unclear"


def test_deterministic_sorting_and_snapshot_evidence(query: CatalogQuery) -> None:
    assert ids(query.by_category(frozenset({"corpus"}))) == [
        "res-alpha",
        "res-zeta",
    ]
    assert ids(
        query.by_category(
            frozenset({"corpus"}),
            sort=SortSpec(field="canonical_name", direction="ascending"),
        )
    ) == ["res-zeta", "res-alpha"]
    assert query.by_annotation_quality(frozenset({"silver"}))[0].evidence_ids == (
        "evidence-annotation",
    )


def test_unknown_empty_and_immutable_inputs(query: CatalogQuery) -> None:
    with pytest.raises(UnknownVocabularyValueError, match="unknown category"):
        query.by_category(frozenset({"bogus"}))
    with pytest.raises(ValidationError):
        DimensionFilter(values=frozenset())
    values = {"corpus"}
    selected = DimensionFilter(values=values)  # type: ignore[arg-type]
    values.add("dictionary")
    assert selected.values == frozenset({"corpus"})
    before = tuple(
        distribution.id
        for resource_item in query.catalog.resources
        for version in resource_item.versions
        for distribution in version.distributions
    )
    query.resources(QueryFilter(categories=selected))
    after = tuple(
        distribution.id
        for resource_item in query.catalog.resources
        for version in resource_item.versions
        for distribution in version.distributions
    )
    assert before == after
    with pytest.raises(ValidationError):
        selected.values = frozenset({"dictionary"})  # type: ignore[misc]
