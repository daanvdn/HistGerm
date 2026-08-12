from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

from histgerm.research.search_providers import (
    ResponseFormat,
    ResultClassification,
    SearchProvider,
    SearchResult,
    assess_search_response,
    build_provider_request,
    parse_bing_rss,
    parse_search_html,
)

NOW = datetime(2026, 8, 12, 13, 24, tzinfo=UTC)


def test_google_bing_and_brave_have_independent_request_identities() -> None:
    requests = [
        build_provider_request(
            provider,
            "Middle High German corpus",
            locale="en-US",
        )
        for provider in SearchProvider
    ]
    assert {request.provider for request in requests} == set(SearchProvider)
    general = [
        request
        for request in requests
        if request.provider
        in {SearchProvider.BING, SearchProvider.BRAVE, SearchProvider.GOOGLE}
    ]
    assert {urlsplit(request.url).hostname for request in general} == {
        "www.bing.com",
        "search.brave.com",
        "www.google.com",
    }
    assert (
        next(
            request for request in requests if request.provider is SearchProvider.BING
        ).response_format
        is ResponseFormat.RSS
    )
    assert all(request.retrieval_mode == "bounded_http" for request in requests)


def test_relevant_bing_rss_results_are_parsed_and_all_inspected() -> None:
    rss = """<?xml version="1.0"?>
    <rss><channel>
      <item><title>TreeTagger</title><link>https://www.cis.uni-muenchen.de/~schmid/tools/TreeTagger/</link>
      <description>Language independent part-of-speech tagger.</description></item>
      <item><title>POS Tagger für mittelhochdeutsche Texte</title>
      <link>https://www.ling.uni-stuttgart.de/institut/ilr/tager/</link>
      <description>Universität Stuttgart.</description></item>
    </channel></rss>"""
    seen: list[str] = []

    def inspect(result: SearchResult) -> tuple[ResultClassification, str]:
        seen.append(result.title)
        return ("lead", "stage applicability requires canonical inspection")

    record = assess_search_response(
        provider=SearchProvider.BING,
        query="Mittelhochdeutsch POS-Tagger",
        retrieval_mode="bounded_http",
        response_format=ResponseFormat.RSS,
        locale="de-DE",
        observed_at=NOW,
        http_status=200,
        body=rss,
        inspector=inspect,
    )
    assert [result.title for result in parse_bing_rss(rss)] == seen
    assert record.assessment == "results"
    assert len(record.inspections) == len(record.results) == 2
    assert all(result.trusted_evidence is False for result in record.results)
    assert "retained untrusted leads" in record.observation


def test_synthetic_html_parses_google_redirects_and_snippets() -> None:
    body = """
    <main>
      <a href="/url?q=https%3A%2F%2Fexample.org%2Ftool&amp;x=1"
         data-snippet="A search snippet, not evidence."><h3>Example Tool</h3></a>
      <a href="javascript:void(0)">Not a result</a>
    </main>
    """
    results = parse_search_html(body)
    assert results[0].url == "https://example.org/tool"
    assert results[0].snippet == "A search snippet, not evidence."
    assert len(results) == 1


def test_google_challenge_is_an_access_gap_without_result_inspection() -> None:
    called = False

    def inspect(result: SearchResult) -> tuple[ResultClassification, str]:
        nonlocal called
        called = True
        return ("lead", "unused")

    record = assess_search_response(
        provider=SearchProvider.GOOGLE,
        query="Middle High German treebank",
        retrieval_mode="bounded_http",
        locale="en-US",
        observed_at=NOW,
        http_status=429,
        body="<title>Our systems have detected unusual traffic</title> CAPTCHA",
        inspector=inspect,
    )
    assert record.assessment == "access_gap"
    assert not record.completed and not called
    assert "HTTP 429 through bounded_http transport for google" in record.observation
    assert "no challenge was bypassed" in record.observation
    assert "URL failed" not in record.observation


def test_non_success_status_infers_response_status_failure_stage() -> None:
    record = assess_search_response(
        provider=SearchProvider.BRAVE,
        query="Middle High German corpus",
        retrieval_mode="bounded_http",
        locale="en-US",
        observed_at=NOW,
        http_status=503,
        body="temporarily unavailable",
        inspector=lambda result: ("unrelated", "unused"),
    )
    assert record.failure_stage == "response_status"
    assert "stage response_status" in record.observation


def test_unrelated_response_requires_inspection_of_every_item() -> None:
    body = """
    <a href="https://example.org/one">Weather</a>
    <a href="https://example.org/two">Shopping</a>
    """
    positions: list[int] = []

    def inspect(result: SearchResult) -> tuple[ResultClassification, str]:
        positions.append(result.position)
        return ("unrelated", "no historical-language or resource context")

    record = assess_search_response(
        provider=SearchProvider.BRAVE,
        query="Old High German dictionary",
        retrieval_mode="bounded_http",
        locale="en-GB",
        observed_at=NOW,
        http_status=200,
        body=body,
        inspector=inspect,
    )
    assert record.assessment == "unrelated"
    assert positions == [1, 2]
    assert "all 2 result items were inspected" in record.observation
