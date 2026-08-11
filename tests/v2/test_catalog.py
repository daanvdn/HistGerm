"""Focused tests for the minimal HistGerm V2 catalog facade."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from histgerm.catalog import Catalog, load_catalog
from histgerm.loading import load_bundled_yaml
from histgerm.models import Corpus, Dictionary, LanguageStage, Task, Tool


def _source() -> dict[str, Any]:
    """Build a compact synthetic provenance source."""

    return {
        "id": "project",
        "url": "https://example.org/project",
        "accessed_on": "2026-08-11",
        "supports": [
            "name",
            "access.model_training",
            "access.original_data_redistribution",
            "access.processed_data_redistribution",
            "access.trained_weight_publication",
        ],
        "quote": "Synthetic direct legal evidence.",
    }


def _access(permission: str = "unclear") -> dict[str, Any]:
    """Build an access record with four explicit permission assessments."""

    return {
        "availability": ["described"],
        "model_training": permission,
        "original_data_redistribution": permission,
        "processed_data_redistribution": permission,
        "trained_weight_publication": permission,
        "source_ids": ["project"],
        "note": "Review required.",
    }


def _tool(
    tool_id: str,
    task: Task,
    output_format: str,
    *,
    permission: str = "unclear",
) -> Tool:
    """Build a compact tool for query tests."""

    return Tool.model_validate(
        {
            "id": tool_id,
            "name": tool_id,
            "sources": [_source()],
            "reviewed_on": "2026-08-11",
            "tasks": [task],
            "supported_stages": ["mhg"],
            "output_formats": [output_format],
            "access": _access(permission),
        }
    )


def _dictionary(
    dictionary_id: str, *, feature: str, machine_readable: bool
) -> Dictionary:
    """Build a compact dictionary for query tests."""

    return Dictionary.model_validate(
        {
            "id": dictionary_id,
            "name": dictionary_id,
            "sources": [_source()],
            "reviewed_on": "2026-08-11",
            "covered_stages": ["mhg"],
            "lexical_features": [feature],
            "machine_readable": machine_readable,
            "access": _access(),
        }
    )


def test_real_loader_preserves_no_filter_order_and_model_types() -> None:
    """Load the bundled corpus without mocks and preserve authored text order."""

    catalog = load_catalog()

    assert [corpus.id for corpus in catalog.find_corpora()] == ["corpus-synthetic-demo"]
    assert [text.id for text in catalog.find_texts()] == ["sermon-a", "sermon-b"]
    assert catalog.find_tools() == []
    assert catalog.find_dictionaries() == []
    assert all(isinstance(text, object) for text in catalog.find_texts())


@pytest.mark.parametrize(
    ("text_id", "filters"),
    [
        (
            "sermon-a",
            {
                "corpus_id": "corpus-synthetic-demo",
                "stage": "ohg",
                "dialect": "  EAST   FRANCONIAN (SYNTHETIC LABEL) ",
                "annotation_type": "pos",
                "tagset": " stts-LIKE  synthetic TAGSET ",
                "date_contains": "LATE 9TH CENTURY",
                "has_overlap": True,
            },
        ),
        (
            "sermon-b",
            {
                "corpus_id": "corpus-synthetic-demo",
                "stage": "mhg",
                "dialect": ("ALEMANNIC WITH MIXED REGIONAL FEATURES (SYNTHETIC LABEL)"),
                "annotation_type": "lemma",
                "tagset": "SYNTHETIC LEMMA CONVENTIONS",
                "date_contains": "1200–1250",
                "has_overlap": True,
            },
        ),
    ],
)
def test_each_text_matches_every_required_filter_independently(
    text_id: str, filters: dict[str, object]
) -> None:
    """Apply all approved text filters with AND semantics to each fixture text."""

    catalog = load_catalog()

    assert [text.id for text in catalog.find_texts(**filters)] == [text_id]  # type: ignore[arg-type]


def test_each_distinguishing_text_filter_works_on_its_own() -> None:
    """Exercise stage, dialect, layer, tagset, and date filters separately."""

    catalog = load_catalog()
    cases = [
        ({"stage": "ohg"}, ["sermon-a"]),
        ({"stage": "mhg"}, ["sermon-b"]),
        ({"dialect": "East Franconian (synthetic label)"}, ["sermon-a"]),
        (
            {"dialect": "Alemannic with mixed regional features (synthetic label)"},
            ["sermon-b"],
        ),
        ({"annotation_type": "lemma"}, ["sermon-b"]),
        ({"tagset": "Synthetic lemma conventions"}, ["sermon-b"]),
        ({"date_contains": "late 9th"}, ["sermon-a"]),
        ({"date_contains": "1200–1250"}, ["sermon-b"]),
    ]

    for filters, expected in cases:
        assert [text.id for text in catalog.find_texts(**filters)] == expected  # type: ignore[arg-type]


def test_text_filters_are_anded_and_ids_are_strictly_qualified() -> None:
    """Reject global text IDs and avoid fuzzy or qualified ID matching."""

    catalog = load_catalog()

    assert (
        catalog.find_texts(
            corpus_id="corpus-synthetic-demo", stage="ohg", annotation_type="lemma"
        )
        == []
    )
    assert catalog.find_texts(corpus_id=" CORPUS-SYNTHETIC-DEMO ") == []
    with pytest.raises(ValueError, match="requires corpus_id"):
        catalog.find_texts(text_id="sermon-a")
    with pytest.raises(ValueError, match="bare corpus-local"):
        catalog.find_texts(
            corpus_id="corpus-synthetic-demo",
            text_id="corpus-synthetic-demo:sermon-a",
        )


def test_corpus_filter_and_false_overlap_filter() -> None:
    """Find corpora by text stage and distinguish an empty overlap list."""

    payload = deepcopy(load_bundled_yaml("corpora/synthetic-corpus.yaml"))
    payload["versions"][0]["texts"][0].pop("overlaps")
    corpus = Corpus.model_validate(payload)
    catalog = Catalog(corpora=[corpus])

    assert catalog.find_corpora(stage=LanguageStage.OHG) == [corpus]
    assert catalog.find_corpora(stage="enhg") == []
    assert [text.id for text in catalog.find_texts(has_overlap=False)] == ["sermon-a"]
    assert [text.id for text in catalog.find_texts(has_overlap=True)] == ["sermon-b"]


def test_tool_filters_use_task_enum_and_normalized_membership() -> None:
    """Filter synthetic tools by enum task, stage, format, and AND semantics."""

    tagger = _tool("tool-tagger", Task.POS_TAGGER, "CoNLL-U")
    lemmatizer = _tool("tool-lemma", Task.LEMMATIZER, "TSV")
    catalog = Catalog(tools=[tagger, lemmatizer])

    assert catalog.find_tools() == [tagger, lemmatizer]
    assert catalog.find_tools(
        task=Task.POS_TAGGER, stage="mhg", output_format="  conll-u "
    ) == [tagger]
    assert catalog.find_tools(task="lemmatizer", output_format="conll-u") == []
    with pytest.raises(ValueError):
        catalog.find_tools(task="POS_TAGGER")


def test_dictionary_filters_are_exact_normalized_and_boolean() -> None:
    """Filter dictionaries by stage, normalized feature, and exact Boolean."""

    readable = _dictionary(
        "dictionary-readable", feature="Spelling Variants", machine_readable=True
    )
    closed = _dictionary("dictionary-closed", feature="Lemmas", machine_readable=False)
    catalog = Catalog(dictionaries=[readable, closed])

    assert catalog.find_dictionaries() == [readable, closed]
    assert catalog.find_dictionaries(
        stage="mhg",
        lexical_feature=" spelling   variants ",
        machine_readable=True,
    ) == [readable]
    assert (
        catalog.find_dictionaries(lexical_feature="spelling", machine_readable=True)
        == []
    )


def test_legal_warnings_resolve_four_rows_per_owner_context() -> None:
    """Resolve corpus access for texts and keep legal rows free of overlaps."""

    catalog = load_catalog()
    corpus = catalog.find_corpora()[0]
    first, second = catalog.find_texts()

    rows = catalog.legal_warnings([corpus, first, second])

    assert len(rows) == 12
    assert [row["text_id"] for row in rows] == [
        *([None] * 4),
        *(["corpus-synthetic-demo:sermon-a"] * 4),
        *(["corpus-synthetic-demo:sermon-b"] * 4),
    ]
    assert {row["value"] for row in rows} == {"unclear"}
    assert {row["field"] for row in rows} == {
        "model_training",
        "original_data_redistribution",
        "processed_data_redistribution",
        "trained_weight_publication",
    }
    assert all("relationship" not in row for row in rows)


def test_legal_warnings_include_prohibited_and_omit_permitted() -> None:
    """Emit prohibited fields while suppressing evidence-backed permissions."""

    prohibited = _tool(
        "tool-prohibited",
        Task.POS_TAGGER,
        "TSV",
        permission="prohibited",
    )
    permitted = _tool(
        "tool-permitted",
        Task.LEMMATIZER,
        "TSV",
        permission="permitted",
    )
    catalog = Catalog(tools=[prohibited, permitted])

    rows = catalog.legal_warnings([prohibited, permitted])

    assert len(rows) == 4
    assert {row["resource_id"] for row in rows} == {"tool-prohibited"}
    assert {row["value"] for row in rows} == {"prohibited"}


def test_overlap_warnings_keep_corpus_and_text_authorship_separate() -> None:
    """Preserve authored overlap order without reciprocal inference or totals."""

    catalog = load_catalog()
    corpus = catalog.find_corpora()[0]
    first, second = catalog.find_texts()

    corpus_rows = catalog.overlap_warnings([corpus])
    text_rows = catalog.overlap_warnings([first, second])

    assert corpus_rows == [
        {
            "owner_id": "corpus-synthetic-demo",
            "relationship": "overlaps",
            "with": "external:corpus-other-synthetic",
            "note": (
                "Synthetic external corpus relationship; no real resource is asserted."
            ),
            "source_ids": ["project"],
        }
    ]
    assert [row["owner_id"] for row in text_rows] == [
        "corpus-synthetic-demo:sermon-a",
        "corpus-synthetic-demo:sermon-b",
    ]
    assert [row["with"] for row in text_rows] == [
        "corpus-synthetic-demo:sermon-b",
        "corpus-synthetic-demo:sermon-a",
    ]
    assert all("text_count" not in row for row in text_rows)


def test_coverage_groups_use_qualified_unique_sorted_text_ids() -> None:
    """Build stable annotation groups and deduplicate repeated input texts."""

    catalog = load_catalog()
    first, second = catalog.find_texts()

    assert catalog.coverage_summary([second, first, second], by=["stage"]) == [
        {
            "stage": "mhg",
            "text_count": 1,
            "text_ids": ["corpus-synthetic-demo:sermon-b"],
        },
        {
            "stage": "ohg",
            "text_count": 1,
            "text_ids": ["corpus-synthetic-demo:sermon-a"],
        },
    ]
    assert catalog.coverage_summary([first, second], by=["annotation_type"]) == [
        {
            "annotation_type": "pos",
            "text_count": 2,
            "text_ids": [
                "corpus-synthetic-demo:sermon-a",
                "corpus-synthetic-demo:sermon-b",
            ],
        },
        {
            "annotation_type": "lemma",
            "text_count": 1,
            "text_ids": ["corpus-synthetic-demo:sermon-b"],
        },
    ]
    assert catalog.coverage_summary([first, second], by=["stage", "tagset"]) == [
        {
            "stage": "ohg",
            "tagset": "STTS-like synthetic tagset",
            "text_count": 1,
            "text_ids": ["corpus-synthetic-demo:sermon-a"],
        },
        {
            "stage": "mhg",
            "tagset": "STTS-like synthetic tagset",
            "text_count": 1,
            "text_ids": ["corpus-synthetic-demo:sermon-b"],
        },
        {
            "stage": "mhg",
            "tagset": "Synthetic lemma conventions",
            "text_count": 1,
            "text_ids": ["corpus-synthetic-demo:sermon-b"],
        },
    ]


@pytest.mark.parametrize("by", [[], ["stage", "stage"], ["unsupported"]])
def test_coverage_dimensions_are_validated(by: list[str]) -> None:
    """Reject empty, duplicate, or unsupported coverage dimensions."""

    catalog = load_catalog()

    with pytest.raises(ValueError):
        catalog.coverage_summary(catalog.find_texts(), by=by)  # type: ignore[arg-type]
