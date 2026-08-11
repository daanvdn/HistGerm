"""Explainable auxiliary-task suitability analysis."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import field_validator

from histgerm.models.access import (
    AvailabilityState,
    Distribution,
    LicenseStatus,
    PermissionState,
)
from histgerm.models.annotation import AnnotationLayer
from histgerm.models.catalog import Catalog
from histgerm.models.common import (
    HistGermModel,
    JsonPointer,
    KnownValue,
    NotApplicableValue,
    SelectionScope,
    StableId,
    VocabularyId,
)
from histgerm.models.corpus import Document
from histgerm.models.dictionary import SuitabilityAssessment
from histgerm.models.resource import Resource, ResourceVersion

type AuxiliaryTask = Literal[
    "lemmatization",
    "pos_tagging",
    "morphological_tagging",
    "dependency_parsing",
    "date_prediction",
    "dictionary_variant_learning",
]
type SuitabilityDecision = Literal["suitable", "review_required", "not_suitable"]

_TASK_TO_ANNOTATION = {
    "lemmatization": "lemma",
    "pos_tagging": "pos",
    "morphological_tagging": "morphology",
    "dependency_parsing": "dependencies",
}
_ACTIVE_AVAILABILITY = frozenset(
    {"available", "partially_available", "request_only", "authentication_required"}
)
_UNUSABLE_AVAILABILITY = frozenset(
    {
        "temporarily_unavailable",
        "discontinued",
        "inaccessible",
        "not_publicly_available",
    }
)
_DECISION_ORDER = {"suitable": 0, "review_required": 1, "not_suitable": 2}


class SuitabilityRequest(HistGermModel):
    """Immutable selection and policy controls for suitability analysis."""

    task: AuxiliaryTask
    language_stages: frozenset[VocabularyId] | None = None
    minimum_qualities: frozenset[VocabularyId] | None = None
    require_training_permission: bool = True
    include_negative: bool = True

    @field_validator("language_stages", "minimum_qualities", mode="before")
    @classmethod
    def copy_selections(cls, value: object) -> object:
        if isinstance(value, (set, list, tuple)):
            return frozenset(value)
        return value


class SuitabilityEvidence(HistGermModel):
    """Evidence references for one exact qualifying or limiting claim."""

    claim_path: JsonPointer
    evidence_ids: tuple[StableId, ...]


class DimensionValue(HistGermModel):
    """A compact coverage coordinate used by suitability snapshots."""

    dimension: Literal["language_stage", "annotation_type"]
    value_id: StableId | VocabularyId | None
    label: str
    knowledge_status: Literal["known", "unknown"]
    range_start: int | None = None
    range_end: int | None = None


class SuitabilityResult(HistGermModel):
    """An immutable explanation for one independently evaluated scope."""

    task: AuxiliaryTask
    decision: SuitabilityDecision
    resource_id: StableId
    version_id: StableId | None
    distribution_ids: tuple[StableId, ...]
    component_ids: tuple[StableId, ...]
    document_ids: tuple[StableId, ...] = ()
    annotation_ids: tuple[StableId, ...]
    quality_ids: tuple[VocabularyId, ...]
    coverage: tuple[DimensionValue, ...]
    reasons: tuple[str, ...]
    limitations: tuple[str, ...]
    availability: tuple[AvailabilityState, ...]
    license_statuses: tuple[LicenseStatus, ...]
    model_training_permissions: tuple[PermissionState, ...]
    evidence: tuple[SuitabilityEvidence, ...]


class _Evaluation:
    def __init__(self) -> None:
        self.positive: list[str] = []
        self.review: list[str] = []
        self.negative: list[str] = []
        self.required_paths: set[str] = set()

    def supported(self, code: str, path: str | None = None) -> None:
        self.positive.append(code)
        if path is not None:
            self.required_paths.add(path)

    def uncertain(self, code: str) -> None:
        self.review.append(code)

    def disqualify(self, code: str) -> None:
        self.negative.append(code)

    @property
    def decision(self) -> SuitabilityDecision:
        if self.negative:
            return "not_suitable"
        if self.review:
            return "review_required"
        return "suitable"


def _scope_ids(
    scope: SelectionScope,
) -> tuple[
    tuple[StableId, ...],
    tuple[StableId, ...],
    tuple[StableId, ...],
]:
    return (
        tuple(sorted(scope.component_ids)),
        tuple(sorted(scope.document_ids)),
        tuple(sorted(scope.annotation_ids)),
    )


def _scope_covers(container: SelectionScope, selected: SelectionScope) -> bool:
    for field in ("component_ids", "document_ids", "annotation_ids"):
        container_ids = getattr(container, field)
        selected_ids = getattr(selected, field)
        if container_ids and selected_ids and not selected_ids <= container_ids:
            return False
    return True


def _stages_for_scope(
    resource: Resource,
    version: ResourceVersion,
    scope: SelectionScope,
) -> tuple[frozenset[VocabularyId] | None, str]:
    document_stages = [
        item.language_stage_ids.value
        for item in version.documents
        if item.id in scope.document_ids
        and isinstance(item.language_stage_ids, KnownValue)
    ]
    if document_stages:
        return frozenset().union(*document_stages), "document"
    component_stages = [
        item.language_stage_ids.value
        for item in version.components
        if item.id in scope.component_ids
        and isinstance(item.language_stage_ids, KnownValue)
    ]
    if component_stages:
        return frozenset().union(*component_stages), "component"
    if isinstance(version.language_stage_ids, KnownValue):
        return version.language_stage_ids.value, "version"
    if isinstance(resource.language_stage_ids, KnownValue):
        return resource.language_stage_ids.value, "resource"
    return None, "unknown"


def _coverage(
    stages: frozenset[VocabularyId] | None,
    annotation_task: VocabularyId | None,
) -> tuple[DimensionValue, ...]:
    values = [
        DimensionValue(
            dimension="language_stage",
            value_id=stage,
            label=stage,
            knowledge_status="known",
        )
        for stage in sorted(stages or ())
    ]
    if stages is None:
        values.append(
            DimensionValue(
                dimension="language_stage",
                value_id=None,
                label="Unknown language stage",
                knowledge_status="unknown",
            )
        )
    if annotation_task is not None:
        values.append(
            DimensionValue(
                dimension="annotation_type",
                value_id=annotation_task,
                label=annotation_task,
                knowledge_status="known",
            )
        )
    return tuple(values)


def _evidence(
    resource: Resource,
    paths: Iterable[str],
    extra: Iterable[tuple[str, Iterable[StableId]]] = (),
) -> tuple[SuitabilityEvidence, ...]:
    references: dict[str, set[StableId]] = {}
    for path in paths:
        ids = resource.claims.get(path)
        if ids:
            references.setdefault(path, set()).update(ids)
    for path, extra_ids in extra:
        materialized = tuple(extra_ids)
        if materialized:
            references.setdefault(path, set()).update(materialized)
    return tuple(
        SuitabilityEvidence(
            claim_path=path,
            evidence_ids=tuple(sorted(references[path])),
        )
        for path in sorted(references)
    )


def _require_evidence(
    evaluation: _Evaluation,
    resource: Resource,
    extra_paths: frozenset[str] = frozenset(),
) -> None:
    missing = sorted(
        path
        for path in evaluation.required_paths
        if path not in resource.claims and path not in extra_paths
    )
    if missing:
        evaluation.uncertain(
            "EVIDENCE_MISSING: qualifying claims lack evidence: " + ", ".join(missing)
        )


def _evaluate_stages(
    evaluation: _Evaluation,
    request: SuitabilityRequest,
    stages: frozenset[VocabularyId] | None,
) -> None:
    if request.language_stages is None:
        return
    if stages is None:
        evaluation.uncertain("SCOPE_LANGUAGE_STAGE_UNKNOWN: coverage is unknown")
    elif stages.isdisjoint(request.language_stages):
        evaluation.disqualify(
            "SCOPE_LANGUAGE_STAGE_MISMATCH: selected stages are not covered"
        )
    else:
        evaluation.supported("SCOPE_LANGUAGE_STAGE_MATCH: selected stage is covered")


def _matching_distributions(
    version: ResourceVersion, scope: SelectionScope
) -> tuple[Distribution, ...]:
    return tuple(
        item for item in version.distributions if _scope_covers(item.scope, scope)
    )


def _evaluate_distributions(
    evaluation: _Evaluation,
    version: ResourceVersion,
    distributions: tuple[Distribution, ...],
    version_index: int,
) -> None:
    if not distributions:
        evaluation.disqualify(
            "ACCESS_DISTRIBUTION_ABSENT: no distribution covers this scope"
        )
        return
    states = {item.availability for item in distributions}
    if states & _ACTIVE_AVAILABILITY:
        evaluation.supported("ACCESS_TECHNICALLY_AVAILABLE: a distribution is usable")
    elif states == {"unknown"}:
        evaluation.uncertain("ACCESS_UNKNOWN: distribution availability is unknown")
    elif states <= _UNUSABLE_AVAILABILITY:
        evaluation.disqualify(
            "ACCESS_UNUSABLE: every covering distribution is unavailable"
        )
    else:
        evaluation.uncertain("ACCESS_REVIEW_REQUIRED: access state requires review")

    for distribution_index, distribution in enumerate(version.distributions):
        if distribution not in distributions:
            continue
        prefix = f"/versions/{version_index}/distributions/{distribution_index}"
        evaluation.required_paths.add(f"{prefix}/availability")
        evaluation.required_paths.add(f"{prefix}/license/status")
        evaluation.required_paths.add(f"{prefix}/access/model_training")


def _evaluate_permissions(
    evaluation: _Evaluation,
    permissions: tuple[PermissionState, ...],
    required: bool,
) -> None:
    if not required:
        return
    if "prohibited" in permissions:
        evaluation.disqualify(
            "PERMISSION_PROHIBITED: model training is explicitly prohibited"
        )
    elif permissions and set(permissions) == {"permitted"}:
        evaluation.supported(
            "PERMISSION_PERMITTED: model training is explicitly permitted"
        )
    else:
        evaluation.uncertain(
            "PERMISSION_UNCLEAR: model-training permission is not explicitly permitted"
        )


def _evaluate_quality(
    evaluation: _Evaluation,
    request: SuitabilityRequest,
    qualities: tuple[VocabularyId, ...],
) -> None:
    if request.minimum_qualities is None:
        return
    if not qualities or "unknown" in qualities:
        evaluation.uncertain("QUALITY_UNKNOWN: supervision quality is unknown")
    elif request.minimum_qualities.isdisjoint(qualities):
        evaluation.disqualify(
            "QUALITY_NOT_ACCEPTED: quality is outside the accepted values"
        )
    else:
        evaluation.supported("QUALITY_ACCEPTED: supervision quality is accepted")


def _annotation_result(
    resource: Resource,
    version: ResourceVersion,
    version_index: int,
    annotation: AnnotationLayer,
    annotation_index: int,
    request: SuitabilityRequest,
) -> SuitabilityResult:
    evaluation = _Evaluation()
    prefix = f"/versions/{version_index}/annotations/{annotation_index}"
    required_task = _TASK_TO_ANNOTATION[request.task]
    evaluation.supported(
        f"FEATURE_{required_task.upper()}: required annotation layer exists",
        f"{prefix}/task",
    )
    evaluation.supported("SCOPE_EXPLICIT: annotation scope is explicit")

    alignment = (
        annotation.alignment_unit.value
        if isinstance(annotation.alignment_unit, KnownValue)
        else None
    )
    if alignment in {"token", "orthographic_word"}:
        evaluation.supported(
            f"ALIGNMENT_SUPPORTED: annotation aligns to {alignment}",
            f"{prefix}/alignment_unit/value",
        )
    elif alignment is None:
        evaluation.uncertain("ALIGNMENT_UNKNOWN: annotation alignment is unknown")
    else:
        evaluation.disqualify(
            f"ALIGNMENT_INCOMPATIBLE: {alignment} cannot supply token/word labels"
        )

    if request.task in {
        "pos_tagging",
        "morphological_tagging",
        "dependency_parsing",
    }:
        if isinstance(annotation.scheme, KnownValue):
            scheme = annotation.scheme.value
            evaluation.supported(
                "SCHEME_DOCUMENTED: annotation scheme is identified",
                f"{prefix}/scheme/value/name",
            )
            if isinstance(scheme.version, KnownValue):
                evaluation.supported(
                    "SCHEME_VERSION_DOCUMENTED: scheme version is identified",
                    f"{prefix}/scheme/value/version/value",
                )
            else:
                evaluation.uncertain(
                    "SCHEME_VERSION_UNKNOWN: annotation scheme version is unknown"
                )
        else:
            evaluation.uncertain("SCHEME_UNKNOWN: annotation scheme is unknown")

    if request.task == "dependency_parsing":
        corpus = resource.corpus
        if isinstance(corpus, KnownValue) and isinstance(
            corpus.value.sentence_segmentation_convention, KnownValue
        ):
            evaluation.supported(
                "SENTENCE_ALIGNMENT_DOCUMENTED: sentence boundaries are documented",
                "/corpus/value/sentence_segmentation_convention/value",
            )
        else:
            evaluation.uncertain(
                "SENTENCE_ALIGNMENT_UNKNOWN: sentence boundaries are unknown"
            )

    quality = (annotation.quality,)
    evaluation.required_paths.add(f"{prefix}/quality")
    _evaluate_quality(evaluation, request, quality)
    stages, _ = _stages_for_scope(resource, version, annotation.scope)
    _evaluate_stages(evaluation, request, stages)
    distributions = _matching_distributions(version, annotation.scope)
    _evaluate_distributions(evaluation, version, distributions, version_index)
    permissions = tuple(sorted({item.access.model_training for item in distributions}))
    _evaluate_permissions(evaluation, permissions, request.require_training_permission)
    _require_evidence(evaluation, resource)

    component_ids, document_ids, annotation_ids = _scope_ids(annotation.scope)
    if not annotation_ids:
        annotation_ids = (annotation.id,)
    return _result(
        request=request,
        evaluation=evaluation,
        resource=resource,
        version=version,
        distributions=distributions,
        component_ids=component_ids,
        document_ids=document_ids,
        annotation_ids=annotation_ids,
        quality_ids=quality,
        stages=stages,
        annotation_task=annotation.task,
        evidence=_evidence(resource, evaluation.required_paths),
    )


def _missing_annotation_result(
    resource: Resource,
    version: ResourceVersion,
    request: SuitabilityRequest,
) -> SuitabilityResult:
    evaluation = _Evaluation()
    required = _TASK_TO_ANNOTATION[request.task]
    evaluation.disqualify(
        f"FEATURE_{required.upper()}_ABSENT: required annotation layer is absent"
    )
    stages, _ = _stages_for_scope(
        resource,
        version,
        SelectionScope(
            resource_ids=frozenset({resource.id}),
            version_ids=frozenset({version.id}),
            filter=NotApplicableValue(status="not_applicable"),
        ),
    )
    _evaluate_stages(evaluation, request, stages)
    return _result(
        request=request,
        evaluation=evaluation,
        resource=resource,
        version=version,
        stages=stages,
    )


def _date_result(
    resource: Resource,
    version: ResourceVersion,
    version_index: int,
    document: Document,
    document_index: int,
    request: SuitabilityRequest,
) -> SuitabilityResult:
    evaluation = _Evaluation()
    prefix = f"/versions/{version_index}/documents/{document_index}"
    chronology = document.chronology
    if isinstance(chronology, KnownValue):
        value = chronology.value
        date_fields = (
            ("earliest_year", value.earliest_year),
            ("latest_year", value.latest_year),
            ("label", value.label),
        )
        known_date_fields = [
            name for name, item in date_fields if isinstance(item, KnownValue)
        ]
        date_known = bool(known_date_fields)
        if date_known:
            evaluation.positive.append(
                "DATE_LABEL_DOCUMENTED: document date or range is explicit"
            )
            evaluation.required_paths.update(
                f"{prefix}/chronology/value/{name}/value" for name in known_date_fields
            )
        else:
            evaluation.uncertain("DATE_LABEL_UNKNOWN: document date is unknown")
        if isinstance(value.dating_method, KnownValue):
            evaluation.supported(
                "DATING_METHOD_DOCUMENTED: dating method is explicit",
                f"{prefix}/chronology/value/dating_method/value",
            )
        else:
            evaluation.uncertain("DATING_METHOD_UNKNOWN: dating method is unknown")
        if isinstance(value.certainty, KnownValue):
            evaluation.supported(
                "DATE_CERTAINTY_DOCUMENTED: dating certainty is explicit",
                f"{prefix}/chronology/value/certainty/value",
            )
        else:
            evaluation.uncertain("DATE_CERTAINTY_UNKNOWN: dating certainty is unknown")
    else:
        evaluation.uncertain("DATE_LABEL_UNKNOWN: document chronology is unknown")
    evaluation.supported("STABLE_DOCUMENT_ID: dating target has a stable document ID")

    scope = SelectionScope(
        resource_ids=frozenset({resource.id}),
        version_ids=frozenset({version.id}),
        component_ids=document.component_ids,
        document_ids=frozenset({document.id}),
        filter=NotApplicableValue(status="not_applicable"),
    )
    stages, _ = _stages_for_scope(resource, version, scope)
    _evaluate_stages(evaluation, request, stages)
    _evaluate_quality(evaluation, request, ())
    distributions = _matching_distributions(version, scope)
    _evaluate_distributions(evaluation, version, distributions, version_index)
    permissions = tuple(sorted({item.access.model_training for item in distributions}))
    _evaluate_permissions(evaluation, permissions, request.require_training_permission)
    _require_evidence(evaluation, resource)
    return _result(
        request=request,
        evaluation=evaluation,
        resource=resource,
        version=version,
        distributions=distributions,
        component_ids=tuple(sorted(document.component_ids)),
        document_ids=(document.id,),
        stages=stages,
        evidence=_evidence(resource, evaluation.required_paths),
    )


def _dictionary_result(
    resource: Resource,
    version: ResourceVersion | None,
    assessment: SuitabilityAssessment | None,
    assessment_index: int | None,
    request: SuitabilityRequest,
) -> SuitabilityResult:
    evaluation = _Evaluation()
    if not isinstance(resource.dictionary, KnownValue):
        raise TypeError("dictionary result requires a known dictionary profile")
    profile = resource.dictionary.value
    scope = (
        assessment.scope
        if assessment is not None
        else SelectionScope(
            resource_ids=frozenset({resource.id}),
            version_ids=(
                frozenset({version.id}) if version is not None else frozenset()
            ),
            filter=NotApplicableValue(status="not_applicable"),
        )
    )
    extra: list[tuple[str, Iterable[StableId]]] = []
    extra_paths: set[str] = set()

    if assessment is None:
        evaluation.disqualify(
            "SUPERVISION_ASSESSMENT_ABSENT: no task-specific assessment exists"
        )
    else:
        prefix = f"/dictionary/value/supervision_suitability/value/{assessment_index}"
        if assessment.status == "not_suitable":
            evaluation.disqualify(
                "SUPERVISION_EXPLICITLY_NOT_SUITABLE: assessment rejects the task"
            )
        elif assessment.status == "review_required":
            evaluation.uncertain(
                "SUPERVISION_REVIEW_REQUIRED: assessment records unresolved issues"
            )
        elif assessment.status == "suitable":
            evaluation.supported(
                "SUPERVISION_DOCUMENTED: task-specific supervision is documented",
                f"{prefix}/status",
            )
        else:
            evaluation.uncertain(
                "SUPERVISION_STATUS_UNKNOWN: assessment status is unrecognized"
            )
        extra.append((f"{prefix}/status", assessment.evidence_ids))
        if assessment.evidence_ids:
            extra_paths.add(f"{prefix}/status")

    features = profile.lexical_features
    headword = isinstance(features.headwords, KnownValue) and features.headwords.value
    lemma = isinstance(features.lemmas, KnownValue) and features.lemmas.value
    variants = (
        isinstance(features.spelling_variants, KnownValue)
        and features.spelling_variants.value
    )
    if headword or lemma:
        target_path = (
            "/dictionary/value/lexical_features/lemmas/value"
            if lemma
            else "/dictionary/value/lexical_features/headwords/value"
        )
        evaluation.supported(
            "LEXICAL_TARGET_PRESENT: lemma or headword feature is explicit",
            target_path,
        )
    elif isinstance(features.headwords, KnownValue) and isinstance(
        features.lemmas, KnownValue
    ):
        evaluation.disqualify(
            "LEXICAL_TARGET_ABSENT: neither lemmas nor headwords are represented"
        )
    else:
        evaluation.uncertain(
            "LEXICAL_TARGET_UNKNOWN: lemma/headword representation is unknown"
        )
    if variants:
        evaluation.supported(
            "HISTORICAL_VARIANTS_PRESENT: spelling variants are explicit",
            "/dictionary/value/lexical_features/spelling_variants/value",
        )
    elif isinstance(features.spelling_variants, KnownValue):
        evaluation.disqualify(
            "HISTORICAL_VARIANTS_ABSENT: spelling variants are explicitly absent"
        )
    else:
        evaluation.uncertain(
            "HISTORICAL_VARIANTS_UNKNOWN: spelling variant coverage is unknown"
        )
    if isinstance(profile.download_formats, KnownValue):
        evaluation.supported(
            "REPRESENTATION_DOCUMENTED: extraction format is documented",
            "/dictionary/value/download_formats/value/0/format_id",
        )
    else:
        evaluation.uncertain(
            "REPRESENTATION_UNKNOWN: machine-readable representation is unknown"
        )

    availability = profile.machine_readable_availability
    if availability in _ACTIVE_AVAILABILITY:
        evaluation.supported(
            "MACHINE_READABLE_ACCESS: machine-readable data is accessible",
            "/dictionary/value/machine_readable_availability",
        )
    elif availability in _UNUSABLE_AVAILABILITY:
        evaluation.disqualify(
            "MACHINE_READABLE_ACCESS_UNUSABLE: reusable data is inaccessible"
        )
    else:
        evaluation.uncertain(
            "MACHINE_READABLE_ACCESS_UNKNOWN: machine-readable access is unclear"
        )

    stages = (
        profile.source_language_stage_ids.value
        if isinstance(profile.source_language_stage_ids, KnownValue)
        else None
    )
    _evaluate_stages(evaluation, request, stages)
    quality = (
        (assessment.quality.value,)
        if assessment is not None and isinstance(assessment.quality, KnownValue)
        else ()
    )
    _evaluate_quality(evaluation, request, quality)
    permissions = (
        (assessment.model_training_permission,) if assessment is not None else ()
    )
    _evaluate_permissions(evaluation, permissions, request.require_training_permission)
    distributions = (
        _matching_distributions(version, scope) if version is not None else ()
    )
    if version is not None:
        for distribution_index, distribution in enumerate(version.distributions):
            if distribution in distributions:
                evaluation.required_paths.add(
                    f"/versions/{resource.versions.index(version)}/"
                    f"distributions/{distribution_index}/license/status"
                )
    _require_evidence(evaluation, resource, frozenset(extra_paths))

    component_ids, document_ids, annotation_ids = _scope_ids(scope)
    return _result(
        request=request,
        evaluation=evaluation,
        resource=resource,
        version=version,
        distributions=distributions,
        component_ids=component_ids,
        document_ids=document_ids,
        annotation_ids=annotation_ids,
        quality_ids=quality,
        stages=stages,
        availability=(availability,),
        permissions=permissions,
        evidence=_evidence(resource, evaluation.required_paths, extra),
    )


def _result(
    *,
    request: SuitabilityRequest,
    evaluation: _Evaluation,
    resource: Resource,
    version: ResourceVersion | None,
    distributions: tuple[Distribution, ...] = (),
    component_ids: tuple[StableId, ...] = (),
    document_ids: tuple[StableId, ...] = (),
    annotation_ids: tuple[StableId, ...] = (),
    quality_ids: tuple[VocabularyId, ...] = (),
    stages: frozenset[VocabularyId] | None = None,
    annotation_task: VocabularyId | None = None,
    availability: tuple[AvailabilityState, ...] | None = None,
    permissions: tuple[PermissionState, ...] | None = None,
    evidence: tuple[SuitabilityEvidence, ...] = (),
) -> SuitabilityResult:
    reasons = tuple(sorted(set(evaluation.positive)))
    limitations = tuple(sorted(set((*evaluation.review, *evaluation.negative))))
    return SuitabilityResult(
        task=request.task,
        decision=evaluation.decision,
        resource_id=resource.id,
        version_id=version.id if version is not None else None,
        distribution_ids=tuple(sorted(item.id for item in distributions)),
        component_ids=tuple(sorted(set(component_ids))),
        document_ids=tuple(sorted(set(document_ids))),
        annotation_ids=tuple(sorted(set(annotation_ids))),
        quality_ids=tuple(sorted(set(quality_ids))),
        coverage=_coverage(stages, annotation_task),
        reasons=reasons,
        limitations=limitations,
        availability=(
            tuple(sorted({item.availability for item in distributions}))
            if availability is None
            else tuple(sorted(set(availability)))
        ),
        license_statuses=tuple(sorted({item.license.status for item in distributions})),
        model_training_permissions=(
            tuple(sorted({item.access.model_training for item in distributions}))
            if permissions is None
            else tuple(sorted(set(permissions)))
        ),
        evidence=evidence,
    )


def _dictionary_results(
    resource: Resource, request: SuitabilityRequest
) -> list[SuitabilityResult]:
    if not isinstance(resource.dictionary, KnownValue):
        return []
    assessments_value = resource.dictionary.value.supervision_suitability
    assessments = (
        assessments_value.value if isinstance(assessments_value, KnownValue) else []
    )
    matching = [
        (index, item)
        for index, item in enumerate(assessments)
        if item.task == request.task
    ]
    versions: Iterable[ResourceVersion | None] = (
        resource.versions if resource.versions else (None,)
    )
    results: list[SuitabilityResult] = []
    for dictionary_version in versions:
        scoped = [
            (index, item)
            for index, item in matching
            if dictionary_version is None
            or not item.scope.version_ids
            or dictionary_version.id in item.scope.version_ids
        ]
        if scoped:
            results.extend(
                _dictionary_result(
                    resource,
                    dictionary_version,
                    item,
                    index,
                    request,
                )
                for index, item in scoped
            )
        else:
            results.append(
                _dictionary_result(
                    resource,
                    dictionary_version,
                    None,
                    None,
                    request,
                )
            )
    return results


def analyze_suitability(
    catalog: Catalog, request: SuitabilityRequest
) -> tuple[SuitabilityResult, ...]:
    """Evaluate every evidence-bearing candidate without mutating the catalog."""

    results: list[SuitabilityResult] = []
    for resource in catalog.resources:
        if request.task in _TASK_TO_ANNOTATION:
            for version_index, version in enumerate(resource.versions):
                matches = [
                    (index, annotation)
                    for index, annotation in enumerate(version.annotations)
                    if annotation.task == _TASK_TO_ANNOTATION[request.task]
                ]
                if matches:
                    results.extend(
                        _annotation_result(
                            resource,
                            version,
                            version_index,
                            annotation,
                            annotation_index,
                            request,
                        )
                        for annotation_index, annotation in matches
                    )
                else:
                    if not (
                        request.task == "lemmatization"
                        and isinstance(resource.dictionary, KnownValue)
                    ):
                        results.append(
                            _missing_annotation_result(resource, version, request)
                        )
            if request.task == "lemmatization":
                results.extend(_dictionary_results(resource, request))
        elif request.task == "date_prediction":
            for version_index, version in enumerate(resource.versions):
                if version.documents:
                    results.extend(
                        _date_result(
                            resource,
                            version,
                            version_index,
                            document,
                            document_index,
                            request,
                        )
                        for document_index, document in enumerate(version.documents)
                    )
                else:
                    evaluation = _Evaluation()
                    evaluation.disqualify(
                        "DOCUMENT_DATES_ABSENT: no document-level dating labels exist"
                    )
                    results.append(
                        _result(
                            request=request,
                            evaluation=evaluation,
                            resource=resource,
                            version=version,
                        )
                    )
        else:
            results.extend(_dictionary_results(resource, request))

    if not request.include_negative:
        results = [item for item in results if item.decision != "not_suitable"]
    return tuple(
        sorted(
            results,
            key=lambda item: (
                _DECISION_ORDER[item.decision],
                item.resource_id,
                item.version_id or "",
                item.annotation_ids,
                item.document_ids,
            ),
        )
    )


def for_auxiliary_task(
    catalog: Catalog, request: SuitabilityRequest
) -> tuple[SuitabilityResult, ...]:
    """Compatibility function matching the approved query-method name."""

    return analyze_suitability(catalog, request)


__all__ = [
    "AuxiliaryTask",
    "DimensionValue",
    "SuitabilityDecision",
    "SuitabilityEvidence",
    "SuitabilityRequest",
    "SuitabilityResult",
    "analyze_suitability",
    "for_auxiliary_task",
]
