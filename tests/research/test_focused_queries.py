from __future__ import annotations

import pytest

from histgerm.research.focused_queries import (
    ResourceCategory,
    apply_exclusion_group,
    bounded_exclusion_groups,
    generate_focused_queries,
)


@pytest.mark.parametrize("category", ["corpus", "tool", "dictionary"])
def test_focused_queries_are_bilingual_and_keep_one_concept(
    category: ResourceCategory,
) -> None:
    queries = generate_focused_queries(category, "mhg")
    assert {query.language for query in queries} == {"de", "en"}
    assert all(
        query.text
        == " ".join(
            filter(
                None,
                (
                    query.stage_term,
                    query.concept,
                    query.qualifier,
                ),
            )
        )
        for query in queries
    )
    assert all(
        query.stage_term in query.text and query.concept in query.text
        for query in queries
    )
    assert all(query.trusted_evidence is False for query in queries)


def test_tool_families_are_expanded_and_tagsets_are_separate_qualifiers() -> None:
    queries = generate_focused_queries("tool", "mhg")
    assert {query.family for query in queries} >= {
        "tagging",
        "morphology",
        "lemmatization",
        "normalization",
        "parsing",
        "segmentation",
        "models",
        "pipelines",
    }
    assert any(query.concept == "Wortartenannotation" for query in queries)
    assert any(
        query.concept == "historical spelling normalization" for query in queries
    )
    tagsets = [query for query in queries if query.qualifier in {"STTS", "HiTS"}]
    assert {query.qualifier for query in tagsets} == {"STTS", "HiTS"}
    assert all(query.family == "tagging" for query in tagsets)
    assert all(query.text.count(query.qualifier or "") == 1 for query in tagsets)


def test_exclusions_are_deduplicated_and_bounded() -> None:
    groups = bounded_exclusion_groups(
        ["TreeTagger", "TreeTagger", "Stuttgart POS Tagger", "A", "B"],
        max_names=2,
        max_characters=48,
    )
    assert all(len(group) <= 2 for group in groups)
    assert [name for group in groups for name in group].count("TreeTagger") == 1
    query = generate_focused_queries("tool", "mhg")[0]
    expanded = apply_exclusion_group(query, groups[0])
    assert expanded.startswith(query.text)
    assert '-"TreeTagger"' in expanded
