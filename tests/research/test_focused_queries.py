from __future__ import annotations

import pytest

from histgerm.models import LanguageStage
from histgerm.research.focused_queries import (
    FocusedQuery,
    QueryFormulation,
    QueryLanguage,
    ResourceCategory,
    apply_exclusion_group,
    bounded_exclusion_groups,
    generate_focused_queries,
    render_query,
)


@pytest.mark.parametrize("category", ["corpus", "tool", "dictionary"])
def test_focused_queries_are_bilingual_and_keep_one_concept(
    category: ResourceCategory,
) -> None:
    queries = generate_focused_queries(category, "mhg")
    assert {query.language for query in queries} == {"de", "en"}
    assert all(query.text == render_query(query, "exact_stage") for query in queries)
    assert all(
        query.stage_term.strip('"') in query.text and query.concept in query.text
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


def query(
    *,
    stage: LanguageStage = LanguageStage.MHG,
    language: QueryLanguage = "en",
    stage_term: str = "Middle High German",
    concept: str = "parser",
    qualifier: str | None = None,
) -> FocusedQuery:
    return FocusedQuery(
        category="tool",
        stage=stage,
        language=language,
        family="parsing",
        stage_term=stage_term,
        concept=concept,
        qualifier=qualifier,
    )


@pytest.mark.parametrize(
    ("stage", "language", "stage_term", "abbreviation"),
    [
        (LanguageStage.OHG, "de", "Althochdeutsch", "OHG"),
        (LanguageStage.OHG, "en", "Old High German", "OHG"),
        (LanguageStage.MHG, "de", "Mittelhochdeutsch", "MHG"),
        (LanguageStage.MHG, "en", "Middle High German", "MHG"),
        (LanguageStage.ENHG, "de", "Frühneuhochdeutsch", "ENHG"),
        (LanguageStage.ENHG, "en", "Early New High German", "ENHG"),
    ],
)
def test_all_stage_language_formulations(
    stage: LanguageStage,
    language: QueryLanguage,
    stage_term: str,
    abbreviation: str,
) -> None:
    focused = query(stage=stage, language=language, stage_term=stage_term)
    expected_stage = f'"{stage_term}"' if " " in stage_term else stage_term
    assert render_query(focused, "plain") == f"{stage_term} parser"
    assert render_query(focused, "exact_stage") == f"{expected_stage} parser"
    assert render_query(focused, "stage_abbreviation") == f"{abbreviation} parser"


def test_user_example_and_german_single_word_stage() -> None:
    assert query().text == '"Middle High German" parser'
    assert (
        query(language="de", stage_term="Mittelhochdeutsch").text
        == "Mittelhochdeutsch parser"
    )


def test_exact_stage_and_concept_quotes_separate_multiword_phrases() -> None:
    focused = query(concept="dependency parser")
    assert (
        render_query(focused, "exact_stage_and_concept")
        == '"Middle High German" "dependency parser"'
    )
    assert '"Middle High German dependency parser"' not in render_query(
        focused, "exact_stage_and_concept"
    )
    assert (
        render_query(focused, "exact_stage") == '"Middle High German" dependency parser'
    )


@pytest.mark.parametrize(
    "formulation",
    [
        "plain",
        "exact_stage",
        "exact_stage_and_concept",
        "stage_abbreviation",
    ],
)
def test_qualifier_stays_separate_and_task_family_is_preserved(
    formulation: QueryFormulation,
) -> None:
    focused = query(concept="POS tagger", qualifier="HiTS")
    rendered = render_query(focused, formulation)
    assert rendered.endswith(" HiTS")
    assert rendered.count("HiTS") == 1
    assert focused.family == "parsing"
    assert "lemmatizer" not in rendered


def test_multiword_qualifier_is_a_separate_exact_phrase() -> None:
    focused = query(qualifier="open source")
    assert render_query(focused) == '"Middle High German" parser "open source"'
    assert '"parser open source"' not in render_query(focused)


def test_embedded_quotes_and_whitespace_are_removed_deterministically() -> None:
    focused = query(
        stage_term='  Middle "High"   German ',
        concept=" dependency “parser” ",
        qualifier='  "HiTS" ',
    )
    assert (
        render_query(focused, "exact_stage_and_concept")
        == '"Middle High German" "dependency parser" HiTS'
    )


def test_exclusions_use_selected_formulation_and_exact_deduplicated_names() -> None:
    focused = query(concept="dependency parser")
    rendered = apply_exclusion_group(
        focused,
        [' Tree "Tagger" ', "Tree Tagger", "Stuttgart POS Tagger"],
        formulation="stage_abbreviation",
    )
    assert rendered == ('MHG dependency parser -"Tree Tagger" -"Stuttgart POS Tagger"')


def test_equivalent_rendered_query_variants_are_deduplicated() -> None:
    queries = generate_focused_queries(
        "tool",
        "mhg",
        qualifiers=[" HiTS ", '"HiTS"'],
    )
    rendered = [render_query(item).casefold() for item in queries]
    assert len(rendered) == len(set(rendered))
