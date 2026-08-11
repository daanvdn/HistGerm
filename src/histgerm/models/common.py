"""Shared enums, fields, and models for the HistGerm V2 schema."""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

__all__ = [
    "Access",
    "AnnotationQuality",
    "AnnotationType",
    "Availability",
    "BaseResource",
    "LanguageStage",
    "LegalPermission",
    "Overlap",
    "OverlapRelationship",
    "ProductionMethod",
    "Size",
    "SizeUnit",
    "Source",
    "Task",
]

STABLE_ID_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
STABLE_ID_RE = re.compile(STABLE_ID_PATTERN)
QUALIFIED_TEXT_RE = re.compile(
    rf"^(?P<corpus>{STABLE_ID_PATTERN[1:-1]}):(?P<text>{STABLE_ID_PATTERN[1:-1]})$"
)
EXTERNAL_CORPUS_RE = re.compile(rf"^external:(?P<corpus>{STABLE_ID_PATTERN[1:-1]})$")
EXTERNAL_TEXT_RE = re.compile(
    rf"^external:(?P<corpus>{STABLE_ID_PATTERN[1:-1]}):"
    rf"(?P<text>{STABLE_ID_PATTERN[1:-1]})$"
)
SUPPORT_RE = re.compile(
    r"^(?:access|aliases|annotations|citation_detail|corpus_links|"
    r"covered_languages|covered_stages|description|download_links|"
    r"evaluation_data|hugging_face_links|id|input_formats|lexical_features|"
    r"links|machine_readable|name|note|notes|output_formats|overlaps|"
    r"reported_metrics|reviewed_on|search_links|sources|supported_stages|"
    r"tasks|texts|training_data|versions)"
    r"(?:\.[a-z0-9]+(?:[-_][a-z0-9]+)*)*$"
)
LINK_PURPOSE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class LanguageStage(StrEnum):
    """Historical German language stages represented by HistGerm."""

    OHG = "ohg"
    MHG = "mhg"
    ENHG = "enhg"


class LegalPermission(StrEnum):
    """Evidence-backed legal permission states."""

    PERMITTED = "permitted"
    PROHIBITED = "prohibited"
    UNCLEAR = "unclear"


class Availability(StrEnum):
    """Ways in which a resource or corpus version is available."""

    DESCRIBED = "described"
    BROWSABLE = "browsable"
    DOWNLOADABLE = "downloadable"
    API = "api"
    REQUEST_ONLY = "request_only"
    AUTHENTICATION_REQUIRED = "authentication_required"
    UNAVAILABLE = "unavailable"
    DISCONTINUED = "discontinued"


class AnnotationType(StrEnum):
    """Supported categories of corpus annotation."""

    LEMMA = "lemma"
    POS = "pos"
    MORPHOLOGY = "morphology"
    DEPENDENCIES = "dependencies"
    NAMED_ENTITIES = "named_entities"
    NORMALIZATION = "normalization"
    DATING = "dating"
    OTHER = "other"


class AnnotationQuality(StrEnum):
    """Documented quality levels for annotation layers."""

    EXPERT_GOLD = "expert_gold"
    MANUALLY_CORRECTED = "manually_corrected"
    SILVER = "silver"
    AUTOMATIC = "automatic"


class ProductionMethod(StrEnum):
    """Methods used to produce an annotation layer."""

    MANUAL = "manual"
    MANUAL_CORRECTED = "manual_corrected"
    AUTOMATIC = "automatic"
    MIXED = "mixed"


class Task(StrEnum):
    """Natural-language processing tasks performed by tools."""

    POS_TAGGER = "pos_tagger"
    MORPHOLOGICAL_TAGGER = "morphological_tagger"
    LEMMATIZER = "lemmatizer"
    SYNTACTIC_PARSER = "syntactic_parser"
    LANGUAGE_MODEL = "language_model"


class SizeUnit(StrEnum):
    """Units accepted for reported resource sizes."""

    TEXT = "text"
    SENTENCE = "sentence"
    ORTHOGRAPHIC_WORD = "orthographic_word"
    TOKEN = "token"
    CHARACTER = "character"
    BYTE = "byte"


class OverlapRelationship(StrEnum):
    """Factual relationships between resources or corpus texts."""

    DUPLICATE = "duplicate"
    DERIVED_FROM = "derived_from"
    OVERLAPS = "overlaps"
    SAME_WORK = "same_work"


def _reject_empty_strings(value: Any) -> Any:
    """Reject empty strings recursively within model input values."""

    if isinstance(value, str) and not value.strip():
        raise ValueError("empty strings are not allowed")
    if isinstance(value, list):
        for item in value:
            _reject_empty_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_empty_strings(key)
            _reject_empty_strings(item)
    return value


def _require_unique(values: list[str], label: str) -> None:
    """Require all strings in a scoped identifier collection to be unique."""

    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")


class _StrictModel(BaseModel):
    """Apply strict, assignment-validating behavior to all V2 models."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        populate_by_name=False,
    )

    @field_validator("*", mode="before")
    @classmethod
    def reject_empty_strings(cls, value: Any) -> Any:
        """Reject empty strings before validating any model field."""

        return _reject_empty_strings(value)


class Source(_StrictModel):
    """A reviewed provenance source supporting documented resource fields."""

    id: str = Field(pattern=STABLE_ID_PATTERN)
    url: HttpUrl
    accessed_on: date
    supports: list[str] = Field(min_length=1)
    title: str | None = None
    citation: str | None = None
    quote: str | None = None
    note: str | None = None

    @field_validator("supports")
    @classmethod
    def validate_supports(cls, values: list[str]) -> list[str]:
        """Validate unique dotted support names and reject JSON Pointers."""

        _require_unique(values, "Source.supports entries")
        for value in values:
            if value.startswith("/") or "/" in value or not SUPPORT_RE.fullmatch(value):
                raise ValueError(
                    "supports entries must be documented dotted field/section names, "
                    "not JSON Pointers"
                )
        return values


class Access(_StrictModel):
    """Resource availability and explicit legal permission assessments."""

    availability: list[Availability] = Field(min_length=1)
    model_training: LegalPermission
    original_data_redistribution: LegalPermission
    processed_data_redistribution: LegalPermission
    trained_weight_publication: LegalPermission
    license: str | None = None
    license_url: HttpUrl | None = None
    requirements: list[str] | None = None
    note: str | None = None
    source_ids: list[str] | None = None

    def validate_evidence(self, sources: dict[str, Source]) -> None:
        """Require local direct evidence for every non-unclear permission."""

        _check_source_ids(self.source_ids, sources, "Access.source_ids")
        for field_name in (
            "model_training",
            "original_data_redistribution",
            "processed_data_redistribution",
            "trained_weight_publication",
        ):
            if getattr(self, field_name) is LegalPermission.UNCLEAR:
                continue
            support = f"access.{field_name}"
            direct = [
                sources[source_id]
                for source_id in self.source_ids or []
                if support in sources[source_id].supports
                and sources[source_id].quote is not None
            ]
            if not direct:
                raise ValueError(
                    f"{field_name} requires an Access.source_ids source with "
                    f"supports={support!r} and a direct quote"
                )


class Size(_StrictModel):
    """A positive reported size with its unit and source description."""

    value: int = Field(gt=0)
    unit: SizeUnit
    source: str
    note: str | None = None


class Overlap(_StrictModel):
    """A factual overlap relationship with a qualified target."""

    relationship: OverlapRelationship
    with_: str = Field(alias="with")
    note: str
    source_ids: list[str] | None = None

    @field_validator("with_")
    @classmethod
    def validate_target_syntax(cls, value: str) -> str:
        """Validate corpus, text, and explicitly external target syntax."""

        if not any(
            pattern.fullmatch(value)
            for pattern in (
                STABLE_ID_RE,
                QUALIFIED_TEXT_RE,
                EXTERNAL_CORPUS_RE,
                EXTERNAL_TEXT_RE,
            )
        ):
            raise ValueError(
                "with must be a corpus ID, corpus-id:text-id, "
                "external:corpus-id, or external:corpus-id:text-id"
            )
        return value

    def validate_scope(self, scope: Literal["corpus", "text"]) -> None:
        """Ensure the target syntax matches its corpus or text owner."""

        if scope == "corpus" and not (
            STABLE_ID_RE.fullmatch(self.with_)
            or EXTERNAL_CORPUS_RE.fullmatch(self.with_)
        ):
            raise ValueError("corpus overlap targets must be corpus IDs")
        if scope == "text" and not (
            QUALIFIED_TEXT_RE.fullmatch(self.with_)
            or EXTERNAL_TEXT_RE.fullmatch(self.with_)
        ):
            raise ValueError("text overlap targets must be qualified text IDs")


class BaseResource(_StrictModel):
    """Shared identity and provenance fields for top-level resources."""

    id: str = Field(pattern=STABLE_ID_PATTERN)
    name: str
    aliases: list[str] | None = None
    description: str | None = None
    links: dict[str, HttpUrl] | None = None
    sources: list[Source] = Field(min_length=1)
    reviewed_on: date

    _access_field: ClassVar[str] = "access"

    @field_validator("links")
    @classmethod
    def validate_link_purposes(
        cls, value: dict[str, HttpUrl] | None
    ) -> dict[str, HttpUrl] | None:
        """Require descriptive lower-snake-case keys for resource links."""

        if value is not None:
            for purpose in value:
                if not LINK_PURPOSE_RE.fullmatch(purpose):
                    raise ValueError("link purposes must use lower_snake_case")
        return value

    @field_validator("sources")
    @classmethod
    def validate_resource_sources(cls, sources: list[Source]) -> list[Source]:
        """Require unique source identifiers within the resource."""

        _require_unique([source.id for source in sources], "resource source IDs")
        return sources

    def _validate_access_and_references(self, access: Access) -> dict[str, Source]:
        """Validate access evidence and return indexed local sources."""

        sources = _source_map(self.sources)
        access.validate_evidence(sources)
        return sources


def _source_map(sources: list[Source]) -> dict[str, Source]:
    """Index resource-local sources by their stable identifiers."""

    return {source.id: source for source in sources}


def _check_source_ids(
    source_ids: list[str] | None, sources: dict[str, Source], location: str
) -> None:
    """Ensure source references resolve within their owning resource."""

    for source_id in source_ids or []:
        if source_id not in sources:
            raise ValueError(f"{location} references unknown source ID {source_id!r}")
