# mypy: disable-error-code="arg-type,call-arg"

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from histgerm.models.access import AccessPolicy, Distribution, LicenseDescription
from histgerm.models.annotation import AnnotationLayer, AnnotationScheme
from histgerm.models.catalog import Catalog
from histgerm.models.common import (
    DateRange,
    KnownValue,
    NotApplicableValue,
    SelectionScope,
)
from histgerm.models.corpus import CorpusProfile, Document
from histgerm.models.dictionary import (
    DictionaryProfile,
    LexicalFeatures,
    SuitabilityAssessment,
)
from histgerm.models.resource import Resource, ResourceVersion
from histgerm.query.suitability import (
    AuxiliaryTask,
    SuitabilityRequest,
    analyze_suitability,
)

TASKS: tuple[AuxiliaryTask, ...] = (
    "lemmatization",
    "pos_tagging",
    "morphological_tagging",
    "dependency_parsing",
    "date_prediction",
    "dictionary_variant_learning",
)
ANNOTATION_TASKS = {
    "lemmatization": "lemma",
    "pos_tagging": "pos",
    "morphological_tagging": "morphology",
    "dependency_parsing": "dependencies",
}


def known(value: Any) -> KnownValue[Any]:
    return KnownValue(status="known", value=value)


def scope(
    *,
    document_ids: frozenset[str] = frozenset(),
    annotation_ids: frozenset[str] = frozenset(),
) -> SelectionScope:
    return SelectionScope(
        resource_ids=frozenset({"res-test"}),
        version_ids=frozenset({"ver-test"}),
        document_ids=document_ids,
        annotation_ids=annotation_ids,
        filter=NotApplicableValue(status="not_applicable"),
    )


def distribution(permission: str, selected_scope: SelectionScope) -> Distribution:
    access = AccessPolicy.model_construct(
        public_description="available",
        online_browsing="available",
        download="available",
        api_access="unknown",
        request_only="unknown",
        authentication_or_agreement="none",
        automated_access="permitted",
        model_training=permission,
        original_redistribution="unclear",
        processed_redistribution="unclear",
        trained_weights_publication="unclear",
    )
    license_description = LicenseDescription.model_construct(status="declared_standard")
    return Distribution.model_construct(
        id="dist-test",
        scope=selected_scope,
        availability="available",
        access=access,
        license=license_description,
    )


def claims_for(paths: set[str]) -> dict[str, frozenset[str]]:
    return {path: frozenset({"evidence-test"}) for path in paths}


def base_resource(
    version: ResourceVersion,
    *,
    corpus: KnownValue[Any] | NotApplicableValue,
    dictionary: KnownValue[Any] | NotApplicableValue,
    claims: dict[str, frozenset[str]],
) -> Resource:
    return Resource.model_construct(
        id="res-test",
        categories=frozenset({"corpus"}),
        language_stage_ids=known(frozenset({"mhg"})),
        versions=[version],
        corpus=corpus,
        dictionary=dictionary,
        claims=claims,
    )


def annotation_catalog(
    task: AuxiliaryTask,
    permission: str,
    *,
    evidence: bool = True,
    quality: str = "expert_gold",
    stage: str = "mhg",
) -> Catalog:
    annotation_task = ANNOTATION_TASKS[task]
    selected_scope = scope(annotation_ids=frozenset({"ann-test"}))
    scheme = AnnotationScheme.model_construct(name="Test scheme", version=known("1"))
    annotation = AnnotationLayer.model_construct(
        id="ann-test",
        task=annotation_task,
        scheme=known(scheme),
        scope=selected_scope,
        alignment_unit=known("token"),
        quality=quality,
    )
    corpus_profile = CorpusProfile.model_construct(
        sentence_segmentation_convention=known("Explicit sentence boundaries")
    )
    version = ResourceVersion.model_construct(
        id="ver-test",
        language_stage_ids=known(frozenset({stage})),
        components=[],
        documents=[],
        annotations=[annotation],
        distributions=[distribution(permission, selected_scope)],
    )
    paths = {
        "/versions/0/annotations/0/task",
        "/versions/0/annotations/0/alignment_unit/value",
        "/versions/0/annotations/0/quality",
        "/versions/0/distributions/0/availability",
        "/versions/0/distributions/0/license/status",
        "/versions/0/distributions/0/access/model_training",
    }
    if task != "lemmatization":
        paths.update(
            {
                "/versions/0/annotations/0/scheme/value/name",
                "/versions/0/annotations/0/scheme/value/version/value",
            }
        )
    if task == "dependency_parsing":
        paths.add("/corpus/value/sentence_segmentation_convention/value")
    resource = base_resource(
        version,
        corpus=known(corpus_profile),
        dictionary=NotApplicableValue(status="not_applicable"),
        claims=claims_for(paths) if evidence else {},
    )
    return Catalog.model_construct(resources=[resource])


def date_catalog(permission: str, *, evidence: bool = True) -> Catalog:
    chronology = DateRange.model_construct(
        earliest_year=known(1200),
        latest_year=known(1250),
        label=known("first half of the thirteenth century"),
        dating_method=known("editorial"),
        certainty=known("certain"),
    )
    document = Document.model_construct(
        id="doc-test",
        component_ids=frozenset(),
        language_stage_ids=known(frozenset({"mhg"})),
        chronology=known(chronology),
    )
    selected_scope = scope(document_ids=frozenset({"doc-test"}))
    version = ResourceVersion.model_construct(
        id="ver-test",
        language_stage_ids=known(frozenset({"mhg"})),
        components=[],
        documents=[document],
        annotations=[],
        distributions=[distribution(permission, selected_scope)],
    )
    paths = {
        "/versions/0/documents/0/chronology/value/earliest_year/value",
        "/versions/0/documents/0/chronology/value/latest_year/value",
        "/versions/0/documents/0/chronology/value/label/value",
        "/versions/0/documents/0/chronology/value/dating_method/value",
        "/versions/0/documents/0/chronology/value/certainty/value",
        "/versions/0/distributions/0/availability",
        "/versions/0/distributions/0/license/status",
        "/versions/0/distributions/0/access/model_training",
    }
    resource = base_resource(
        version,
        corpus=known(CorpusProfile.model_construct()),
        dictionary=NotApplicableValue(status="not_applicable"),
        claims=claims_for(paths) if evidence else {},
    )
    return Catalog.model_construct(resources=[resource])


def dictionary_catalog(
    task: AuxiliaryTask, permission: str, *, evidence: bool = True
) -> Catalog:
    selected_scope = scope()
    assessment = SuitabilityAssessment.model_construct(
        task=task,
        status="suitable",
        scope=selected_scope,
        quality=known("expert_gold"),
        model_training_permission=permission,
        evidence_ids=frozenset({"evidence-assessment"}) if evidence else frozenset(),
    )
    profile = DictionaryProfile.model_construct(
        source_language_stage_ids=known(frozenset({"mhg"})),
        download_formats=known([object()]),
        machine_readable_availability="available",
        lexical_features=LexicalFeatures.model_construct(
            headwords=known(True),
            lemmas=known(True),
            spelling_variants=known(True),
        ),
        supervision_suitability=known([assessment]),
    )
    version = ResourceVersion.model_construct(
        id="ver-test",
        language_stage_ids=known(frozenset({"mhg"})),
        components=[],
        documents=[],
        annotations=[],
        distributions=[distribution(permission, selected_scope)],
    )
    paths = {
        "/dictionary/value/lexical_features/lemmas/value",
        "/dictionary/value/lexical_features/spelling_variants/value",
        "/dictionary/value/download_formats/value/0/format_id",
        "/dictionary/value/machine_readable_availability",
        "/versions/0/distributions/0/license/status",
    }
    resource = base_resource(
        version,
        corpus=NotApplicableValue(status="not_applicable"),
        dictionary=known(profile),
        claims=claims_for(paths) if evidence else {},
    )
    return Catalog.model_construct(resources=[resource])


def catalog_for(
    task: AuxiliaryTask, permission: str, *, evidence: bool = True
) -> Catalog:
    if task in ANNOTATION_TASKS:
        return annotation_catalog(task, permission, evidence=evidence)
    if task == "date_prediction":
        return date_catalog(permission, evidence=evidence)
    return dictionary_catalog(task, permission, evidence=evidence)


@pytest.mark.parametrize("task", TASKS)
def test_all_six_tasks_have_suitable_positive_fixtures(task: AuxiliaryTask) -> None:
    result = analyze_suitability(
        catalog_for(task, "permitted"), SuitabilityRequest(task=task)
    )

    assert len(result) == 1
    assert result[0].decision == "suitable"
    assert result[0].reasons
    assert result[0].coverage
    assert result[0].evidence


@pytest.mark.parametrize("task", TASKS)
def test_explicit_prohibition_disqualifies_all_six_tasks(
    task: AuxiliaryTask,
) -> None:
    result = analyze_suitability(
        catalog_for(task, "prohibited"), SuitabilityRequest(task=task)
    )[0]

    assert result.decision == "not_suitable"
    assert any("PERMISSION_PROHIBITED" in item for item in result.limitations)


@pytest.mark.parametrize("task", TASKS)
def test_unclear_permission_requires_review_for_all_six_tasks(
    task: AuxiliaryTask,
) -> None:
    result = analyze_suitability(
        catalog_for(task, "unclear"), SuitabilityRequest(task=task)
    )[0]

    assert result.decision == "review_required"
    assert result.model_training_permissions == ("unclear",)


@pytest.mark.parametrize("task", TASKS)
def test_missing_positive_evidence_requires_review(task: AuxiliaryTask) -> None:
    result = analyze_suitability(
        catalog_for(task, "permitted", evidence=False),
        SuitabilityRequest(task=task),
    )[0]

    assert result.decision == "review_required"
    assert any("EVIDENCE_MISSING" in item for item in result.limitations)


def test_quality_and_language_scope_are_applied_to_the_same_annotation() -> None:
    catalog = annotation_catalog(
        "pos_tagging", "permitted", quality="silver", stage="ohg"
    )
    result = analyze_suitability(
        catalog,
        SuitabilityRequest(
            task="pos_tagging",
            language_stages=frozenset({"mhg"}),
            minimum_qualities=frozenset({"expert_gold"}),
        ),
    )[0]

    assert result.decision == "not_suitable"
    assert any("QUALITY_NOT_ACCEPTED" in item for item in result.limitations)
    assert any("SCOPE_LANGUAGE_STAGE_MISMATCH" in item for item in result.limitations)


def test_annotation_scopes_are_independent_and_results_are_stably_ordered() -> None:
    catalog = annotation_catalog("lemmatization", "permitted")
    resource = catalog.resources[0]
    version = resource.versions[0]
    first = version.annotations[0]
    second = first.model_copy(
        update={
            "id": "ann-z",
            "scope": scope(annotation_ids=frozenset({"ann-z"})),
        }
    )
    changed_version = version.model_copy(update={"annotations": [second, first]})
    changed_resource = resource.model_copy(update={"versions": [changed_version]})
    changed_catalog = catalog.model_copy(update={"resources": [changed_resource]})
    before = changed_catalog.model_copy(deep=True)

    results = analyze_suitability(
        changed_catalog, SuitabilityRequest(task="lemmatization")
    )

    assert [item.annotation_ids for item in results] == [
        ("ann-test",),
        ("ann-z",),
    ]
    assert changed_catalog == before
    with pytest.raises(ValidationError):
        results[0].decision = "not_suitable"  # type: ignore[misc]


def test_negative_results_can_be_excluded() -> None:
    assert (
        analyze_suitability(
            annotation_catalog("lemmatization", "prohibited"),
            SuitabilityRequest(task="lemmatization", include_negative=False),
        )
        == ()
    )


def test_lemmatization_accepts_documented_dictionary_route() -> None:
    result = analyze_suitability(
        dictionary_catalog("lemmatization", "permitted"),
        SuitabilityRequest(task="lemmatization"),
    )

    assert len(result) == 1
    assert result[0].decision == "suitable"
    assert result[0].annotation_ids == ()
