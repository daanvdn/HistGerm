from __future__ import annotations

from datetime import UTC, datetime

from histgerm.catalog import load_catalog
from histgerm.models import LanguageStage
from histgerm.research.discovery_orchestration import (
    DiscoveryConfig,
    DiscoveryDependencies,
    ProviderResponse,
    run_discovery,
)
from histgerm.research.inventory_vocabulary import (
    FetchedDocument,
    VocabularyLimits,
)
from histgerm.research.search_providers import (
    ResponseFormat,
    ResultClassification,
    SearchRequest,
    SearchResult,
)


class EmptyModel:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls = 0

    def __call__(self, prompt: str, /) -> str:
        self.events.append("model")
        self.calls += 1
        return '{"candidates":[]}'


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
        "huggingface",
    }
    assert any('-"' in request.query for request in requests)
    assert all(request.retrieval_mode == "bounded_http" for request in requests)
    assert inspected
    assert result.leads[0].trusted_evidence is False
    assert result.metrics["focused_queries_attempted"] == len(requests)
    assert result.metrics["model_leads"] == 0


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
        "huggingface",
    }
    assert all(record.assessment == "transport_error" for record in result.assessments)
    assert all(record.failure_stage == "request" for record in result.assessments)
    assert all(
        "bounded_http transport" in record.observation for record in result.assessments
    )
