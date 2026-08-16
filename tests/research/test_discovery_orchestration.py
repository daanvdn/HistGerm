from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from histgerm.catalog import Catalog, load_catalog
from histgerm.models import BaseResource, LanguageStage, Source
from histgerm.research.crawl4ai_adapter import (
    CacheDisposition,
    Crawl4AIAdapterError,
    Crawl4AIResult,
)
from histgerm.research.discovery_orchestration import (
    DiscoveryConfig,
    DiscoveryDependencies,
    DiscoveryRunResult,
    ProviderFetch,
    ProviderResponse,
    _active_inventory_vocabulary,
    _completion_gaps,
    _controlled_recall_queries,
    _cross_channel_pivot,
    run_discovery,
)
from histgerm.research.focused_queries import FocusedQuery, ResourceCategory
from histgerm.research.inventory_vocabulary import (
    CandidateDecision,
    ClassifierCandidate,
    FetchedDocument,
    VocabularyLimits,
)
from histgerm.research.search_providers import (
    ResponseFormat,
    ResultClassification,
    SearchRequest,
    SearchResult,
)
from histgerm.research.vocabulary_store import (
    DiscoveryVocabulary,
    VocabularyContext,
    VocabularyRevisionError,
    VocabularySource,
    VocabularyTerm,
    VocabularyWording,
    apply_vocabulary,
    load_vocabulary,
    serialize_vocabulary,
)


class EmptyModel:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls = 0

    def __call__(self, prompt: str, /) -> str:
        self.events.append("model")
        self.calls += 1
        return '{"candidates":[]}'


class MalformedSiblingModel:
    """Return one valid lead beside a malformed sibling, then empty responses."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, prompt: str, /) -> str:
        self.calls += 1
        if self.calls == 1:
            return json.dumps(
                {
                    "candidates": [
                        {"name": "Kept Lead", "aliases": ["KL"]},
                        {"name": "Bad Sibling", "aliases": [], "rationale": "nope"},
                    ]
                }
            )
        return '{"candidates":[]}'


def test_malformed_model_sibling_keeps_valid_lead_and_surfaces_metrics() -> None:
    model = MalformedSiblingModel()

    result = run_discovery(
        DiscoveryConfig(
            category="tool",
            stage=LanguageStage.MHG,
            max_mined_terms=0,
            max_exclusion_groups=1,
            vocabulary=VocabularyLimits(max_pages=1),
        ),
        DiscoveryDependencies(
            catalog=load_catalog(),
            model_call=model,
            vocabulary_transport=lambda url, *, max_bytes: FetchedDocument(
                url, "text/plain", b""
            ),
            provider_fetch=lambda request: ProviderResponse(
                retrieval_mode="bounded_http",
                observed_at=datetime(2026, 8, 12, tzinfo=UTC),
                http_status=200,
                body="<main>No results</main>",
            ),
            result_inspector=lambda result: ("unrelated", "no matching resource"),
        ),
    )

    assert [lead.name for lead in result.elicitation.leads] == ["Kept Lead"]
    assert len(result.elicitation.quarantines) == 1
    assert result.elicitation.quarantines[0].scope == "candidate"
    assert result.metrics["model_leads"] == 1
    assert result.metrics["elicitation_quarantined_candidates"] == 1
    assert result.metrics["elicitation_retries"] == 0
    assert result.metrics["elicitation_blocked_responses"] == 0
    assert result.as_json()["metrics"] == result.metrics


def test_production_path_enforces_order_channels_inspection_and_exclusions() -> None:
    events: list[str] = []
    requests: list[SearchRequest] = []
    inspected: list[str] = []
    model = EmptyModel(events)

    def vocabulary_transport(url: str, *, max_bytes: int) -> FetchedDocument:
        events.append("vocabulary")
        return FetchedDocument(
            url,
            "text/html",
            b"<h1>Middle High German corpus using HiTS</h1>",
        )

    def provider_fetch(request: SearchRequest) -> ProviderResponse:
        events.append("provider")
        requests.append(request)
        excluded = '-"' in request.query
        if request.response_format is ResponseFormat.RSS:
            body = (
                "<rss><channel><item><title>Found Tool</title>"
                "<link>https://example.org/found</link></item></channel></rss>"
                if excluded
                else "<rss><channel/></rss>"
            )
        else:
            body = (
                '<a href="https://example.org/found">Found Tool</a>'
                if excluded
                else "<main>No results</main>"
            )
        return ProviderResponse(
            retrieval_mode="bounded_http",
            observed_at=datetime(2026, 8, 12, tzinfo=UTC),
            http_status=200,
            body=body,
        )

    def inspect(
        result: SearchResult,
    ) -> tuple[ResultClassification, str]:
        inspected.append(result.url)
        return "lead", "untrusted lead requiring canonical verification"

    result = run_discovery(
        DiscoveryConfig(
            category="tool",
            stage=LanguageStage.MHG,
            max_mined_terms=1,
            max_exclusion_groups=1,
            vocabulary=VocabularyLimits(max_pages=1, max_terms=4),
        ),
        DiscoveryDependencies(
            catalog=load_catalog(),
            model_call=model,
            vocabulary_transport=vocabulary_transport,
            provider_fetch=provider_fetch,
            result_inspector=inspect,
        ),
    )

    assert model.calls == 2
    assert events.index("model") < events.index("vocabulary") < events.index("provider")
    assert result.elicitation.leads == ()
    first_round = [request for request in requests if '-"' not in request.query]
    assert {request.channel for request in first_round} == {
        "general_web_google",
        "general_web_bing",
        "general_web_brave",
        "clarin",
        "olac",
        "zenodo",
        "institutional",
        "github",
        "gitlab",
        "huggingface",
    }
    assert any('-"' in request.query for request in requests)
    assert all(request.retrieval_mode == "bounded_http" for request in requests)
    assert inspected
    assert result.leads[0].trusted_evidence is False
    assert result.metrics["focused_queries_attempted"] == len(requests)
    assert result.metrics["model_leads"] == 0
    serialized_assessments = result.as_json()["assessments"]
    assert isinstance(serialized_assessments, list)
    assert "gitlab" in {
        assessment["provider"]
        for assessment in serialized_assessments
        if isinstance(assessment, dict)
    }


def test_provider_failure_is_audited_without_stopping_required_channels() -> None:
    requests: list[SearchRequest] = []

    def fail(request: SearchRequest) -> ProviderResponse:
        requests.append(request)
        raise OSError("synthetic network gap")

    result = run_discovery(
        DiscoveryConfig(
            category="dictionary",
            stage=LanguageStage.OHG,
            max_mined_terms=0,
            max_exclusion_groups=1,
            vocabulary=VocabularyLimits(max_pages=1),
        ),
        DiscoveryDependencies(
            catalog=load_catalog(),
            model_call=EmptyModel([]),
            vocabulary_transport=lambda url, *, max_bytes: FetchedDocument(
                url, "text/plain", b"Old High German dictionary"
            ),
            provider_fetch=fail,
            result_inspector=lambda result: ("unrelated", "unused"),
        ),
    )

    assert {request.channel for request in requests} >= {
        "general_web_google",
        "general_web_bing",
        "general_web_brave",
        "clarin",
        "olac",
        "zenodo",
        "institutional",
        "github",
        "gitlab",
        "huggingface",
    }
    assert all(record.assessment == "transport_error" for record in result.assessments)
    assert all(record.failure_stage == "request" for record in result.assessments)
    assert all(
        "bounded_http transport" in record.observation for record in result.assessments
    )


def test_provider_aware_exact_variants_fallbacks_and_exclusions() -> None:
    requests: list[SearchRequest] = []

    def provider_fetch(request: SearchRequest) -> ProviderResponse:
        requests.append(request)
        body = (
            "<rss><channel/></rss>"
            if request.response_format is ResponseFormat.RSS
            else "<main>No results</main>"
        )
        return ProviderResponse(
            retrieval_mode="bounded_http",
            observed_at=datetime(2026, 8, 12, tzinfo=UTC),
            http_status=200,
            body=body,
        )

    result = run_discovery(
        DiscoveryConfig(
            category="tool",
            stage=LanguageStage.MHG,
            max_mined_terms=0,
            max_exclusion_groups=1,
            exclusion_group_size=1,
            vocabulary=VocabularyLimits(max_pages=1),
        ),
        DiscoveryDependencies(
            catalog=load_catalog(),
            model_call=EmptyModel([]),
            vocabulary_transport=lambda url, *, max_bytes: FetchedDocument(
                url, "text/plain", b""
            ),
            provider_fetch=provider_fetch,
            result_inspector=lambda result: ("unrelated", "no matching resource"),
        ),
    )

    exact_parser = '"Middle High German" parser'
    assert any(
        request.channel == "general_web_bing" and request.query == exact_parser
        for request in requests
    )
    assert any(
        request.channel == "institutional" and request.query == exact_parser
        for request in requests
    )
    assert any(
        request.channel == "clarin" and request.query == "Middle High German parser"
        for request in requests
    )

    bing_queries = [
        request.query for request in requests if request.channel == "general_web_bing"
    ]
    assert any(
        query.startswith('"Middle High German" "dependency parser" -"')
        for query in bing_queries
    )
    assert any(query.startswith('MHG dependency parser -"') for query in bing_queries)
    assert not any(
        query.startswith('"Middle High German" "parser"') for query in bing_queries
    )
    assert len(bing_queries) == len(set(bing_queries))

    clarin_queries = [
        request.query for request in requests if request.channel == "clarin"
    ]
    assert not any(query.startswith("MHG ") for query in clarin_queries)
    assert not any('"dependency parser"' in query for query in clarin_queries)
    strict_exclusion = next(
        query
        for query in bing_queries
        if query.startswith('"Middle High German" "dependency parser" -"')
    )
    assert strict_exclusion.count('"') == 6
    assert result.metrics["focused_queries_attempted"] == len(requests)
    assert [record.query for record in result.assessments] == [
        request.query for request in requests
    ]


def test_model_terms_abbreviation_and_cross_channel_metadata_pivots() -> None:
    requests: list[SearchRequest] = []

    def provider_fetch(request: SearchRequest) -> ProviderResponse:
        requests.append(request)
        if request.channel == "github" and request.query == "Middle High German BERT":
            body = (
                '<a href="https://github.com/source-lab/historical-model">'
                "Generic historical model. README: Middle High German "
                "BERT tokenizer with word embeddings. Architecture: HistBERT; "
                "alias: ArchiveEncoder; tokenizer and "
                "word embeddings. Model card: "
                "https://huggingface.co/different-handle/mhg-model and "
                "https://huggingface.co/different-handle/mhg-model</a>"
            )
        else:
            body = (
                "<rss><channel/></rss>"
                if request.response_format is ResponseFormat.RSS
                else "<main>No results</main>"
            )
        return ProviderResponse(
            retrieval_mode="bounded_http",
            observed_at=datetime(2026, 8, 12, tzinfo=UTC),
            http_status=200,
            body=body,
        )

    result = run_discovery(
        DiscoveryConfig(
            category="tool",
            stage=LanguageStage.MHG,
            max_mined_terms=0,
            max_exclusion_groups=1,
            vocabulary=VocabularyLimits(max_pages=1),
        ),
        DiscoveryDependencies(
            catalog=load_catalog(),
            model_call=EmptyModel([]),
            vocabulary_transport=lambda url, *, max_bytes: FetchedDocument(
                url, "text/plain", b""
            ),
            provider_fetch=provider_fetch,
            result_inspector=lambda result: (
                ("lead", "canonical metadata inspected")
                if result.url == "https://github.com/source-lab/historical-model"
                else ("unrelated", "not the synthetic repository")
            ),
        ),
    )

    assert any(request.query == "MHG BERT" for request in requests)
    assert any(
        request.channel == "huggingface"
        and request.query == "Middle High German different-handle mhg-model"
        for request in requests
    )
    assert any(
        request.channel == "huggingface"
        and request.query == "Middle High German HistBERT"
        for request in requests
    )
    assert (
        sum(
            request.channel == "huggingface"
            and request.query == "Middle High German different-handle mhg-model"
            for request in requests
        )
        == 1
    )
    assert all(query.trusted_evidence is False for query in result.queries)
    assert not result.complete
    assert any("next_page_unavailable" in gap for gap in result.completion_gaps)


@pytest.mark.parametrize(
    ("url", "source_channel", "expected"),
    [
        (
            "https://gitlab.com/history-lab/mhg-model",
            "github",
            ("history-lab mhg-model", "gitlab"),
        ),
        (
            "https://gitlab.com/history-lab/corpora/medieval/nlp/mhg-model",
            "github",
            ("history-lab corpora medieval mhg-model", "gitlab"),
        ),
        (
            "https://github.com/different-owner/mhg-model",
            "gitlab",
            ("different-owner mhg-model", "github"),
        ),
        (
            "https://huggingface.co/models/card-owner/mhg-model",
            "github",
            ("card-owner mhg-model", "huggingface"),
        ),
        (
            "https://zenodo.org/records/123456",
            "huggingface",
            ("Zenodo 123456", "zenodo"),
        ),
        (
            "https://doi.org/10.5281/zenodo.123456",
            "huggingface",
            ("10.5281 zenodo.123456", "general_web_google"),
        ),
        (
            "https://institute.example.edu/research/projects/mhg",
            "clarin",
            ("mhg", "institutional"),
        ),
        (
            "https://gitlab.com/research/mhg-model",
            "zenodo",
            ("research mhg-model", "gitlab"),
        ),
        (
            "https://huggingface.co/paper-author/mhg-card",
            "zenodo",
            ("paper-author mhg-card", "huggingface"),
        ),
    ],
)
def test_generic_provenance_link_pivots(
    url: str,
    source_channel: str,
    expected: tuple[str, str],
) -> None:
    assert _cross_channel_pivot(url, source_channel) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://gitlab.com/history-lab/mhg-model",
        "https://localhost/history-lab/mhg-model",
        "https://127.0.0.1/history-lab/mhg-model",
        "https://10.0.0.4/history-lab/mhg-model",
        "https://127-0-0-1.nip.io/history-lab/mhg-model",
        "https://user:secret@gitlab.com/history-lab/mhg-model",
        "file:///history-lab/mhg-model",
        "https://gitlab.com/explore/projects",
        "https://github.com/history-lab/mhg-model/issues",
    ],
)
def test_provenance_pivots_reject_unsafe_or_noncanonical_urls(url: str) -> None:
    assert _cross_channel_pivot(url, "zenodo") is None


def test_follow_up_bound_exhaustion_remains_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "histgerm.research.discovery_orchestration._MAX_FOLLOW_UP_QUERIES", 1
    )

    def provider_fetch(request: SearchRequest) -> ProviderResponse:
        body = (
            '<a href="https://github.com/source-lab/historical-model">'
            "alias: FirstHandle alias: SecondHandle</a>"
            if request.channel == "github"
            and request.query == "Middle High German BERT"
            else (
                "<rss><channel/></rss>"
                if request.response_format is ResponseFormat.RSS
                else "<main>No results</main>"
            )
        )
        return ProviderResponse(
            retrieval_mode="bounded_http",
            observed_at=datetime(2026, 8, 12, tzinfo=UTC),
            http_status=200,
            body=body,
            exhausted=True,
        )

    result = run_discovery(
        DiscoveryConfig(
            category="tool",
            stage=LanguageStage.MHG,
            max_mined_terms=0,
            max_exclusion_groups=1,
            vocabulary=VocabularyLimits(max_pages=1),
        ),
        DiscoveryDependencies(
            catalog=load_catalog(),
            model_call=EmptyModel([]),
            vocabulary_transport=lambda url, *, max_bytes: FetchedDocument(
                url, "text/plain", b""
            ),
            provider_fetch=provider_fetch,
            result_inspector=lambda result: (
                ("lead", "synthetic provenance metadata")
                if result.url == "https://github.com/source-lab/historical-model"
                else ("unrelated", "not the synthetic repository")
            ),
        ),
    )

    assert not result.complete
    assert any("follow-up limit 1 reached" in gap for gap in result.completion_gaps)


def test_gmh_recall_executes_last_and_is_bounded_by_query_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries = (
        FocusedQuery(
            category="tool",
            stage=LanguageStage.MHG,
            language="de",
            family="tagging",
            stage_term="Mittelhochdeutsch",
            concept="Tagger",
        ),
        FocusedQuery(
            category="tool",
            stage=LanguageStage.MHG,
            language="en",
            family="tagging",
            stage_term="Middle High German",
            concept="POS tagger",
        ),
        FocusedQuery(
            category="tool",
            stage=LanguageStage.MHG,
            language="en",
            family="parsing",
            stage_term="Middle High German",
            concept="dependency parser",
        ),
    )
    monkeypatch.setattr(
        "histgerm.research.discovery_orchestration._queries",
        lambda config, vocabulary: queries,
    )
    requests: list[SearchRequest] = []

    def provider_fetch(request: SearchRequest) -> ProviderResponse:
        requests.append(request)
        return ProviderResponse(
            retrieval_mode="bounded_http",
            observed_at=datetime(2026, 8, 12, tzinfo=UTC),
            http_status=200,
            body=(
                "<rss><channel/></rss>"
                if request.response_format is ResponseFormat.RSS
                else "<main>No results</main>"
            ),
            exhausted=True,
        )

    run_discovery(
        DiscoveryConfig(
            category="tool",
            stage=LanguageStage.MHG,
            max_mined_terms=0,
            max_exclusion_groups=1,
            vocabulary=VocabularyLimits(max_pages=1),
        ),
        DiscoveryDependencies(
            catalog=load_catalog(),
            model_call=EmptyModel([]),
            vocabulary_transport=lambda url, *, max_bytes: FetchedDocument(
                url, "text/plain", b""
            ),
            provider_fetch=provider_fetch,
            result_inspector=lambda result: ("unrelated", "synthetic"),
        ),
    )

    gmh = [request for request in requests if request.query.startswith("gmh ")]
    assert [request.query for request in gmh] == [
        "gmh Tagger",
        "gmh Tagger",
        "gmh Tagger",
        "gmh dependency parser",
        "gmh dependency parser",
        "gmh dependency parser",
    ]
    assert {request.channel for request in gmh} == {
        "general_web_google",
        "general_web_bing",
        "general_web_brave",
    }
    assert _controlled_recall_queries(queries) == (queries[0], queries[2])
    last_mhg = max(
        index
        for index, request in enumerate(requests)
        if request.query.startswith("MHG ")
    )
    first_gmh = min(
        index
        for index, request in enumerate(requests)
        if request.query.startswith("gmh ")
    )
    assert last_mhg < first_gmh


def test_negative_claim_enqueues_only_bounded_task_gap_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = FocusedQuery(
        category="tool",
        stage=LanguageStage.MHG,
        language="en",
        family="tagging",
        stage_term="Middle High German",
        concept="tagger",
    )
    monkeypatch.setattr(
        "histgerm.research.discovery_orchestration._queries",
        lambda config, vocabulary: (initial,),
    )
    requests: list[SearchRequest] = []

    def provider_fetch(request: SearchRequest) -> ProviderResponse:
        requests.append(request)
        body = (
            '<a href="https://example.org/gap" '
            'data-snippet="No named-entity recognition model exists.">'
            "Unverified gap claim</a>"
            if request.channel == "github"
            and request.query == "Middle High German tagger"
            else (
                "<rss><channel/></rss>"
                if request.response_format is ResponseFormat.RSS
                else "<main>No results</main>"
            )
        )
        return ProviderResponse(
            retrieval_mode="bounded_http",
            observed_at=datetime(2026, 8, 12, tzinfo=UTC),
            http_status=200,
            body=body,
            exhausted=True,
        )

    result = run_discovery(
        DiscoveryConfig(
            category="tool",
            stage=LanguageStage.MHG,
            max_mined_terms=0,
            max_exclusion_groups=1,
            vocabulary=VocabularyLimits(max_pages=1),
        ),
        DiscoveryDependencies(
            catalog=load_catalog(),
            model_call=EmptyModel([]),
            vocabulary_transport=lambda url, *, max_bytes: FetchedDocument(
                url, "text/plain", b""
            ),
            provider_fetch=provider_fetch,
            result_inspector=lambda result: (
                ("lead", "untrusted negative gap wording")
                if result.url == "https://example.org/gap"
                else ("unrelated", "synthetic")
            ),
        ),
    )

    gap_requests = [
        request
        for request in requests
        if request.query == '"Middle High German" named-entity recognition'
    ]
    assert [request.channel for request in gap_requests] == [
        "general_web_google",
        "general_web_bing",
        "general_web_brave",
    ]
    assert (
        sum(query.family == "untrusted_metadata_lead" for query in result.queries) == 1
    )


def test_orchestration_inspects_and_deduplicates_multiple_provider_pages() -> None:
    requests: list[SearchRequest] = []

    def provider_fetch(request: SearchRequest) -> ProviderResponse:
        requests.append(request)
        if request.channel == "github" and request.query == "Middle High German BERT":
            second_page = "p=2" in request.url
            body = (
                '<a href="https://example.org/model">Generic model</a>'
                '<a href="https://example.org/second">Second model</a>'
                if second_page
                else '<a href="https://example.org/model">Generic model</a>'
            )
            return ProviderResponse(
                retrieval_mode="bounded_http",
                observed_at=datetime(2026, 8, 12, tzinfo=UTC),
                http_status=200,
                body=body,
                next_cursor=None if second_page else "2",
                exhausted=second_page,
            )
        body = (
            "<rss><channel/></rss>"
            if request.response_format is ResponseFormat.RSS
            else "<main>No results</main>"
        )
        return ProviderResponse(
            retrieval_mode="bounded_http",
            observed_at=datetime(2026, 8, 12, tzinfo=UTC),
            http_status=200,
            body=body,
            exhausted=True,
        )

    result = run_discovery(
        DiscoveryConfig(
            category="tool",
            stage=LanguageStage.MHG,
            max_mined_terms=0,
            max_exclusion_groups=1,
            vocabulary=VocabularyLimits(max_pages=1),
        ),
        DiscoveryDependencies(
            catalog=load_catalog(),
            model_call=EmptyModel([]),
            vocabulary_transport=lambda url, *, max_bytes: FetchedDocument(
                url, "text/plain", b""
            ),
            provider_fetch=provider_fetch,
            result_inspector=lambda result: ("lead", "inspected synthetic page"),
        ),
    )

    bert_requests = [
        request
        for request in requests
        if request.channel == "github" and request.query == "Middle High German BERT"
    ]
    assert len(bert_requests) == 2
    assert "p=2" in bert_requests[1].url
    bert_attempts = [
        record
        for record in result.assessments
        if record.channel == "github" and record.query == "Middle High German BERT"
    ]
    assert [record.page_number for record in bert_attempts] == [1, 2]
    assert all(record.pagination_state == "complete" for record in bert_attempts)
    assert sum(lead.url == "https://example.org/model" for lead in result.leads) == 1
    assert any(lead.url == "https://example.org/second" for lead in result.leads)


def test_completion_gaps_cover_pending_leads_and_architecture_families() -> None:
    config = DiscoveryConfig(category="tool", stage=LanguageStage.MHG)
    sparse_query = FocusedQuery(
        category="tool",
        stage=LanguageStage.MHG,
        language="en",
        family="models",
        stage_term="Middle High German",
        concept="language model",
    )

    gaps = _completion_gaps(
        config,
        (sparse_query,),
        (),
        follow_up_limit_reached=True,
    )

    assert any("follow-up limit" in gap for gap in gaps)
    assert any("architecture families unqueried" in gap for gap in gaps)


def _catalog(*urls: str) -> Catalog:
    template = load_catalog().tools[0]
    tools = [
        template.model_copy(
            update={
                "id": f"tool-synthetic-{index}",
                "name": f"Synthetic Tool {index}",
                "links": {"homepage": url},
                "sources": [],
                "hugging_face_links": [],
                "supported_stages": [LanguageStage.MHG],
            }
        )
        for index, url in enumerate(urls, start=1)
    ]
    return Catalog(tools=tools)


def _shared_source_catalog(
    url: str,
    *,
    corpus_stages: tuple[LanguageStage, ...] = (),
    tool_stages: tuple[LanguageStage, ...] = (),
) -> Catalog:
    catalog = load_catalog()

    def shared_sources(record: BaseResource) -> list[Source]:
        return [source.model_copy(update={"url": url}) for source in record.sources]

    corpora = []
    if corpus_stages:
        corpus = catalog.corpora[0]
        corpora.append(
            corpus.model_copy(
                update={
                    "links": {"homepage": url},
                    "sources": shared_sources(corpus),
                    "covered_stages": list(corpus_stages),
                    "versions": [
                        version.model_copy(update={"links": {"homepage": url}})
                        for version in corpus.versions
                    ],
                }
            )
        )
    tools = [
        catalog.tools[0].model_copy(
            update={
                "id": f"tool-synthetic-shared-{stage.value}",
                "links": {"homepage": url},
                "sources": shared_sources(catalog.tools[0]),
                "hugging_face_links": [url],
                "supported_stages": [stage],
            }
        )
        for stage in tool_stages
    ]
    return Catalog(corpora=corpora, tools=tools)


def _write_empty_vocabulary(path: Path, *, on: date) -> None:
    path.write_bytes(
        serialize_vocabulary(
            DiscoveryVocabulary(
                schema_version=1,
                revision=0,
                updated_on=on,
                sources=[],
                terms=[],
            )
        )
    )


def _crawl_result(url: str, markdown: str) -> Crawl4AIResult:
    import hashlib

    digest = hashlib.sha256(markdown.encode()).hexdigest()
    return Crawl4AIResult(
        source_url=url,
        final_url=url,
        cleaned_markdown=markdown,
        structured_metadata={"title": "Historical Tagger"},
        page_links=(),
        response_metadata={"headers": {"etag": '"fixture"'}},
        raw_content_sha256=digest,
        cleaned_content_sha256=digest,
        crawl_cache_key=f"cache-{digest}",
        cache_disposition=CacheDisposition.MISS,
        crawler_version="0.7.4",
    )


class FixtureCrawler:
    def __init__(
        self,
        pages: dict[str, str],
        *,
        failures: set[str] | None = None,
    ) -> None:
        self.pages = pages
        self.failures = failures or set()
        self.calls: list[tuple[str, bool]] = []

    async def render(self, url: str, *, refresh: bool = False) -> Crawl4AIResult:
        self.calls.append((url, refresh))
        if url in self.failures:
            raise Crawl4AIAdapterError("synthetic barrier", stage="challenge")
        return _crawl_result(url, self.pages[url])


class AcceptingClassifier:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, str], ...]] = []

    def __call__(
        self,
        candidates: tuple[ClassifierCandidate, ...],
        *,
        max_terms: int,
    ) -> Sequence[CandidateDecision]:
        self.calls.append(
            tuple(
                (candidate.normalized, candidate.suggested_kind.value)
                for candidate in candidates
            )
        )
        return [
            CandidateDecision(
                candidate.normalized,
                candidate.suggested_kind,
                True,
            )
            for candidate in candidates[:max_terms]
        ]


def _empty_provider(request: SearchRequest) -> ProviderResponse:
    body = (
        "<rss><channel/></rss>"
        if request.response_format is ResponseFormat.RSS
        else "<main>No results</main>"
    )
    return ProviderResponse(
        retrieval_mode="bounded_http",
        observed_at=datetime(2026, 8, 12, tzinfo=UTC),
        http_status=200,
        body=body,
    )


def _incremental_run(
    path: Path,
    catalog: Catalog,
    crawler: FixtureCrawler,
    classifier: AcceptingClassifier,
    *,
    on: date,
    provider_fetch: ProviderFetch = _empty_provider,
) -> DiscoveryRunResult:
    return run_discovery(
        DiscoveryConfig(
            category="tool",
            stage=LanguageStage.MHG,
            max_mined_terms=2,
            max_exclusion_groups=1,
            run_on=on,
            vocabulary=VocabularyLimits(max_pages=8),
        ),
        DiscoveryDependencies(
            catalog=catalog,
            model_call=EmptyModel([]),
            provider_fetch=provider_fetch,
            result_inspector=lambda result: ("unrelated", "offline"),
            vocabulary_path=path,
            vocabulary_crawler=crawler,
            vocabulary_classifier=classifier,
        ),
    )


def test_incremental_new_then_fresh_reuse_has_zero_external_mining_calls(
    tmp_path: Path,
) -> None:
    on = date(2026, 8, 12)
    url = "https://www.synthetic.org/project"
    path = tmp_path / "vocabulary.yaml"
    _write_empty_vocabulary(path, on=on)
    crawler = FixtureCrawler(
        {url: "A Middle High German tool for Part-of-speech tagging with HiTS."}
    )
    classifier = AcceptingClassifier()

    first = _incremental_run(path, _catalog(url), crawler, classifier, on=on)
    first_classifier_calls = len(classifier.calls)
    second = _incremental_run(
        path, _catalog(url), crawler, classifier, on=on + timedelta(days=1)
    )

    assert crawler.calls == [(url, False)]
    assert first_classifier_calls > 0
    assert len(classifier.calls) == first_classifier_calls
    assert first.vocabulary_revision == 1
    assert second.vocabulary_revision == 1
    assert second.metrics["vocabulary_sources_refreshed"] == 0
    assert second.metrics["vocabulary_sources_reused"] == 1
    assert any(query.family.startswith("mined_") for query in second.queries)


def test_expired_unchanged_digest_refreshes_without_classification(
    tmp_path: Path,
) -> None:
    on = date(2026, 8, 12)
    url = "https://www.synthetic.org/project"
    markdown = "A Middle High German tool using HiTS."
    path = tmp_path / "vocabulary.yaml"
    _write_empty_vocabulary(path, on=on)
    crawler = FixtureCrawler({url: markdown})
    classifier = AcceptingClassifier()
    _incremental_run(path, _catalog(url), crawler, classifier, on=on)
    classifier_calls = len(classifier.calls)

    result = _incremental_run(
        path, _catalog(url), crawler, classifier, on=on + timedelta(days=30)
    )

    assert crawler.calls == [(url, False), (url, True)]
    assert len(classifier.calls) == classifier_calls
    assert result.vocabulary_revision == 2
    assert load_vocabulary(path).sources[0].last_successful_on == on + timedelta(
        days=30
    )


def test_changed_source_reuses_decisions_and_inactivates_missing_associations(
    tmp_path: Path,
) -> None:
    on = date(2026, 8, 12)
    url = "https://www.synthetic.org/project"
    path = tmp_path / "vocabulary.yaml"
    _write_empty_vocabulary(path, on=on)
    crawler = FixtureCrawler({url: "A Middle High German tool using HiTS and STTS."})
    classifier = AcceptingClassifier()
    _incremental_run(path, _catalog(url), crawler, classifier, on=on)
    crawler.pages[url] = "A Middle High German tool using HiTS and ANNIS."

    result = _incremental_run(
        path, _catalog(url), crawler, classifier, on=on + timedelta(days=30)
    )
    stored = load_vocabulary(path)
    stts = next(term for term in stored.terms if term.normalized == "stts")

    assert cast(int, result.metrics["vocabulary_reused_decisions"]) > 0
    assert cast(int, result.metrics["vocabulary_inactive_associations"]) > 0
    assert not stts.active
    assert stts.wordings[0].inactive_source_urls == [url]
    assert classifier.calls[-1]
    assert all(key[0] == "annis" for key in classifier.calls[-1])


def test_access_gap_preserves_terms_and_records_bounded_retry(
    tmp_path: Path,
) -> None:
    on = date(2026, 8, 12)
    url = "https://www.synthetic.org/project"
    path = tmp_path / "vocabulary.yaml"
    _write_empty_vocabulary(path, on=on)
    crawler = FixtureCrawler({url: "A Middle High German tool using HiTS."})
    classifier = AcceptingClassifier()
    _incremental_run(path, _catalog(url), crawler, classifier, on=on)
    crawler.failures.add(url)

    result = _incremental_run(
        path, _catalog(url), crawler, classifier, on=on + timedelta(days=30)
    )
    stored = load_vocabulary(path)

    assert result.metrics["vocabulary_access_gaps"] == 1
    assert any(term.active for term in stored.terms)
    assert stored.sources[0].gap is not None
    assert stored.sources[0].gap.retry_after == on + timedelta(days=37)


def test_orphaning_keeps_multisource_term_active_and_provider_failure_keeps_write(
    tmp_path: Path,
) -> None:
    on = date(2026, 8, 12)
    first_url = "https://www.synthetic.org/one"
    second_url = "https://www.synthetic.org/two"
    path = tmp_path / "vocabulary.yaml"
    _write_empty_vocabulary(path, on=on)
    crawler = FixtureCrawler(
        {
            first_url: "A Middle High German tool using HiTS.",
            second_url: "A Middle High German tool using HiTS.",
        }
    )
    classifier = AcceptingClassifier()
    _incremental_run(path, _catalog(first_url, second_url), crawler, classifier, on=on)

    def failed_provider(request: SearchRequest) -> ProviderResponse:
        raise OSError("synthetic provider failure")

    result = _incremental_run(
        path,
        _catalog(second_url),
        crawler,
        classifier,
        on=on + timedelta(days=1),
        provider_fetch=failed_provider,
    )
    stored = load_vocabulary(path)
    hits = next(
        term
        for term in stored.terms
        if term.normalized == "hits" and term.kind.value == "tagset_standard"
    )

    assert result.vocabulary_revision == 2
    assert all(record.assessment == "transport_error" for record in result.assessments)
    assert hits.active
    assert hits.wordings[0].source_urls == [second_url]
    assert hits.wordings[0].inactive_source_urls == [first_url]
    assert next(
        source for source in stored.sources if source.url == first_url
    ).status == ("orphaned")


def test_stale_revision_from_concurrent_update_surfaces(
    tmp_path: Path,
) -> None:
    on = date(2026, 8, 12)
    url = "https://www.synthetic.org/project"
    path = tmp_path / "vocabulary.yaml"
    _write_empty_vocabulary(path, on=on)

    class RacingCrawler(FixtureCrawler):
        async def render(self, url: str, *, refresh: bool = False) -> Crawl4AIResult:
            current = load_vocabulary(path)
            apply_vocabulary(path, current, expected_revision=current.revision)
            return await super().render(url, refresh=refresh)

    with pytest.raises(VocabularyRevisionError, match="expected revision 0, found 1"):
        _incremental_run(
            path,
            _catalog(url),
            RacingCrawler({url: "A Middle High German tool using HiTS."}),
            AcceptingClassifier(),
            on=on,
        )


def test_invalid_vocabulary_is_not_bootstrapped_or_retrieved(
    tmp_path: Path,
) -> None:
    path = tmp_path / "vocabulary.yaml"
    invalid = b"schema_version: 2\nrevision: 0\n"
    path.write_bytes(invalid)
    url = "https://www.synthetic.org/project"
    crawler = FixtureCrawler({url: "A Middle High German tool using HiTS."})

    with pytest.raises(ValueError):
        _incremental_run(
            path,
            _catalog(url),
            crawler,
            AcceptingClassifier(),
            on=date(2026, 8, 12),
        )

    assert crawler.calls == []
    assert path.read_bytes() == invalid


def test_active_vocabulary_is_scoped_to_current_source_category_and_stage() -> None:
    on = date(2026, 8, 12)
    matching_url = "https://www.synthetic.org/mhg-tool"
    other_stage_url = "https://www.synthetic.org/ohg-tool"
    other_category_url = "https://www.synthetic.org/mhg-corpus"
    orphaned_url = "https://www.synthetic.org/orphaned"
    catalog = load_catalog()
    mhg_tool = catalog.tools[0].model_copy(
        update={
            "id": "tool-synthetic-mhg",
            "links": {"homepage": matching_url},
            "sources": [],
            "hugging_face_links": [],
            "supported_stages": [LanguageStage.MHG],
        }
    )
    ohg_tool = catalog.tools[0].model_copy(
        update={
            "id": "tool-synthetic-ohg",
            "links": {"homepage": other_stage_url},
            "sources": [],
            "hugging_face_links": [],
            "supported_stages": [LanguageStage.OHG],
        }
    )
    mhg_corpus = catalog.corpora[0].model_copy(
        update={
            "links": {"homepage": other_category_url},
            "covered_stages": [LanguageStage.MHG],
        }
    )
    synthetic_catalog = Catalog(
        tools=[mhg_tool, ohg_tool],
        corpora=[mhg_corpus],
    )
    sources = [
        VocabularySource(
            url=url,
            resource_ids=[resource_id],
            source_fields=["links.homepage"],
            refresh_after=on + timedelta(days=30),
            status="active",
            extractor_version=1,
        )
        for url, resource_id in (
            (matching_url, mhg_tool.id),
            (other_stage_url, ohg_tool.id),
            (other_category_url, mhg_corpus.id),
        )
    ]
    sources.append(
        VocabularySource(
            url=orphaned_url,
            resource_ids=[],
            source_fields=[],
            refresh_after=on + timedelta(days=30),
            status="orphaned",
            extractor_version=1,
        )
    )
    term = VocabularyTerm(
        normalized="synthetic tagging",
        kind="task",
        active=True,
        contexts=[
            VocabularyContext(category="tool", stage=LanguageStage.MHG),
            VocabularyContext(category="tool", stage=LanguageStage.OHG),
            VocabularyContext(category="corpus", stage=LanguageStage.MHG),
        ],
        wordings=[
            VocabularyWording(
                value="Matching wording",
                source_urls=[matching_url],
                first_seen_on=on,
                last_seen_on=on,
            ),
            VocabularyWording(
                value="Other-stage wording",
                source_urls=[other_stage_url],
                first_seen_on=on,
                last_seen_on=on,
            ),
            VocabularyWording(
                value="Other-category wording",
                source_urls=[other_category_url],
                first_seen_on=on,
                last_seen_on=on,
            ),
            VocabularyWording(
                value="Inactive wording",
                inactive_source_urls=[matching_url],
                first_seen_on=on,
                last_seen_on=on,
            ),
            VocabularyWording(
                value="Orphaned wording",
                inactive_source_urls=[orphaned_url],
                first_seen_on=on,
                last_seen_on=on,
            ),
            VocabularyWording(
                value="Multi-source wording",
                source_urls=[matching_url, other_stage_url],
                first_seen_on=on,
                last_seen_on=on,
            ),
        ],
    )
    vocabulary = DiscoveryVocabulary(
        schema_version=1,
        revision=0,
        updated_on=on,
        sources=sources,
        terms=[term],
    )

    active_vocabulary = _active_inventory_vocabulary(
        vocabulary,
        synthetic_catalog,
        "tool",
        LanguageStage.MHG,
    )

    assert len(active_vocabulary.terms) == 1
    active = active_vocabulary.terms[0]
    assert active.wordings == ("Matching wording", "Multi-source wording")
    assert active.source_urls == (matching_url,)


def test_shared_source_persists_only_supported_category_stage_observations(
    tmp_path: Path,
) -> None:
    on = date(2026, 8, 12)
    url = "https://www.synthetic.org/shared"
    shared_catalog = _shared_source_catalog(
        url,
        corpus_stages=(LanguageStage.MHG,),
        tool_stages=(LanguageStage.OHG, LanguageStage.MHG),
    )
    path = tmp_path / "vocabulary.yaml"
    _write_empty_vocabulary(path, on=on)

    _incremental_run(
        path,
        shared_catalog,
        FixtureCrawler(
            {url: ("A Middle High German corpus and Old High German tool using HiTS.")}
        ),
        AcceptingClassifier(),
        on=on,
    )

    stored = load_vocabulary(path)
    hits = next(
        term
        for term in stored.terms
        if term.normalized == "hits" and term.kind.value == "tagset_standard"
    )
    assert {(context.category, context.stage) for context in hits.contexts} == {
        ("corpus", LanguageStage.MHG),
        ("tool", LanguageStage.MHG),
        ("tool", LanguageStage.OHG),
    }
    supported_contexts: tuple[tuple[ResourceCategory, LanguageStage], ...] = (
        ("corpus", LanguageStage.MHG),
        ("tool", LanguageStage.MHG),
        ("tool", LanguageStage.OHG),
    )
    for category, stage in supported_contexts:
        active = _active_inventory_vocabulary(
            stored,
            shared_catalog,
            category,
            stage,
        )
        assert any(term.normalized == "hits" for term in active.terms)

    unsupported = _active_inventory_vocabulary(
        stored,
        shared_catalog,
        "corpus",
        LanguageStage.OHG,
    )
    assert all(term.normalized != "hits" for term in unsupported.terms)


def test_fresh_reuse_applies_provenance_only_context_add_and_remove(
    tmp_path: Path,
) -> None:
    on = date(2026, 8, 12)
    url = "https://www.synthetic.org/provenance"
    path = tmp_path / "vocabulary.yaml"
    _write_empty_vocabulary(path, on=on)
    crawler = FixtureCrawler(
        {url: ("A Middle High German tool and Old High German corpus using HiTS.")}
    )
    classifier = AcceptingClassifier()
    tool_catalog = _shared_source_catalog(
        url,
        tool_stages=(LanguageStage.MHG,),
    )
    _incremental_run(path, tool_catalog, crawler, classifier, on=on)
    classifier_calls = len(classifier.calls)

    added_catalog = _shared_source_catalog(
        url,
        corpus_stages=(LanguageStage.OHG,),
        tool_stages=(LanguageStage.MHG,),
    )
    _incremental_run(
        path,
        added_catalog,
        crawler,
        classifier,
        on=on + timedelta(days=1),
    )
    added = load_vocabulary(path)
    added_hits = next(term for term in added.terms if term.normalized == "hits")

    assert crawler.calls == [(url, False)]
    assert len(classifier.calls) == classifier_calls
    assert ("corpus", LanguageStage.OHG) in {
        (context.category, context.stage) for context in added_hits.contexts
    }
    assert any(
        term.normalized == "hits"
        for term in _active_inventory_vocabulary(
            added,
            added_catalog,
            "corpus",
            LanguageStage.OHG,
        ).terms
    )

    corpus_catalog = _shared_source_catalog(
        url,
        corpus_stages=(LanguageStage.OHG,),
    )
    _incremental_run(
        path,
        corpus_catalog,
        crawler,
        classifier,
        on=on + timedelta(days=2),
    )
    removed = load_vocabulary(path)

    assert crawler.calls == [(url, False)]
    assert len(classifier.calls) == classifier_calls
    assert all(
        term.normalized != "hits"
        for term in _active_inventory_vocabulary(
            removed,
            corpus_catalog,
            "tool",
            LanguageStage.MHG,
        ).terms
    )
    assert any(
        term.normalized == "hits"
        for term in _active_inventory_vocabulary(
            removed,
            corpus_catalog,
            "corpus",
            LanguageStage.OHG,
        ).terms
    )


def test_expired_unchanged_digest_applies_provenance_context_without_classifier(
    tmp_path: Path,
) -> None:
    on = date(2026, 8, 12)
    url = "https://www.synthetic.org/expired-provenance"
    path = tmp_path / "vocabulary.yaml"
    _write_empty_vocabulary(path, on=on)
    crawler = FixtureCrawler(
        {url: ("A Middle High German tool and Old High German corpus using HiTS.")}
    )
    classifier = AcceptingClassifier()
    tool_catalog = _shared_source_catalog(
        url,
        tool_stages=(LanguageStage.MHG,),
    )
    _incremental_run(path, tool_catalog, crawler, classifier, on=on)
    classifier_calls = len(classifier.calls)

    added_catalog = _shared_source_catalog(
        url,
        corpus_stages=(LanguageStage.OHG,),
        tool_stages=(LanguageStage.MHG,),
    )
    _incremental_run(
        path,
        added_catalog,
        crawler,
        classifier,
        on=on + timedelta(days=30),
    )
    stored = load_vocabulary(path)
    hits = next(term for term in stored.terms if term.normalized == "hits")

    assert crawler.calls == [(url, False), (url, True)]
    assert len(classifier.calls) == classifier_calls
    assert ("corpus", LanguageStage.OHG) in {
        (context.category, context.stage) for context in hits.contexts
    }
    assert any(
        term.normalized == "hits"
        for term in _active_inventory_vocabulary(
            stored,
            added_catalog,
            "corpus",
            LanguageStage.OHG,
        ).terms
    )
