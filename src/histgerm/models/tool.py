"""Tool/model category profile."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import field_validator, model_validator

from histgerm.models.access import (
    ApiInterface,
    CliInterface,
    HuggingFaceReference,
    LicenseDescription,
)
from histgerm.models.common import (
    HistGermModel,
    HttpUrlValue,
    KnowledgeValue,
    KnownValue,
    NonEmptyStr,
    SelectionScope,
    StableId,
    VocabularyId,
)


def _known_set(value: Any) -> Any:
    if isinstance(value, Mapping) and value.get("status") == "known":
        inner = value.get("value")
        if isinstance(inner, list):
            return {**value, "value": frozenset(inner)}
    return value


class DatasetUse(HistGermModel):
    resource_id: KnowledgeValue[StableId]
    external_name: KnowledgeValue[NonEmptyStr]
    scope: KnowledgeValue[SelectionScope]
    note: KnowledgeValue[NonEmptyStr]

    @model_validator(mode="after")
    def validate_identity(self) -> DatasetUse:
        resource_known = isinstance(self.resource_id, KnownValue)
        external_known = isinstance(self.external_name, KnownValue)
        if resource_known == external_known:
            raise ValueError(
                "dataset use requires exactly one known resource_id or external_name"
            )
        if isinstance(self.resource_id, KnownValue) and not (
            self.resource_id.value.startswith("res-")
        ):
            raise ValueError("dataset resource_id must use the 'res-' prefix")
        return self


class EvaluationMetric(HistGermModel):
    name: NonEmptyStr
    value: KnowledgeValue[float]
    scale: KnowledgeValue[NonEmptyStr]
    task: KnowledgeValue[VocabularyId]
    dataset: KnowledgeValue[DatasetUse]
    scope: KnowledgeValue[SelectionScope]
    note: KnowledgeValue[NonEmptyStr]


class ToolProfile(HistGermModel):
    supported_tasks: KnowledgeValue[frozenset[VocabularyId]]
    input_formats: KnowledgeValue[frozenset[VocabularyId]]
    output_formats: KnowledgeValue[frozenset[VocabularyId]]
    language_stage_ids: KnowledgeValue[frozenset[VocabularyId]]
    implementation_languages: KnowledgeValue[frozenset[NonEmptyStr]]
    frameworks: KnowledgeValue[frozenset[NonEmptyStr]]
    model_architecture: KnowledgeValue[NonEmptyStr]
    training_data: KnowledgeValue[list[DatasetUse]]
    evaluation_data: KnowledgeValue[list[DatasetUse]]
    reported_metrics: KnowledgeValue[list[EvaluationMetric]]
    installation_url: KnowledgeValue[HttpUrlValue]
    usage_url: KnowledgeValue[HttpUrlValue]
    package_name: KnowledgeValue[NonEmptyStr]
    cli: KnowledgeValue[CliInterface]
    api: KnowledgeValue[ApiInterface]
    hugging_face: KnowledgeValue[HuggingFaceReference]
    software_license: LicenseDescription
    model_license: LicenseDescription
    maintenance_status: KnowledgeValue[VocabularyId]

    @field_validator(
        "supported_tasks",
        "input_formats",
        "output_formats",
        "language_stage_ids",
        "implementation_languages",
        "frameworks",
        mode="before",
    )
    @classmethod
    def accept_yaml_sets(cls, value: Any) -> Any:
        return _known_set(value)


__all__ = ["DatasetUse", "EvaluationMetric", "ToolProfile"]
