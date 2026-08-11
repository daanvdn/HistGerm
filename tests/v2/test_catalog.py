"""Behavior tests for the minimal HistGerm V2 catalog facade."""

from __future__ import annotations

from typing import Any

import pytest

from histgerm.catalog import Catalog, load_catalog
from histgerm.models import Corpus, Dictionary, LanguageStage, Task, Tool


def _source() -> dict[str, Any]:
    """Build compact direct evidence for synthetic records."""

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
    """Build all four explicit legal permission assessments."""

    return {
        "availability": ["described"],
        "model_training": permission,
        "original_data_redistribution": permission,
        "processed_data_redistribution": permission,
        "trained_weight_publication": permission,
        "source_ids": ["project"],
        "note": "Review required.",
    }


def _corpus() -> Corpus:
    """Build an independent two-text corpus for filter and warning behavior."""

    return Corpus.model_validate(
        {
            "id": "corpus-test",
            "name": "Test Corpus",
            "sources": [_source()],
            "reviewed_on": "2026-08-11",
            "access": _access(),
            "versions": [
                {
                    "id": "v1",
                    "availability": ["downloadable"],
                    "source_ids": ["project"],
                    "annotations": [
                        {
                            "id": "pos",
                            "type": "pos",
                            "tagset_name": "STTS-like test tagset",
                            "source_ids": ["project"],
                        },
                        {
                            "id": "lemma",
                            "type": "lemma",
                            "tagset_name": "Test lemma conventions",
                            "source_ids": ["project"],
                        },
                    ],
                    "texts": [
                        {
                            "id": "sermon-a",
                            "title": "Early Sermon",
                            "stages": ["ohg"],
                            "dialect": "East Franconian",
                            "date": "approximately late 9th century",
                            "annotation_ids": ["pos"],
                            "source_ids": ["project"],
                            "overlaps": [
                                {
                                    "relationship": "same_work",
                                    "with": "corpus-test:sermon-b",
                                    "note": "Two witnesses of one work.",
                                    "source_ids": ["project"],
                                }
                            ],
                        },
                        {
                            "id": "sermon-b",
                            "title": "Later Sermon",
                            "stages": ["mhg"],
                            "dialect": "Alemannic with mixed regional features",
                            "date": "circa 1200–1250",
                            "annotation_ids": ["pos", "lemma"],
                            "source_ids": ["project"],
                            "overlaps": [
                                {
                                    "relationship": "same_work",
                                    "with": "corpus-test:sermon-a",
                                    "note": "Related, not duplicate.",
                                    "source_ids": ["project"],
                                }
                            ],
                        },
                    ],
                }
            ],
            "overlaps": [
                {
                    "relationship": "overlaps",
                    "with": "external:corpus-other",
                    "note": "External corpus relationship.",
                    "source_ids": ["project"],
                }
            ],
        }
    )


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


def test_real_loader_preserves_order_and_distinct_resource_types() -> None:
    """Load exactly the three authored resources as distinct model types."""

    catalog = load_catalog()

    assert [corpus.id for corpus in catalog.find_corpora()] == ["res-rem"]
    assert [tool.id for tool in catalog.find_tools()] == ["res-rnntagger"]
    assert [item.id for item in catalog.find_dictionaries()] == ["res-mwb"]
    assert len(catalog.find_texts()) == 406
    assert [text.id for text in catalog.find_texts()[:2]] == ["m001", "m002"]
    assert catalog.find_texts()[-1].id == "m552"
    assert isinstance(catalog.find_corpora()[0], Corpus)
    assert isinstance(catalog.find_tools()[0], Tool)
    assert isinstance(catalog.find_dictionaries()[0], Dictionary)


def test_exactly_four_public_find_methods_exist() -> None:
    """Keep the approved query surface to four named find methods."""

    methods = {
        name
        for name in dir(Catalog)
        if name.startswith("find_") and callable(getattr(Catalog, name))
    }
    assert methods == {
        "find_corpora",
        "find_texts",
        "find_tools",
        "find_dictionaries",
    }


@pytest.mark.parametrize(
    ("text_id", "filters"),
    [
        (
            "sermon-a",
            {
                "corpus_id": "corpus-test",
                "text_id": "sermon-a",
                "stage": "ohg",
                "dialect": "  EAST   FRANCONIAN ",
                "annotation_type": "pos",
                "tagset": " stts-LIKE  test TAGSET ",
                "date_contains": "LATE 9TH CENTURY",
                "has_overlap": True,
            },
        ),
        (
            "sermon-b",
            {
                "corpus_id": "corpus-test",
                "text_id": "sermon-b",
                "stage": "mhg",
                "dialect": "Alemannic with mixed regional features",
                "annotation_type": "lemma",
                "tagset": "TEST LEMMA CONVENTIONS",
                "date_contains": "1200–1250",
                "has_overlap": True,
            },
        ),
    ],
)
def test_every_required_text_filter_combines_with_and_semantics(
    text_id: str, filters: dict[str, object]
) -> None:
    """Apply every text filter together to each independent fixture text."""

    catalog = Catalog(corpora=[_corpus()])
    assert [text.id for text in catalog.find_texts(**filters)] == [text_id]  # type: ignore[arg-type]


def test_every_required_text_filter_works_independently() -> None:
    """Exercise ID, stage, dialect, layer, tagset, date, and overlap filters."""

    corpus = _corpus()
    corpus.versions[0].texts[0].overlaps = None
    catalog = Catalog(corpora=[corpus])
    cases = [
        ({"corpus_id": "corpus-test"}, ["sermon-a", "sermon-b"]),
        ({"corpus_id": "corpus-test", "text_id": "sermon-a"}, ["sermon-a"]),
        ({"stage": LanguageStage.OHG}, ["sermon-a"]),
        ({"dialect": " east   franconian "}, ["sermon-a"]),
        ({"annotation_type": "lemma"}, ["sermon-b"]),
        ({"tagset": " test lemma conventions "}, ["sermon-b"]),
        ({"date_contains": "LATE 9TH"}, ["sermon-a"]),
        ({"has_overlap": False}, ["sermon-a"]),
        ({"has_overlap": True}, ["sermon-b"]),
    ]
    for filters, expected in cases:
        assert [text.id for text in catalog.find_texts(**filters)] == expected  # type: ignore[arg-type]


def test_text_ids_are_strictly_scoped_and_exact() -> None:
    """Require corpus qualification while keeping IDs non-normalized."""

    catalog = Catalog(corpora=[_corpus()])

    assert catalog.find_texts(corpus_id=" CORPUS-TEST ") == []
    assert catalog.find_texts(corpus_id="corpus-test", text_id="SERMON-A") == []
    assert (
        catalog.find_texts(
            corpus_id="corpus-test", stage="ohg", annotation_type="lemma"
        )
        == []
    )
    with pytest.raises(ValueError, match="requires corpus_id"):
        catalog.find_texts(text_id="sermon-a")
    with pytest.raises(ValueError, match="bare corpus-local"):
        catalog.find_texts(corpus_id="corpus-test", text_id="corpus-test:sermon-a")


def test_find_corpora_filters_by_any_owned_text_stage() -> None:
    """Return corpora containing a text in the requested stage."""

    corpus = _corpus()
    catalog = Catalog(corpora=[corpus])

    assert catalog.find_corpora() == [corpus]
    assert catalog.find_corpora(stage="ohg") == [corpus]
    assert catalog.find_corpora(stage="enhg") == []


def test_tool_filters_use_task_enum_and_normalized_exact_membership() -> None:
    """Filter tools by enum task, stage, format, and AND semantics."""

    tagger = _tool("tool-tagger", Task.POS_TAGGER, "CoNLL-U")
    lemmatizer = _tool("tool-lemma", Task.LEMMATIZER, "TSV")
    catalog = Catalog(tools=[tagger, lemmatizer])

    assert catalog.find_tools() == [tagger, lemmatizer]
    assert catalog.find_tools(
        task=Task.POS_TAGGER, stage="mhg", output_format="  conll-u "
    ) == [tagger]
    assert catalog.find_tools(task="lemmatizer", output_format="conll-u") == []
    assert catalog.find_tools(output_format="conll") == []
    with pytest.raises(ValueError):
        catalog.find_tools(task="POS_TAGGER")


def test_dictionary_filters_are_normalized_exact_and_boolean() -> None:
    """Filter dictionaries by stage, exact feature, and Boolean state."""

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
    assert catalog.find_dictionaries(lexical_feature="spelling") == []
    assert catalog.find_dictionaries(machine_readable=False) == [closed]


def test_legal_warnings_are_separate_and_resolve_text_ownership() -> None:
    """Return four legal rows per unclear owner without overlap fields."""

    corpus = _corpus()
    catalog = Catalog(corpora=[corpus])
    first, second = catalog.find_texts(corpus_id="corpus-test")

    rows = catalog.legal_warnings([corpus, first, second])

    assert len(rows) == 12
    assert [row["text_id"] for row in rows] == [
        *([None] * 4),
        *(["corpus-test:sermon-a"] * 4),
        *(["corpus-test:sermon-b"] * 4),
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
    """Warn on prohibited fields while suppressing permitted fields."""

    prohibited = _tool(
        "tool-prohibited", Task.POS_TAGGER, "TSV", permission="prohibited"
    )
    permitted = _tool("tool-permitted", Task.LEMMATIZER, "TSV", permission="permitted")

    rows = Catalog(tools=[prohibited, permitted]).legal_warnings(
        [prohibited, permitted]
    )

    assert len(rows) == 4
    assert {row["resource_id"] for row in rows} == {"tool-prohibited"}
    assert {row["value"] for row in rows} == {"prohibited"}


def test_overlap_warnings_preserve_authored_corpus_and_text_rows() -> None:
    """Keep overlap rows separate from legal warnings and uninferred."""

    corpus = _corpus()
    catalog = Catalog(corpora=[corpus])
    first, second = catalog.find_texts(corpus_id="corpus-test")

    assert catalog.overlap_warnings([corpus]) == [
        {
            "owner_id": "corpus-test",
            "relationship": "overlaps",
            "with": "external:corpus-other",
            "note": "External corpus relationship.",
            "source_ids": ["project"],
        }
    ]
    rows = catalog.overlap_warnings([first, second])
    assert [row["owner_id"] for row in rows] == [
        "corpus-test:sermon-a",
        "corpus-test:sermon-b",
    ]
    assert [row["with"] for row in rows] == [
        "corpus-test:sermon-b",
        "corpus-test:sermon-a",
    ]
    assert all("field" not in row and "text_count" not in row for row in rows)


def test_coverage_rows_are_simple_unique_and_stable() -> None:
    """Build plain grouped rows with qualified, deduplicated text IDs."""

    catalog = Catalog(corpora=[_corpus()])
    first, second = catalog.find_texts(corpus_id="corpus-test")

    assert catalog.coverage_summary([second, first, second], by=["stage"]) == [
        {
            "stage": "mhg",
            "text_count": 1,
            "text_ids": ["corpus-test:sermon-b"],
        },
        {
            "stage": "ohg",
            "text_count": 1,
            "text_ids": ["corpus-test:sermon-a"],
        },
    ]
    assert catalog.coverage_summary([first, second], by=["annotation_type"]) == [
        {
            "annotation_type": "pos",
            "text_count": 2,
            "text_ids": ["corpus-test:sermon-a", "corpus-test:sermon-b"],
        },
        {
            "annotation_type": "lemma",
            "text_count": 1,
            "text_ids": ["corpus-test:sermon-b"],
        },
    ]
    assert catalog.coverage_summary([first, second], by=["stage", "tagset"]) == [
        {
            "stage": "ohg",
            "tagset": "STTS-like test tagset",
            "text_count": 1,
            "text_ids": ["corpus-test:sermon-a"],
        },
        {
            "stage": "mhg",
            "tagset": "STTS-like test tagset",
            "text_count": 1,
            "text_ids": ["corpus-test:sermon-b"],
        },
        {
            "stage": "mhg",
            "tagset": "Test lemma conventions",
            "text_count": 1,
            "text_ids": ["corpus-test:sermon-b"],
        },
    ]


@pytest.mark.parametrize("by", [[], ["stage", "stage"], ["unsupported"]])
def test_coverage_dimensions_are_validated(by: list[str]) -> None:
    """Reject empty, duplicate, or unsupported coverage dimensions."""

    catalog = Catalog(corpora=[_corpus()])
    with pytest.raises(ValueError):
        catalog.coverage_summary(catalog.find_texts(), by=by)  # type: ignore[arg-type]
