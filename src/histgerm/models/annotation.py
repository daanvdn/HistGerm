"""Independent annotation-layer models."""

from __future__ import annotations

from pydantic import Field, field_validator

from histgerm.models.common import (
    ExtensionData,
    HistGermModel,
    HttpUrlValue,
    KnowledgeValue,
    NonEmptyStr,
    ResponsibleParty,
    SelectionScope,
    StableId,
    VocabularyId,
)
from histgerm.models.corpus import SizeMeasurement
from histgerm.models.tool import EvaluationMetric


class AnnotationScheme(HistGermModel):
    name: NonEmptyStr
    version: KnowledgeValue[NonEmptyStr]
    tagset_url: KnowledgeValue[HttpUrlValue]
    description: KnowledgeValue[NonEmptyStr]


class SchemeMapping(HistGermModel):
    target_scheme: AnnotationScheme
    mapping_url: KnowledgeValue[HttpUrlValue]
    coverage: KnowledgeValue[NonEmptyStr]
    note: KnowledgeValue[NonEmptyStr]


class AnnotationLayer(HistGermModel):
    id: StableId
    task: VocabularyId
    scheme: KnowledgeValue[AnnotationScheme]
    scope: SelectionScope
    coverage_measurements: list[SizeMeasurement] = Field(default_factory=list)
    alignment_unit: KnowledgeValue[VocabularyId]
    production_method: KnowledgeValue[VocabularyId]
    quality: VocabularyId
    annotators: KnowledgeValue[list[ResponsibleParty]]
    guidelines_url: KnowledgeValue[HttpUrlValue]
    inter_annotator_agreement: KnowledgeValue[list[EvaluationMetric]]
    evaluation_results: KnowledgeValue[list[EvaluationMetric]]
    missing_value_convention: KnowledgeValue[NonEmptyStr]
    scheme_mappings: KnowledgeValue[list[SchemeMapping]]
    extensions: ExtensionData = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: StableId) -> StableId:
        if not value.startswith("ann-"):
            raise ValueError("annotation id must use the 'ann-' prefix")
        return value


__all__ = [
    "AnnotationLayer",
    "AnnotationScheme",
    "EvaluationMetric",
    "SchemeMapping",
]
