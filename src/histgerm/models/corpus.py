"""Corpus models for the HistGerm V2 schema."""

from __future__ import annotations

from datetime import date

from pydantic import Field, HttpUrl, field_validator, model_validator

from .common import (
    LINK_PURPOSE_RE,
    STABLE_ID_PATTERN,
    Access,
    AnnotationQuality,
    AnnotationType,
    Availability,
    BaseResource,
    LanguageStage,
    Overlap,
    ProductionMethod,
    Size,
    _check_source_ids,
    _require_unique,
    _StrictModel,
)

__all__ = [
    "AnnotationLayer",
    "Corpus",
    "CorpusText",
    "CorpusVersion",
]


class AnnotationLayer(_StrictModel):
    """A version-local annotation layer available to corpus texts."""

    id: str = Field(pattern=STABLE_ID_PATTERN)
    type: AnnotationType
    tagset_name: str | None = None
    tagset_version: str | None = None
    tagset_link: HttpUrl | None = None
    quality: AnnotationQuality | None = None
    production_method: ProductionMethod | None = None
    source_ids: list[str] = Field(min_length=1)
    note: str | None = None

    @model_validator(mode="after")
    def explain_other(self) -> AnnotationLayer:
        """Require an explanation for project-specific annotation types."""

        if (
            self.type is AnnotationType.OTHER
            and self.tagset_name is None
            and self.note is None
        ):
            raise ValueError("annotation type 'other' requires tagset_name or note")
        return self


class CorpusText(_StrictModel):
    """An inline corpus text with scholarly metadata and layer references."""

    id: str = Field(pattern=STABLE_ID_PATTERN)
    title: str
    shared_work_id: str | None = Field(default=None, pattern=STABLE_ID_PATTERN)
    stages: list[LanguageStage] = Field(min_length=1)
    dialect: str
    date: str
    annotation_ids: list[str]
    authors: list[str] | None = None
    genres: list[str] | None = None
    text_types: list[str] | None = None
    regions: list[str] | None = None
    mixed_languages: list[str] | None = None
    code_switching: bool | None = None
    witness_id: str | None = None
    witness_note: str | None = None
    edition_id: str | None = None
    edition_note: str | None = None
    sizes: list[Size] | None = None
    source_ids: list[str] = Field(min_length=1)
    overlaps: list[Overlap] | None = None
    note: str | None = None

    @model_validator(mode="after")
    def validate_local_collections(self) -> CorpusText:
        """Validate unique layer references and text-scoped overlaps."""

        _require_unique(self.annotation_ids, "CorpusText.annotation_ids")
        for overlap in self.overlaps or []:
            overlap.validate_scope("text")
        return self


class CorpusVersion(_StrictModel):
    """A corpus release containing inline texts and annotation layers."""

    id: str = Field(pattern=STABLE_ID_PATTERN)
    label: str | None = None
    released_on: date | None = None
    links: dict[str, HttpUrl] | None = None
    license: str | None = None
    availability: list[Availability] = Field(min_length=1)
    sizes: list[Size] | None = None
    annotations: list[AnnotationLayer]
    texts: list[CorpusText] = Field(min_length=1)
    source_ids: list[str] | None = None
    note: str | None = None

    @field_validator("links")
    @classmethod
    def validate_link_purposes(
        cls, value: dict[str, HttpUrl] | None
    ) -> dict[str, HttpUrl] | None:
        """Require descriptive lower-snake-case keys for version links."""

        if value is not None:
            for purpose in value:
                if not LINK_PURPOSE_RE.fullmatch(purpose):
                    raise ValueError("link purposes must use lower_snake_case")
        return value

    @model_validator(mode="after")
    def validate_version_scope(self) -> CorpusVersion:
        """Validate version-local layer, text, and annotation references."""

        layer_ids = [layer.id for layer in self.annotations]
        text_ids = [text.id for text in self.texts]
        _require_unique(layer_ids, "annotation layer IDs within a version")
        _require_unique(text_ids, "text IDs within a version")
        known_layers = set(layer_ids)
        for text in self.texts:
            unknown = set(text.annotation_ids) - known_layers
            if unknown:
                raise ValueError(
                    f"text {text.id!r} references unknown annotation IDs "
                    f"{sorted(unknown)!r}"
                )
        return self


class Corpus(BaseResource):
    """A corpus resource with versions, texts, access, and overlaps."""

    access: Access
    versions: list[CorpusVersion] = Field(min_length=1)
    overlaps: list[Overlap] | None = None
    notes: list[str] | None = None

    @model_validator(mode="after")
    def validate_corpus_scope(self) -> Corpus:
        """Validate all corpus-local identifiers and source references."""

        sources = self._validate_access_and_references(self.access)
        _require_unique([version.id for version in self.versions], "corpus version IDs")
        all_text_ids: list[str] = []
        for overlap in self.overlaps or []:
            overlap.validate_scope("corpus")
            _check_source_ids(overlap.source_ids, sources, "Corpus.overlaps.source_ids")
        for version in self.versions:
            _check_source_ids(version.source_ids, sources, "CorpusVersion.source_ids")
            for layer in version.annotations:
                _check_source_ids(
                    layer.source_ids, sources, "AnnotationLayer.source_ids"
                )
            for text in version.texts:
                all_text_ids.append(text.id)
                _check_source_ids(text.source_ids, sources, "CorpusText.source_ids")
                for overlap in text.overlaps or []:
                    _check_source_ids(
                        overlap.source_ids, sources, "CorpusText.overlaps.source_ids"
                    )
        _require_unique(all_text_ids, "text IDs across all corpus versions")
        own_texts = {f"{self.id}:{text_id}" for text_id in all_text_ids}
        for version in self.versions:
            for text in version.texts:
                for overlap in text.overlaps or []:
                    if (
                        overlap.with_.startswith(f"{self.id}:")
                        and overlap.with_ not in own_texts
                    ):
                        raise ValueError(
                            f"text {text.id!r} overlap references unknown local "
                            f"text {overlap.with_!r}"
                        )
        return self

    def validate_inventory_references(
        self, corpus_ids: set[str], qualified_text_ids: set[str]
    ) -> None:
        """Resolve non-external overlap targets against the full inventory."""

        for overlap in self.overlaps or []:
            if (
                not overlap.with_.startswith("external:")
                and overlap.with_ not in corpus_ids
            ):
                raise ValueError(f"unknown corpus overlap target {overlap.with_!r}")
        for version in self.versions:
            for text in version.texts:
                for overlap in text.overlaps or []:
                    if (
                        not overlap.with_.startswith("external:")
                        and overlap.with_ not in qualified_text_ids
                    ):
                        raise ValueError(
                            f"unknown text overlap target {overlap.with_!r}"
                        )
