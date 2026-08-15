from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

from histgerm.research.search_providers import (
    ResponseFormat,
    ResultClassification,
    ResultInspection,
    SearchPageResponse,
    SearchProvider,
    SearchRequest,
    SearchResult,
    assess_paginated_search,
    assess_search_response,
    build_provider_request,
    parse_bing_rss,
    parse_search_html,
    replace_result_inspections,
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
    gitlab = next(
        request for request in requests if request.provider is SearchProvider.GITLAB
    )
    assert urlsplit(gitlab.url).hostname == "gitlab.com"
    assert "search=Middle+High+German+corpus" in gitlab.url
    assert "scope=projects" in gitlab.url


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


def test_replacing_inspections_can_remove_all_retained_leads() -> None:
    record = assess_search_response(
        provider=SearchProvider.CLARIN,
        channel="clarin",
        query="Althochdeutsch Korpus",
        retrieval_mode="bounded_http",
        locale="de-DE",
        observed_at=NOW,
        http_status=200,
        body=(
            '<a href="https://example.org/one">One</a>'
            '<a href="https://example.org/two">Two</a>'
        ),
        inspector=lambda result: (
            ("lead", "previously retained")
            if result.position == 1
            else ("unrelated", "inspection pending")
        ),
    )

    replaced = replace_result_inspections(
        record,
        (
            ResultInspection(1, "unrelated", "not an OHG corpus"),
            ResultInspection(2, "unrelated", "not an OHG corpus"),
        ),
    )

    assert replaced.assessment == "unrelated"
    assert "all 2 result items were inspected as unrelated" in replaced.observation
    assert [inspection.position for inspection in replaced.inspections] == [1, 2]


def test_replacing_inspections_preserves_remaining_leads() -> None:
    record = assess_search_response(
        provider=SearchProvider.CLARIN,
        channel="clarin",
        query="Althochdeutsch Korpus",
        retrieval_mode="bounded_http",
        locale="de-DE",
        observed_at=NOW,
        http_status=200,
        body=(
            '<a href="https://example.org/one">One</a>'
            '<a href="https://example.org/two">Two</a>'
        ),
        inspector=lambda result: ("unrelated", "inspection pending"),
    )

    replaced = replace_result_inspections(
        record,
        (
            ResultInspection(1, "lead", "candidate OHG corpus"),
            ResultInspection(2, "unrelated", "not an OHG corpus"),
        ),
    )

    assert replaced.assessment == "results"
    assert "retained untrusted leads" in replaced.observation
    assert replaced.observation.startswith("HTTP 200 through bounded_http transport")
    assert [inspection.reason for inspection in replaced.inspections] == [
        "candidate OHG corpus",
        "not an OHG corpus",
    ]


def test_replacing_empty_inspections_preserves_transport_observation() -> None:
    record = assess_search_response(
        provider=SearchProvider.BRAVE,
        query="Old High German corpus",
        retrieval_mode="bounded_http",
        locale="en-US",
        observed_at=NOW,
        http_status=503,
        body="temporarily unavailable",
        inspector=lambda result: ("unrelated", "unused"),
    )

    assert replace_result_inspections(record, ()) == record


def test_paginated_search_inspects_two_pages_until_explicit_exhaustion() -> None:
    request = build_provider_request(
        SearchProvider.GOOGLE,
        "Middle High German corpus",
        locale="en-US",
    )
    requested_urls: list[str] = []
    pages = [
        SearchPageResponse(
            retrieval_mode="bounded_http",
            observed_at=NOW,
            http_status=200,
            body='<a href="https://example.org/one">One</a>',
            next_cursor="10",
        ),
        SearchPageResponse(
            retrieval_mode="bounded_http",
            observed_at=NOW,
            http_status=200,
            body='<a href="https://example.org/two">Two</a>',
            exhausted=True,
        ),
    ]

    def fetch(page_request: SearchRequest) -> SearchPageResponse:
        requested_urls.append(page_request.url)
        return pages[len(requested_urls) - 1]

    record = assess_paginated_search(
        request,
        fetch_page=fetch,
        inspector=lambda result: ("lead", f"inspected {result.title}"),
    )

    assert record.completed
    assert record.stop_reason == "provider_exhausted"
    assert len(record.attempts) == 2
    assert [result.title for result in record.results] == ["One", "Two"]
    assert "start=10" in requested_urls[1]


def test_paginated_search_stops_on_repeated_cursor() -> None:
    request = build_provider_request(
        SearchProvider.BING,
        "Althochdeutsch Wörterbuch",
        locale="de-DE",
    )
    calls = 0

    def fetch(_: SearchRequest) -> SearchPageResponse:
        nonlocal calls
        calls += 1
        return SearchPageResponse(
            retrieval_mode="bounded_http",
            observed_at=NOW,
            http_status=200,
            body=(
                "<rss><channel><item><title>Result</title>"
                f"<link>https://example.org/{calls}</link></item></channel></rss>"
            ),
            next_cursor="11",
        )

    record = assess_paginated_search(
        request,
        fetch_page=fetch,
        inspector=lambda result: ("lead", result.url),
    )

    assert not record.completed
    assert record.stop_reason == "repeated_cursor"
    assert len(record.attempts) == calls == 2


def test_paginated_search_reports_max_page_truncation() -> None:
    request = build_provider_request(
        SearchProvider.ZENODO,
        "Middle High German dataset",
        locale="en-US",
    )
    record = assess_paginated_search(
        request,
        max_pages=1,
        fetch_page=lambda _: SearchPageResponse(
            retrieval_mode="bounded_http",
            observed_at=NOW,
            http_status=200,
            body='<a href="https://example.org/one">One</a>',
            next_cursor="2",
        ),
        inspector=lambda result: ("lead", result.url),
    )

    assert record.state == "access_gap"
    assert record.stop_reason == "max_pages"
    assert "strict 1-page limit" in record.observation


def test_paginated_search_reports_unsupported_provider_without_fetching() -> None:
    request = build_provider_request(
        SearchProvider.OLAC,
        "Old High German corpus",
        locale="en-US",
    )
    called = False

    def fetch(_: SearchRequest) -> SearchPageResponse:
        nonlocal called
        called = True
        raise AssertionError("unsupported pagination must not issue a request")

    record = assess_paginated_search(
        request,
        fetch_page=fetch,
        inspector=lambda result: ("lead", result.url),
    )

    assert record.state == "access_gap"
    assert record.stop_reason == "unsupported_pagination"
    assert not record.attempts and not called


def test_paginated_search_inspects_duplicates_before_stable_url_dedupe() -> None:
    request = build_provider_request(
        SearchProvider.GITHUB,
        "Middle High German parser",
        locale="en-US",
    )
    inspected: list[str] = []
    pages = iter(
        (
            SearchPageResponse(
                retrieval_mode="bounded_http",
                observed_at=NOW,
                http_status=200,
                body=(
                    '<a href="https://EXAMPLE.org/tool#first">Tool first</a>'
                    '<a href="https://example.org/other">Other</a>'
                ),
                next_cursor="2",
            ),
            SearchPageResponse(
                retrieval_mode="bounded_http",
                observed_at=NOW,
                http_status=200,
                body='<a href="https://example.org/tool#second">Tool duplicate</a>',
                exhausted=True,
            ),
        )
    )

    def inspect(result: SearchResult) -> tuple[ResultClassification, str]:
        inspected.append(result.title)
        return ("lead", "synthetic inspection")

    record = assess_paginated_search(
        request,
        fetch_page=lambda _: next(pages),
        inspector=inspect,
    )

    assert inspected == ["Tool first", "Other", "Tool duplicate"]
    assert [result.title for result in record.results] == ["Tool first", "Other"]
    assert [result.position for result in record.results] == [1, 2]


def test_gitlab_pagination_uses_only_explicit_numeric_page_cursor() -> None:
    request = build_provider_request(
        SearchProvider.GITLAB,
        "Middle High German parser",
        locale="en-US",
    )
    pages = iter(
        (
            SearchPageResponse(
                retrieval_mode="bounded_http",
                observed_at=NOW,
                http_status=200,
                body='<a href="https://gitlab.com/history-lab/parser">Parser</a>',
                next_cursor="2",
            ),
            SearchPageResponse(
                retrieval_mode="bounded_http",
                observed_at=NOW,
                http_status=200,
                body='<a href="https://gitlab.com/history-lab/tagger">Tagger</a>',
                exhausted=True,
            ),
        )
    )
    requested_urls: list[str] = []

    def fetch(page_request: SearchRequest) -> SearchPageResponse:
        requested_urls.append(page_request.url)
        return next(pages)

    record = assess_paginated_search(
        request,
        fetch_page=fetch,
        inspector=lambda result: ("lead", result.url),
    )

    assert record.completed
    assert record.provider is SearchProvider.GITLAB
    assert record.stop_reason == "provider_exhausted"
    assert "page=2" in requested_urls[1]


def test_first_page_empty_is_an_explicit_incomplete_gap() -> None:
    request = build_provider_request(
        SearchProvider.BRAVE,
        "Early New High German dictionary",
        locale="en-US",
    )
    record = assess_paginated_search(
        request,
        fetch_page=lambda _: SearchPageResponse(
            retrieval_mode="bounded_http",
            observed_at=NOW,
            http_status=200,
            body="<main>No results</main>",
            exhausted=True,
        ),
        inspector=lambda result: ("unrelated", result.url),
    )

    assert record.state == "access_gap"
    assert record.stop_reason == "first_page_inconclusive"
