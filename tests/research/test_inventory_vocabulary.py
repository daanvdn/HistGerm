from __future__ import annotations

import threading
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import cast

import pytest

from histgerm.catalog import Catalog
from histgerm.models import (
    Access,
    AnnotationLayer,
    Availability,
    Corpus,
    CorpusVersion,
    Dictionary,
    LanguageStage,
    LegalPermission,
    Source,
    Task,
    Tool,
)
from histgerm.research.inventory_vocabulary import (
    ClassifierCandidate,
    FetchedDocument,
    URLKind,
    VocabularyKind,
    VocabularyLimits,
    enumerate_inventory_urls,
    extract_document,
    mine_inventory_vocabulary,
    normalize_term,
)
from histgerm.research.models import ResourceCategory


def access(source_id: str) -> Access:
    return Access(
        availability=[Availability.DESCRIBED],
        model_training=LegalPermission.UNCLEAR,
        original_data_redistribution=LegalPermission.UNCLEAR,
        processed_data_redistribution=LegalPermission.UNCLEAR,
        trained_weight_publication=LegalPermission.UNCLEAR,
        source_ids=[source_id],
    )


def source(source_id: str, url: str, supports: list[str]) -> Source:
    return Source(
        id=source_id,
        url=url,
        accessed_on=date(2026, 8, 12),
        supports=supports,
    )


def catalog() -> Catalog:
    corpus = Corpus(
        id="corpus-reference",
        name="Reference Corpus of Middle High German",
        aliases=["ReM"],
        reviewed_on=date(2026, 8, 12),
        covered_stages=[LanguageStage.MHG],
        links={
            "homepage": "https://Example.org/project/#top",
            "download": "https://example.org/corpus.zip",
        },
        sources=[
            source(
                "corpus-page",
                "https://example.org/project/",
                ["name", "covered_stages", "access", "versions"],
            )
        ],
        access=access("corpus-page"),
        versions=[
            CorpusVersion(
                id="v1",
                links={
                    "documentation": "https://example.org/docs/",
                    "data_download": "https://example.org/data.xml",
                },
                availability=[Availability.DESCRIBED],
                annotations=[
                    AnnotationLayer(
                        id="pos",
                        type="pos",
                        tagset_name="HiTS",
                        source_ids=["corpus-page"],
                    )
                ],
                texts=[],
            )
        ],
    )
    tool = Tool(
        id="tool-tagger",
        name="Historical Tagger",
        aliases=["HT"],
        reviewed_on=date(2026, 8, 12),
        links={
            "repository": "https://github.com/example/tagger",
            "homepage": "https://example.org/project",
        },
        sources=[
            source(
                "tool-page",
                "https://example.org/tool-metadata",
                ["name", "tasks", "supported_stages", "access"],
            )
        ],
        tasks=[Task.POS_TAGGER, Task.LEMMATIZER],
        supported_stages=[LanguageStage.MHG],
        input_formats=["TEI-XML"],
        output_formats=["CoNLL-U"],
        access=access("tool-page"),
    )
    dictionary = Dictionary(
        id="dictionary-lexicon",
        name="Middle High German Lexicon",
        aliases=["MHGL"],
        reviewed_on=date(2026, 8, 12),
        links={"api_documentation": "https://example.org/api/docs"},
        search_links=["https://example.org/search"],
        sources=[
            source(
                "dictionary-page",
                "https://example.org/dictionary",
                ["name", "covered_stages", "access"],
            )
        ],
        covered_stages=[LanguageStage.MHG],
        access=access("dictionary-page"),
    )
    return Catalog(corpora=[corpus], tools=[tool], dictionaries=[dictionary])


def test_inventory_urls_cover_all_categories_and_deduplicate() -> None:
    urls = enumerate_inventory_urls(catalog())
    by_url = {entry.url: entry for entry in urls}

    shared = by_url["https://example.org/project"]
    assert shared.resource_ids == ("corpus-reference", "tool-tagger")
    assert set(shared.kinds) == {URLKind.HOMEPAGE, URLKind.METADATA}
    assert by_url["https://github.com/example/tagger"].kinds == (
        URLKind.OFFICIAL_REPOSITORY,
    )
    assert by_url["https://example.org/docs"].kinds == (URLKind.DOCUMENTATION,)
    assert "https://example.org/corpus.zip" not in by_url
    assert "https://example.org/data.xml" not in by_url
    assert {resource_id for entry in urls for resource_id in entry.resource_ids} == {
        "corpus-reference",
        "tool-tagger",
        "dictionary-lexicon",
    }


def test_offline_html_extraction_ignores_executable_and_hidden_content() -> None:
    document = extract_document(
        b"""
        <html><head>
          <title>ReM \xe2\x80\x93 Referenzkorpus Mittelhochdeutsch</title>
          <meta name="keywords" content="HiTS, TEI-XML">
          <script>run_command('steal secrets')</script>
          <style>.hidden { content: "bad"; }</style>
        </head><body>
          <nav>Home Login Contact</nav>
          <h1>Part-of-speech tagging</h1>
          <noscript>install untrusted software</noscript>
          <p>Middle High German corpus using HiTS and CoNLL-U.</p>
        </body></html>
        """,
        "text/html; charset=utf-8",
        max_characters=1_000,
    )

    assert document.title == "ReM – Referenzkorpus Mittelhochdeutsch"
    assert document.headings == ("Part-of-speech tagging",)
    assert document.metadata == ("HiTS, TEI-XML",)
    assert "steal secrets" not in document.visible_text
    assert "install untrusted" not in document.visible_text


def test_normalization_and_mining_filter_deduplicate_wordings() -> None:
    html = b"""
      <title>ReM (REM): Middle High German Corpus</title>
      <meta name="description" content="HiTS tagset; hits standard; TEI-XML">
      <h1>Wortartenannotation and part-of-speech tagging</h1>
      <nav>Home Search Login Documentation</nav>
      <p>The Referenzkorpus project exports CoNLL-U.</p>
    """

    def transport(url: str, *, max_bytes: int) -> FetchedDocument:
        assert max_bytes > 0
        return FetchedDocument(url, "text/html", html)

    result = mine_inventory_vocabulary(
        catalog(),
        category="tool",
        stages=[LanguageStage.MHG],
        transport=transport,
        limits=VocabularyLimits(max_pages=2, max_terms=100),
    )
    terms = {(term.kind, term.normalized): term for term in result.terms}

    assert normalize_term("  TEI—XML ") == "tei xml"
    hits = terms[(VocabularyKind.TAGSET_STANDARD, "hits")]
    assert set(hits.wordings) >= {"HiTS", "hits"}
    assert (VocabularyKind.TASK, "wortartenannotation") in terms
    assert (VocabularyKind.TASK, "part of speech tagging") in terms
    assert (VocabularyKind.FORMAT, "conll u") in terms
    assert all(
        term.normalized not in {"home", "search", "login"} for term in result.terms
    )


def test_classifier_input_output_and_page_byte_limits_are_enforced() -> None:
    calls: list[tuple[str, int]] = []
    offered_counts: list[tuple[int, int]] = []
    html = b"""
      <title>Projektverbund Sprachgeschichte (PSG)</title>
      <h1>Middle High German corpus</h1>
      <p>Historical tagging with HiTS, STTS, TEI, XML, JSON and CoNLL-U.</p>
    """

    def transport(url: str, *, max_bytes: int) -> FetchedDocument:
        calls.append((url, max_bytes))
        return FetchedDocument(url, "text/html", html[:max_bytes])

    def classifier(
        candidates: tuple[ClassifierCandidate, ...], *, max_terms: int
    ) -> Sequence[str]:
        offered_counts.append((len(candidates), max_terms))
        return [
            candidates[0].normalized,
            "invented output",
            *(candidate.normalized for candidate in candidates[1:]),
        ]

    result = mine_inventory_vocabulary(
        catalog(),
        category="corpus",
        stages=[LanguageStage.MHG],
        transport=transport,
        classifier=classifier,
        limits=VocabularyLimits(
            max_pages=1,
            max_page_bytes=400,
            max_total_bytes=200,
            max_classifier_candidates=2,
            max_classifier_terms=1,
            max_terms=20,
        ),
    )

    assert len(calls) == 1
    assert calls[0][1] == 200
    assert offered_counts == [(2, 1)]
    assert result.fetched_bytes <= 200
    assert all(term.normalized != "invented output" for term in result.terms)
    assert len(result.terms) <= 20


def test_retrieval_gaps_are_not_availability_claims_and_leave_no_files(
    tmp_path: Path,
) -> None:
    before = tuple(tmp_path.iterdir())

    def transport(url: str, *, max_bytes: int) -> FetchedDocument:
        if url.endswith("/project"):
            raise OSError("synthetic access gap")
        return FetchedDocument(url, "application/zip", b"not metadata")

    result = mine_inventory_vocabulary(
        catalog(),
        category="dictionary",
        stages=[LanguageStage.MHG],
        transport=transport,
        limits=VocabularyLimits(max_pages=2, max_terms=10),
    )

    assert result.gaps
    assert any("synthetic access gap" in gap.reason for gap in result.gaps)
    assert all(not hasattr(gap, "availability") for gap in result.gaps)
    assert tuple(tmp_path.iterdir()) == before


def test_concurrency_is_bounded() -> None:
    active = 0
    maximum = 0
    lock = threading.Lock()
    release = threading.Event()

    def transport(url: str, *, max_bytes: int) -> FetchedDocument:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active >= 2:
                release.set()
        release.wait(timeout=1)
        with lock:
            active -= 1
        return FetchedDocument(
            url,
            "text/html",
            b"<p>Middle High German corpus with HiTS.</p>",
        )

    mine_inventory_vocabulary(
        catalog(),
        category="corpus",
        stages=[LanguageStage.MHG],
        transport=transport,
        limits=VocabularyLimits(max_pages=4, max_concurrency=2, max_terms=10),
    )

    assert maximum <= 2
    assert maximum == 2


def test_aggregate_byte_limit_also_caps_request_count() -> None:
    calls = 0

    def transport(url: str, *, max_bytes: int) -> FetchedDocument:
        nonlocal calls
        calls += 1
        assert max_bytes == 1
        return FetchedDocument(url, "text/plain", b"x")

    result = mine_inventory_vocabulary(
        catalog(),
        category="corpus",
        stages=[LanguageStage.MHG],
        transport=transport,
        limits=VocabularyLimits(
            max_pages=20,
            max_page_bytes=20,
            max_total_bytes=1,
            max_terms=10,
        ),
    )

    assert calls == 1
    assert result.fetched_bytes == 1


@pytest.mark.parametrize("category", ["", "unknown"])
def test_invalid_category_is_rejected(category: str) -> None:
    with pytest.raises(ValueError, match="category"):
        mine_inventory_vocabulary(
            catalog(),
            category=cast(ResourceCategory, category),
            stages=[LanguageStage.MHG],
            transport=lambda url, *, max_bytes: FetchedDocument(url, "text/plain", b""),
        )
