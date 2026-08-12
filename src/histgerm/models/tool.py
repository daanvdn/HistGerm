"""Tool model for the HistGerm V2 schema."""

from __future__ import annotations

from typing import Any

from pydantic import Field, HttpUrl, field_validator, model_validator

from .common import Access, BaseResource, LanguageStage, Task

__all__ = ["Tool"]

type MetricValue = int | float | str
type ReportedMetric = dict[str, MetricValue]


class Tool(BaseResource):
    """An NLP tool with controlled tasks, formats, and access metadata."""

    tasks: list[Task] = Field(min_length=1)
    supported_stages: list[LanguageStage] | None = None
    input_formats: list[str] | None = None
    output_formats: list[str] | None = None
    access: Access
    training_data: list[str] | None = None
    evaluation_data: list[str] | None = None
    reported_metrics: list[ReportedMetric] | None = None
    hugging_face_links: list[HttpUrl] | None = None
    note: str | None = None

    @field_validator("reported_metrics", mode="before")
    @classmethod
    def validate_reported_metrics(cls, metrics: Any) -> Any:
        """Validate compact metric mappings without adding a public model."""

        allowed = {"name", "value", "task", "dataset", "note"}
        required = {"name", "value"}
        for metric in metrics or []:
            if not isinstance(metric, dict):
                raise ValueError("each reported metric must be a mapping")
            keys = set(metric)
            if not required <= keys or not keys <= allowed:
                raise ValueError(
                    "each metric requires name/value and permits only task/dataset/note"
                )
            for key in ("name", "task", "dataset", "note"):
                if key in metric and not isinstance(metric[key], str):
                    raise ValueError(f"reported metric {key} must be a string")
            value = metric["value"]
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ValueError(
                    "reported metric value must be int, float, or string (not bool)"
                )
        return metrics

    @model_validator(mode="after")
    def validate_tool_sources(self) -> Tool:
        """Validate the tool's access evidence against local sources."""

        if not self.id.startswith("tool-"):
            raise ValueError("tool IDs must start with 'tool-'")
        self._validate_access_and_references(self.access)
        return self
