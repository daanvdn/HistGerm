"""Provider-specific parsing and auditable search-result assessment."""

from __future__ import annotations

import html
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import parse_qs, urlencode, urlsplit
from xml.etree import ElementTree

from .fetching import RetrievalFailureStage, RetrievalMode


class SearchProvider(StrEnum):
    """Independent search and registry provider identities."""

    BING = "bing"
    BRAVE = "brave"
    CLARIN = "clarin"
    GITHUB = "github"
    GOOGLE = "google"
    HUGGINGFACE = "huggingface"
    OLAC = "olac"
    ZENODO = "zenodo"


class ResponseFormat(StrEnum):
    """Provider response/interface format, distinct from retrieval transport."""

    HTML = "html"
    RSS = "rss"
    API = "api"


type Assessment = Literal[
    "results", "empty", "unrelated", "access_gap", "transport_error"
]
type ResultClassification = Literal["lead", "unrelated"]


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One untrusted search lead parsed from a provider response."""

    position: int
    url: str
    title: str
    snippet: str | None = None
    trusted_evidence: Literal[False] = False


@dataclass(frozen=True, slots=True)
class SearchRequest:
    """An exact provider query and interface request."""

    provider: SearchProvider
    channel: str
    query: str
    retrieval_mode: RetrievalMode
    response_format: ResponseFormat
    locale: str
    url: str


@dataclass(frozen=True, slots=True)
class ResultInspection:
    """Record item-level inspection before response-level classification."""

    position: int
    classification: ResultClassification
    reason: str


@dataclass(frozen=True, slots=True)
class SearchAssessmentRecord:
    """Preserve exact provider, query, transport context, and assessment."""

    provider: SearchProvider
    channel: str
    query: str
    retrieval_mode: RetrievalMode
    response_format: ResponseFormat
    locale: str
    observed_at: datetime
    http_status: int | None
    failure_stage: RetrievalFailureStage | None
    assessment: Assessment
    observation: str
    results: tuple[SearchResult, ...]
    inspections: tuple[ResultInspection, ...]

    @property
    def completed(self) -> bool:
        """Report whether this interface was inspectable to completion."""

        return self.assessment in {"results", "empty", "unrelated"}


type ResultInspector = Callable[[SearchResult], tuple[ResultClassification, str]]


def build_provider_request(
    provider: SearchProvider,
    query: str,
    *,
    channel: str = "general_web",
    locale: str,
    retrieval_mode: RetrievalMode = "bounded_http",
    response_format: ResponseFormat | None = None,
) -> SearchRequest:
    """Build an independent provider request without treating interfaces as equal."""

    if not query.strip() or not locale.strip():
        raise ValueError("query and locale must not be empty")
    interface = response_format
    if interface is None:
        interface = (
            ResponseFormat.RSS
            if provider is SearchProvider.BING
            else ResponseFormat.HTML
        )
    if provider is SearchProvider.BING:
        base_url = "https://www.bing.com/search"
        parameters = {"q": query, "setlang": locale}
        if interface is ResponseFormat.RSS:
            parameters["format"] = "rss"
    elif provider is SearchProvider.BRAVE:
        base_url = "https://search.brave.com/search"
        parameters = {"q": query, "source": "web", "spellcheck": "0"}
    elif provider is SearchProvider.GOOGLE:
        base_url = "https://www.google.com/search"
        parameters = {"q": query, "hl": locale}
    elif provider is SearchProvider.CLARIN:
        base_url = "https://vlo.clarin.eu/search"
        parameters = {"query": query}
    elif provider is SearchProvider.OLAC:
        base_url = "https://www.language-archives.org/tools/search.php4"
        parameters = {"q": query}
    elif provider is SearchProvider.ZENODO:
        base_url = "https://zenodo.org/search"
        parameters = {"q": query}
    elif provider is SearchProvider.GITHUB:
        base_url = "https://github.com/search"
        parameters = {"q": query, "type": "repositories"}
    else:
        base_url = "https://huggingface.co/search/full-text"
        parameters = {"q": query}
    return SearchRequest(
        provider=provider,
        channel=channel,
        query=query,
        retrieval_mode=retrieval_mode,
        response_format=interface,
        locale=locale,
        url=f"{base_url}?{urlencode(parameters)}",
    )


def assess_search_response(
    *,
    provider: SearchProvider,
    channel: str = "general_web",
    query: str,
    retrieval_mode: RetrievalMode,
    response_format: ResponseFormat = ResponseFormat.HTML,
    locale: str,
    observed_at: datetime,
    http_status: int | None,
    failure_stage: RetrievalFailureStage | None = None,
    body: str,
    inspector: ResultInspector,
) -> SearchAssessmentRecord:
    """Parse and inspect every item before assigning a response assessment."""

    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    challenge = _access_gap(provider, http_status, body)
    if challenge is not None:
        return SearchAssessmentRecord(
            provider=provider,
            channel=channel,
            query=query,
            retrieval_mode=retrieval_mode,
            response_format=response_format,
            locale=locale,
            observed_at=observed_at,
            http_status=http_status,
            failure_stage=failure_stage or "challenge",
            assessment="access_gap",
            observation=transport_observation(
                provider=provider,
                retrieval_mode=retrieval_mode,
                observed_at=observed_at,
                http_status=http_status,
                failure_stage=failure_stage or "challenge",
                detail=challenge,
            ),
            results=(),
            inspections=(),
        )
    if http_status is None or not 200 <= http_status < 300:
        transport_stage: RetrievalFailureStage = failure_stage or (
            "response_status" if http_status is not None else "request"
        )
        return SearchAssessmentRecord(
            provider=provider,
            channel=channel,
            query=query,
            retrieval_mode=retrieval_mode,
            response_format=response_format,
            locale=locale,
            observed_at=observed_at,
            http_status=http_status,
            failure_stage=transport_stage,
            assessment="transport_error",
            observation=transport_observation(
                provider=provider,
                retrieval_mode=retrieval_mode,
                observed_at=observed_at,
                http_status=http_status,
                failure_stage=transport_stage,
                detail="provider response was not available for item inspection",
            ),
            results=(),
            inspections=(),
        )
    try:
        results = (
            parse_bing_rss(body)
            if response_format is ResponseFormat.RSS
            else parse_search_html(body)
        )
    except ValueError as error:
        parse_stage: RetrievalFailureStage = failure_stage or "response_body"
        return SearchAssessmentRecord(
            provider=provider,
            channel=channel,
            query=query,
            retrieval_mode=retrieval_mode,
            response_format=response_format,
            locale=locale,
            observed_at=observed_at,
            http_status=http_status,
            failure_stage=parse_stage,
            assessment="transport_error",
            observation=transport_observation(
                provider=provider,
                retrieval_mode=retrieval_mode,
                observed_at=observed_at,
                http_status=http_status,
                failure_stage=parse_stage,
                detail=f"provider response could not be parsed: {error}",
            ),
            results=(),
            inspections=(),
        )
    inspections = tuple(
        ResultInspection(result.position, *inspector(result)) for result in results
    )
    if not results:
        assessment: Assessment = "empty"
        detail = "provider returned no parseable result items"
    elif all(item.classification == "unrelated" for item in inspections):
        assessment = "unrelated"
        detail = f"all {len(results)} result items were inspected as unrelated"
    else:
        assessment = "results"
        detail = f"inspected all {len(results)} result items; retained untrusted leads"
    return SearchAssessmentRecord(
        provider=provider,
        channel=channel,
        query=query,
        retrieval_mode=retrieval_mode,
        response_format=response_format,
        locale=locale,
        observed_at=observed_at,
        http_status=http_status,
        failure_stage=failure_stage,
        assessment=assessment,
        observation=transport_observation(
            provider=provider,
            retrieval_mode=retrieval_mode,
            observed_at=observed_at,
            http_status=http_status,
            failure_stage=failure_stage,
            detail=detail,
        ),
        results=results,
        inspections=inspections,
    )


def parse_bing_rss(document: str) -> tuple[SearchResult, ...]:
    """Parse Bing RSS result items as untrusted leads."""

    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as error:
        raise ValueError("invalid Bing RSS response") from error
    results: list[SearchResult] = []
    for item in root.findall(".//item"):
        title = _element_text(item.find("title"))
        url = _element_text(item.find("link"))
        description = _element_text(item.find("description"))
        if title and _is_public_result_url(url):
            results.append(
                SearchResult(
                    position=len(results) + 1,
                    url=url,
                    title=title,
                    snippet=description or None,
                )
            )
    return tuple(results)


class _ResultHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[SearchResult] = []
        self._href: str | None = None
        self._snippet: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a" or self._href is not None:
            return
        attributes = dict(attrs)
        href = attributes.get("href")
        if href is not None:
            self._href = _unwrap_google_url(href)
            self._snippet = attributes.get("data-snippet")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._href is None:
            return
        title = _clean_text(" ".join(self._text))
        if title and _is_public_result_url(self._href):
            self.results.append(
                SearchResult(
                    position=len(self.results) + 1,
                    url=self._href,
                    title=title,
                    snippet=_clean_text(self._snippet) if self._snippet else None,
                )
            )
        self._href = None
        self._snippet = None
        self._text = []


def parse_search_html(document: str) -> tuple[SearchResult, ...]:
    """Parse synthetic or provider HTML anchors as untrusted result leads."""

    parser = _ResultHTMLParser()
    parser.feed(document)
    parser.close()
    return tuple(parser.results)


def transport_observation(
    *,
    provider: SearchProvider,
    retrieval_mode: RetrievalMode,
    observed_at: datetime,
    http_status: int | None,
    failure_stage: RetrievalFailureStage | None,
    detail: str,
) -> str:
    """Describe only what the bounded request context observed."""

    timestamp = observed_at.isoformat()
    status = f"HTTP {http_status}" if http_status is not None else "no HTTP response"
    stage = f", stage {failure_stage}" if failure_stage is not None else ""
    return (
        f"{status} through {retrieval_mode} transport for "
        f"{provider.value} at {timestamp}{stage}; {detail}"
    )


def _access_gap(
    provider: SearchProvider, http_status: int | None, body: str
) -> str | None:
    folded = body.casefold()
    markers = (
        "captcha",
        "unusual traffic",
        "automated queries",
        "before you continue to google",
        "consent.google",
        "verify you are human",
        "access denied",
    )
    marker = next((value for value in markers if value in folded), None)
    if marker is not None:
        return f"access gap encountered ({marker}); no challenge was bypassed"
    if provider is SearchProvider.GOOGLE and http_status in {403, 429}:
        return "Google access gap encountered; no challenge was bypassed"
    return None


def _element_text(element: ElementTree.Element | None) -> str:
    if element is None:
        return ""
    return _clean_text("".join(element.itertext()))


def _clean_text(value: str | None) -> str:
    if value is None:
        return ""
    return " ".join(html.unescape(value).split())


def _unwrap_google_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.path == "/url":
        values = parse_qs(parsed.query)
        return values.get("q", values.get("url", [url]))[0]
    return url


def _is_public_result_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


__all__ = [
    "Assessment",
    "ResultClassification",
    "ResultInspection",
    "ResponseFormat",
    "RetrievalMode",
    "SearchAssessmentRecord",
    "SearchProvider",
    "SearchRequest",
    "SearchResult",
    "assess_search_response",
    "build_provider_request",
    "parse_bing_rss",
    "parse_search_html",
    "transport_observation",
]
