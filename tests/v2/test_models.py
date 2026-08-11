"""Focused tests for the approved HistGerm V2 model contract."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from enum import StrEnum
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

import histgerm
from histgerm import (
    Access,
    AnnotationLayer,
    AnnotationQuality,
    AnnotationType,
    Availability,
    BaseResource,
    Corpus,
    CorpusText,
    CorpusVersion,
    Dictionary,
    LanguageStage,
    LegalPermission,
    Overlap,
    OverlapRelationship,
    ProductionMethod,
    Size,
    SizeUnit,
    Source,
    Task,
    Tool,
)

PUBLIC_MODELS = (
    BaseResource,
    Corpus,
    CorpusVersion,
    CorpusText,
    AnnotationLayer,
    Tool,
    Dictionary,
    Source,
    Access,
    Size,
    Overlap,
)
PUBLIC_ENUMS = (
    LanguageStage,
    LegalPermission,
    Availability,
    AnnotationType,
    AnnotationQuality,
    ProductionMethod,
    Task,
    SizeUnit,
    OverlapRelationship,
)


def source_data(
    source_id: str = "source-main",
    *,
    supports: list[str] | None = None,
    quote: str | None = "Direct evidence.",
) -> dict[str, Any]:
    """Build valid local source input."""

    return {
        "id": source_id,
        "url": "https://example.org/evidence",
        "accessed_on": "2026-08-11",
        "supports": ["name"] if supports is None else supports,
        "quote": quote,
    }


def access_data(
    permission: str = "unclear", *, source_ids: list[str] | None = None
) -> dict[str, Any]:
    """Build valid access input with all four explicit legal fields."""

    return {
        "availability": ["described"],
        "model_training": permission,
        "original_data_redistribution": permission,
        "processed_data_redistribution": permission,
        "trained_weight_publication": permission,
        "source_ids": source_ids,
    }


def text_data(text_id: str = "text-one") -> dict[str, Any]:
    """Build valid corpus text input."""

    return {
        "id": text_id,
        "title": "Text One",
        "shared_work_id": "shared-work",
        "stages": ["mhg"],
        "dialect": "West Central German",
        "date": "around 1200",
        "annotation_ids": ["lemma"],
        "source_ids": ["source-main"],
        "sizes": [
            {
                "value": 120,
                "unit": "token",
                "source": "source-main",
                "note": "Reported token count.",
            }
        ],
    }


def version_data(version_id: str = "v1", text_id: str = "text-one") -> dict[str, Any]:
    """Build valid corpus version input."""

    return {
        "id": version_id,
        "label": "Version 1",
        "links": {"download_zip": "https://example.org/version.zip"},
        "availability": ["downloadable"],
        "annotations": [
            {
                "id": "lemma",
                "type": "lemma",
                "quality": "expert_gold",
                "production_method": "manual",
                "source_ids": ["source-main"],
            }
        ],
        "texts": [text_data(text_id)],
        "source_ids": ["source-main"],
    }


def corpus_data() -> dict[str, Any]:
    """Build valid corpus input."""

    return {
        "id": "corpus-one",
        "name": "Corpus One",
        "aliases": [" C1 "],
        "links": {"homepage": "https://example.org/corpus"},
        "sources": [source_data()],
        "reviewed_on": "2026-08-11",
        "access": access_data(),
        "versions": [version_data()],
    }


def tool_data() -> dict[str, Any]:
    """Build valid tool input."""

    return {
        "id": "tool-one",
        "name": "Tool One",
        "sources": [source_data()],
        "reviewed_on": "2026-08-11",
        "tasks": ["pos_tagger", "lemmatizer"],
        "supported_stages": ["mhg"],
        "access": access_data(),
        "reported_metrics": [
            {
                "name": "accuracy",
                "value": 0.94,
                "task": "pos_tagger",
                "dataset": "test-set",
            }
        ],
    }


def dictionary_data() -> dict[str, Any]:
    """Build valid dictionary input."""

    return {
        "id": "dictionary-one",
        "name": "Dictionary One",
        "sources": [source_data()],
        "reviewed_on": "2026-08-11",
        "covered_stages": ["mhg"],
        "covered_languages": ["German"],
        "lexical_features": ["lemmas"],
        "search_links": ["https://example.org/search"],
        "api_links": ["https://example.org/api"],
        "machine_readable": True,
        "access": access_data(),
        "corpus_links": ["corpus-one"],
    }


def test_exact_public_model_and_enum_boundaries() -> None:
    """Expose exactly eleven model concepts and nine closed enums."""

    assert len(PUBLIC_MODELS) == 11
    assert len(PUBLIC_ENUMS) == 9
    assert all(issubclass(model, BaseModel) for model in PUBLIC_MODELS)
    assert all(issubclass(enum, StrEnum) for enum in PUBLIC_ENUMS)
    assert set(histgerm.__all__) == {
        *(model.__name__ for model in PUBLIC_MODELS),
        *(enum.__name__ for enum in PUBLIC_ENUMS),
    }


@pytest.mark.parametrize(
    ("enum", "members"),
    [
        (LanguageStage, {"OHG": "ohg", "MHG": "mhg", "ENHG": "enhg"}),
        (
            LegalPermission,
            {
                "PERMITTED": "permitted",
                "PROHIBITED": "prohibited",
                "UNCLEAR": "unclear",
            },
        ),
        (
            Availability,
            {
                "DESCRIBED": "described",
                "BROWSABLE": "browsable",
                "DOWNLOADABLE": "downloadable",
                "API": "api",
                "REQUEST_ONLY": "request_only",
                "AUTHENTICATION_REQUIRED": "authentication_required",
                "UNAVAILABLE": "unavailable",
                "DISCONTINUED": "discontinued",
            },
        ),
        (
            AnnotationType,
            {
                "LEMMA": "lemma",
                "POS": "pos",
                "MORPHOLOGY": "morphology",
                "DEPENDENCIES": "dependencies",
                "NAMED_ENTITIES": "named_entities",
                "NORMALIZATION": "normalization",
                "DATING": "dating",
                "OTHER": "other",
            },
        ),
        (
            AnnotationQuality,
            {
                "EXPERT_GOLD": "expert_gold",
                "MANUALLY_CORRECTED": "manually_corrected",
                "SILVER": "silver",
                "AUTOMATIC": "automatic",
            },
        ),
        (
            ProductionMethod,
            {
                "MANUAL": "manual",
                "MANUAL_CORRECTED": "manual_corrected",
                "AUTOMATIC": "automatic",
                "MIXED": "mixed",
            },
        ),
        (
            Task,
            {
                "POS_TAGGER": "pos_tagger",
                "MORPHOLOGICAL_TAGGER": "morphological_tagger",
                "LEMMATIZER": "lemmatizer",
                "SYNTACTIC_PARSER": "syntactic_parser",
                "LANGUAGE_MODEL": "language_model",
            },
        ),
        (
            SizeUnit,
            {
                "TEXT": "text",
                "SENTENCE": "sentence",
                "ORTHOGRAPHIC_WORD": "orthographic_word",
                "TOKEN": "token",
                "CHARACTER": "character",
                "BYTE": "byte",
            },
        ),
        (
            OverlapRelationship,
            {
                "DUPLICATE": "duplicate",
                "DERIVED_FROM": "derived_from",
                "OVERLAPS": "overlaps",
                "SAME_WORK": "same_work",
            },
        ),
    ],
)
def test_exact_enum_members(enum: type[StrEnum], members: dict[str, str]) -> None:
    """Keep each closed enum's names and values exact."""

    assert {member.name: member.value for member in enum} == members


def test_positive_construction_of_all_models() -> None:
    """Construct every approved model with representative valid data."""

    source = Source(**source_data())
    access = Access(**access_data())
    size = Size(value=1, unit="text", source="source-main")
    overlap = Overlap(
        **{
            "relationship": "same_work",
            "with": "external:other-corpus:other-text",
            "note": "Same work in an external corpus.",
        }
    )
    layer = AnnotationLayer(**version_data()["annotations"][0])
    text = CorpusText(**text_data())
    version = CorpusVersion(**version_data())
    base = BaseResource(
        id="base-one",
        name=" Base One ",
        sources=[source],
        reviewed_on=date(2026, 8, 11),
    )
    corpus = Corpus(**corpus_data())
    tool = Tool(**tool_data())
    dictionary = Dictionary(**dictionary_data())

    assert base.name == "Base One"
    assert corpus.aliases == ["C1"]
    assert text.stages == [LanguageStage.MHG]
    assert layer.quality is AnnotationQuality.EXPERT_GOLD
    assert version.availability == [Availability.DOWNLOADABLE]
    assert tool.tasks == [Task.POS_TAGGER, Task.LEMMATIZER]
    assert dictionary.machine_readable is True
    assert access.model_training is LegalPermission.UNCLEAR
    assert size.unit is SizeUnit.TEXT
    assert overlap.with_ == "external:other-corpus:other-text"


def test_tool_tasks_parse_and_reject_invalid_or_empty_values() -> None:
    """Parse approved Task strings and reject unknown or empty task lists."""

    assert Tool(**tool_data()).tasks == [Task.POS_TAGGER, Task.LEMMATIZER]
    for tasks in ([], ["tagger"], ["POS_TAGGER"]):
        data = tool_data()
        data["tasks"] = tasks
        with pytest.raises(ValidationError):
            Tool(**data)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda data: data["sources"].append(deepcopy(data["sources"][0])),
            "resource source IDs",
        ),
        (
            lambda data: data["versions"].append(deepcopy(data["versions"][0])),
            "corpus version IDs",
        ),
        (
            lambda data: data["versions"][0]["annotations"].append(
                deepcopy(data["versions"][0]["annotations"][0])
            ),
            "annotation layer IDs",
        ),
        (
            lambda data: data["versions"].append(version_data("v2", "text-one")),
            "text IDs across",
        ),
    ],
)
def test_scoped_identifiers_are_unique(mutate: Any, message: str) -> None:
    """Reject duplicate source, version, layer, and corpus-local text IDs."""

    data = corpus_data()
    mutate(data)
    with pytest.raises(ValidationError, match=message):
        Corpus(**data)


def test_overlap_syntax_scope_alias_and_inventory_resolution() -> None:
    """Enforce qualified text overlap targets and safe input aliases."""

    data = corpus_data()
    data["versions"][0]["texts"].append(text_data("text-two"))
    data["versions"][0]["texts"][0]["overlaps"] = [
        {
            "relationship": "same_work",
            "with": "corpus-one:text-two",
            "note": "Two witnesses of the same work.",
            "source_ids": ["source-main"],
        }
    ]
    corpus = Corpus(**data)
    overlap = corpus.versions[0].texts[0].overlaps
    assert overlap is not None
    assert overlap[0].model_dump(by_alias=True)["with"] == "corpus-one:text-two"
    corpus.validate_inventory_references(
        {"corpus-one"}, {"corpus-one:text-one", "corpus-one:text-two"}
    )

    with pytest.raises(ValidationError):
        Overlap.model_validate(
            {
                "relationship": "overlaps",
                "with_": "corpus-one:text-two",
                "note": "Field-name population is intentionally disabled.",
            }
        )
    bad_scope = deepcopy(data)
    bad_scope["versions"][0]["texts"][0]["overlaps"][0]["with"] = "corpus-two"
    with pytest.raises(ValidationError, match="qualified text IDs"):
        Corpus(**bad_scope)
    with pytest.raises(ValueError, match="unknown text overlap target"):
        corpus.validate_inventory_references({"corpus-one"}, {"corpus-one:text-one"})


def test_local_source_layer_and_text_overlap_references_resolve() -> None:
    """Reject unknown local provenance, annotation, and self-corpus targets."""

    bad_source = corpus_data()
    bad_source["versions"][0]["texts"][0]["source_ids"] = ["missing"]
    with pytest.raises(ValidationError, match="unknown source ID"):
        Corpus(**bad_source)

    bad_layer = corpus_data()
    bad_layer["versions"][0]["texts"][0]["annotation_ids"] = ["missing"]
    with pytest.raises(ValidationError, match="unknown annotation IDs"):
        Corpus(**bad_layer)

    bad_overlap = corpus_data()
    bad_overlap["versions"][0]["texts"][0]["overlaps"] = [
        {
            "relationship": "overlaps",
            "with": "corpus-one:missing",
            "note": "Unresolvable local target.",
        }
    ]
    with pytest.raises(ValidationError, match="unknown local text"):
        Corpus(**bad_overlap)


def test_non_unclear_legal_permissions_require_direct_evidence() -> None:
    """Require exact support and a quote for every decided legal permission."""

    legal_fields = [
        "model_training",
        "original_data_redistribution",
        "processed_data_redistribution",
        "trained_weight_publication",
    ]
    data = tool_data()
    data["sources"] = [
        source_data(
            supports=[f"access.{field}" for field in legal_fields],
            quote="The terms directly grant these permissions.",
        )
    ]
    data["access"] = access_data("permitted", source_ids=["source-main"])
    assert Tool(**data).access.model_training is LegalPermission.PERMITTED

    missing_quote = deepcopy(data)
    missing_quote["sources"][0]["quote"] = None
    with pytest.raises(ValidationError, match="direct quote"):
        Tool(**missing_quote)

    missing_support = deepcopy(data)
    missing_support["sources"][0]["supports"] = ["access"]
    with pytest.raises(ValidationError, match="supports="):
        Tool(**missing_support)


def test_explicit_unclear_permissions_need_no_claimed_direct_evidence() -> None:
    """Preserve explicit unclear values without inventing legal evidence."""

    access = Tool(**tool_data()).access
    assert {
        access.model_training,
        access.original_data_redistribution,
        access.processed_data_redistribution,
        access.trained_weight_publication,
    } == {LegalPermission.UNCLEAR}


@pytest.mark.parametrize(
    "bad_input",
    [
        {"id": "UpperCase"},
        {"id": "bad_id"},
        {"id": "-leading"},
        {"id": "double--hyphen"},
        {"id": ""},
    ],
)
def test_stable_ids_reject_unapproved_forms(bad_input: dict[str, str]) -> None:
    """Accept only lower-case kebab-case stable identifiers."""

    data = source_data()
    data.update(bad_input)
    with pytest.raises(ValidationError):
        Source(**data)


@pytest.mark.parametrize(
    "supports",
    [
        [],
        ["name", "name"],
        ["/access/model_training"],
        ["access/model_training"],
        ["unknown_section"],
    ],
)
def test_source_supports_are_nonempty_unique_and_dotted(
    supports: list[str],
) -> None:
    """Reject empty, duplicate, pointer-like, and unknown support scopes."""

    with pytest.raises(ValidationError):
        Source(**source_data(supports=supports))


def test_urls_sizes_other_annotations_and_metrics_are_strict() -> None:
    """Reject invalid URLs, nonpositive sizes, unexplained other, and bad metrics."""

    invalid_source = source_data()
    invalid_source["url"] = "ftp://example.org/source"
    with pytest.raises(ValidationError):
        Source(**invalid_source)
    with pytest.raises(ValidationError):
        Size(value=0, unit="token", source="reported")
    with pytest.raises(ValidationError, match="requires tagset_name or note"):
        AnnotationLayer(
            id="tokens",
            type="other",
            source_ids=["source-main"],
        )
    for metric in (
        {"name": "accuracy"},
        {"name": "accuracy", "value": True},
        {"name": "accuracy", "value": 1, "extra": "no"},
    ):
        data = tool_data()
        data["reported_metrics"] = [metric]
        with pytest.raises(ValidationError):
            Tool(**data)


def test_extra_fields_and_recursive_empty_strings_are_rejected() -> None:
    """Forbid unknown fields and empty strings throughout nested input."""

    extra = tool_data()
    extra["unknown"] = "value"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Tool(**extra)

    empty_alias = corpus_data()
    empty_alias["aliases"] = ["   "]
    with pytest.raises(ValidationError, match="empty strings"):
        Corpus(**empty_alias)

    empty_metric = tool_data()
    empty_metric["reported_metrics"] = [{"name": " ", "value": 1}]
    with pytest.raises(ValidationError, match="empty strings"):
        Tool(**empty_metric)


def test_assignment_validation_preserves_schema_invariants() -> None:
    """Revalidate fields when approved model instances are mutated."""

    size = Size(value=3, unit="token", source="report")
    with pytest.raises(ValidationError):
        size.value = 0
