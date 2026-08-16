"""Provider-specific parsing and auditable search-result assessment."""

from __future__ import annotations

import html
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree

from .fetching import RetrievalFailureStage, RetrievalMode

MAX_PROVIDER_PAGES = 5
MAX_PROVIDER_RESULTS = 100


class SearchProvider(StrEnum):
    """Independent search and registry provider identities."""

    BING = "bing"
    BRAVE = "brave"
    CLARIN = "clarin"
    GITHUB = "github"
    GITLAB = "gitlab"
    GOOGLE = "google"
    HUGGINGFACE = "huggingface"
    LAUDATIO = "laudatio"
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
type PaginationState = Literal["complete", "access_gap"]
type PaginationStopReason = Literal[
    "provider_exhausted",
    "unsupported_pagination",
    "repeated_page",
    "repeated_cursor",
    "max_pages",
    "max_results",
    "access_gap",
    "transport_error",
    "next_page_unavailable",
    "unsafe_pagination_response",
    "first_page_inconclusive",
]


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
    method: Literal["GET", "POST"] = "GET"
    body: bytes | None = None
    headers: tuple[tuple[str, str], ...] = ()


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
    page_number: int = 1
    pagination_state: PaginationState | None = None
    pagination_stop_reason: PaginationStopReason | None = None

    @property
    def completed(self) -> bool:
        """Report whether this interface was inspectable to completion."""

        return (
            self.assessment in {"results", "empty", "unrelated"}
            and self.pagination_state != "access_gap"
        )


type ResultInspector = Callable[[SearchResult], tuple[ResultClassification, str]]


@dataclass(frozen=True, slots=True)
class SearchPageResponse:
    """One provider response plus explicit, transport-derived page state."""

    retrieval_mode: RetrievalMode
    observed_at: datetime
    http_status: int | None
    body: str
    failure_stage: RetrievalFailureStage | None = None
    next_cursor: str | None = None
    exhausted: bool = False


@dataclass(frozen=True, slots=True)
class PaginatedSearchAssessment:
    """Aggregate bounded page attempts without losing per-request evidence."""

    provider: SearchProvider
    channel: str
    query: str
    locale: str
    retrieval_mode: RetrievalMode
    state: PaginationState
    stop_reason: PaginationStopReason
    observation: str
    attempts: tuple[SearchAssessmentRecord, ...]
    results: tuple[SearchResult, ...]

    @property
    def completed(self) -> bool:
        """Report only explicit provider exhaustion as complete."""

        return self.state == "complete"


type SearchPageFetcher = Callable[[SearchRequest], SearchPageResponse]

_PAGINATION_PARAMETERS: dict[SearchProvider, str] = {
    SearchProvider.BING: "first",
    SearchProvider.BRAVE: "offset",
    SearchProvider.GITHUB: "p",
    SearchProvider.GITLAB: "page",
    SearchProvider.GOOGLE: "start",
    SearchProvider.ZENODO: "page",
    SearchProvider.LAUDATIO: "from",
}


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
    elif provider is SearchProvider.GITLAB:
        base_url = "https://gitlab.com/search"
        parameters = {"search": query, "scope": "projects"}
    elif provider is SearchProvider.HUGGINGFACE:
        base_url = "https://huggingface.co/search/full-text"
        parameters = {"q": query}
    else:
        payload = json.dumps(
            {"searchData": {"from": 0, "size": 20, "query": query}},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        return SearchRequest(
            provider=provider,
            channel=channel,
            query=query,
            retrieval_mode=retrieval_mode,
            response_format=ResponseFormat.API,
            locale=locale,
            url="https://www.laudatio-repository.org/api/elasticapi/v1/corpora/latest/searchMain",
            method="POST",
            body=payload,
            headers=(
                ("Accept", "application/json"),
                ("Content-Type", "application/json"),
                ("Api-Version", "v1"),
            ),
        )
    return SearchRequest(
        provider=provider,
        channel=channel,
        query=query,
        retrieval_mode=retrieval_mode,
        response_format=interface,
        locale=locale,
        url=f"{base_url}?{urlencode(parameters)}",
    )


def assess_paginated_search(
    request: SearchRequest,
    *,
    fetch_page: SearchPageFetcher,
    inspector: ResultInspector,
    max_pages: int = MAX_PROVIDER_PAGES,
    max_results: int = MAX_PROVIDER_RESULTS,
) -> PaginatedSearchAssessment:
    """Fetch documented provider pages within strict page and result ceilings.

    ``fetch_page`` must derive ``next_cursor`` and ``exhausted`` from the
    provider response. An absent cursor is never guessed or treated as
    exhaustion.
    """

    _validate_pagination_limits(max_pages=max_pages, max_results=max_results)
    if request.provider not in _PAGINATION_PARAMETERS:
        return _pagination_record(
            request,
            state="access_gap",
            stop_reason="unsupported_pagination",
            detail=(
                f"{request.provider.value} pagination is not supported by the "
                "documented bounded interface"
            ),
        )

    attempts: list[SearchAssessmentRecord] = []
    unique_results: list[SearchResult] = []
    seen_items: set[str] = set()
    seen_cursors: set[str] = set()
    seen_pages: set[tuple[str, ...]] = set()
    page_request = request

    while True:
        response = fetch_page(page_request)
        record = assess_search_response(
            provider=page_request.provider,
            channel=page_request.channel,
            query=page_request.query,
            retrieval_mode=response.retrieval_mode,
            response_format=page_request.response_format,
            locale=page_request.locale,
            observed_at=response.observed_at,
            http_status=response.http_status,
            failure_stage=response.failure_stage,
            body=response.body,
            inspector=inspector,
        )
        attempts.append(record)
        page_key = tuple(_stable_result_key(result) for result in record.results)
        if record.completed and page_key in seen_pages:
            return _pagination_record(
                request,
                state="access_gap",
                stop_reason="repeated_page",
                detail="provider repeated an already inspected result page",
                attempts=attempts,
                results=unique_results,
            )
        if record.completed:
            seen_pages.add(page_key)
        for result in record.results:
            key = _stable_result_key(result)
            if key in seen_items:
                continue
            seen_items.add(key)
            if len(unique_results) < max_results:
                unique_results.append(
                    SearchResult(
                        position=len(unique_results) + 1,
                        url=result.url,
                        title=result.title,
                        snippet=result.snippet,
                    )
                )

        if len(seen_items) > max_results:
            return _pagination_record(
                request,
                state="access_gap",
                stop_reason="max_results",
                detail=f"strict {max_results}-result limit reached",
                attempts=attempts,
                results=unique_results,
            )
        if record.assessment == "access_gap":
            return _pagination_record(
                request,
                state="access_gap",
                stop_reason="access_gap",
                detail="provider challenge, refusal, or rate limit stopped pagination",
                attempts=attempts,
                results=unique_results,
            )
        if record.assessment == "transport_error":
            return _pagination_record(
                request,
                state="access_gap",
                stop_reason="transport_error",
                detail="unsafe or unavailable response stopped pagination",
                attempts=attempts,
                results=unique_results,
            )
        if response.exhausted and response.next_cursor is not None:
            return _pagination_record(
                request,
                state="access_gap",
                stop_reason="unsafe_pagination_response",
                detail="response claimed exhaustion and also supplied a next cursor",
                attempts=attempts,
                results=unique_results,
            )
        if response.exhausted:
            if len(attempts) == 1 and record.assessment in {"empty", "unrelated"}:
                return _pagination_record(
                    request,
                    state="access_gap",
                    stop_reason="first_page_inconclusive",
                    detail=(
                        f"first page was {record.assessment}; this is not sufficient "
                        "evidence of complete provider coverage"
                    ),
                    attempts=attempts,
                    results=unique_results,
                )
            return _pagination_record(
                request,
                state="complete",
                stop_reason="provider_exhausted",
                detail=f"provider exhaustion observed after {len(attempts)} pages",
                attempts=attempts,
                results=unique_results,
            )
        cursor = (response.next_cursor or "").strip()
        if not cursor:
            return _pagination_record(
                request,
                state="access_gap",
                stop_reason="next_page_unavailable",
                detail="provider did not expose a safely retrievable next page",
                attempts=attempts,
                results=unique_results,
            )
        normalized_cursor = (
            str(int(cursor)) if cursor.isascii() and cursor.isdecimal() else cursor
        )
        if normalized_cursor in seen_cursors:
            return _pagination_record(
                request,
                state="access_gap",
                stop_reason="repeated_cursor",
                detail=f"provider repeated pagination cursor {cursor!r}",
                attempts=attempts,
                results=unique_results,
            )
        seen_cursors.add(normalized_cursor)
        if len(attempts) >= max_pages:
            return _pagination_record(
                request,
                state="access_gap",
                stop_reason="max_pages",
                detail=f"strict {max_pages}-page limit reached before exhaustion",
                attempts=attempts,
                results=unique_results,
            )
        try:
            page_request = build_provider_page_request(request, cursor)
        except ValueError as error:
            return _pagination_record(
                request,
                state="access_gap",
                stop_reason="unsafe_pagination_response",
                detail=f"next page could not be safely constructed: {error}",
                attempts=attempts,
                results=unique_results,
            )


def supports_pagination(provider: SearchProvider) -> bool:
    """Return whether bounded pagination is implemented for a provider."""

    return provider in _PAGINATION_PARAMETERS


def build_provider_page_request(
    request: SearchRequest,
    cursor: str,
) -> SearchRequest:
    """Build one same-interface page request from a documented numeric cursor."""

    parameter = _PAGINATION_PARAMETERS.get(request.provider)
    if parameter is None:
        raise ValueError(f"{request.provider.value} pagination is unsupported")
    minimum = 0 if request.provider is SearchProvider.LAUDATIO else 1
    if not cursor.isascii() or not cursor.isdecimal() or int(cursor) < minimum:
        raise ValueError("pagination cursor must be a positive ASCII integer")
    if request.provider is SearchProvider.LAUDATIO:
        if request.method != "POST" or request.body is None:
            raise ValueError("LAUDATIO pagination requires its JSON POST request")
        try:
            payload = json.loads(request.body)
            search_data = payload["searchData"]
            if (
                not isinstance(search_data, dict)
                or set(search_data) != {"from", "size", "query"}
                or search_data["size"] != 20
                or search_data["query"] != request.query
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            raise ValueError(
                "LAUDATIO request body is not the fixed search contract"
            ) from error
        search_data["from"] = int(cursor)
        return replace(
            request,
            body=json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode(),
        )
    parsed = urlsplit(request.url)
    parameters = parse_qs(parsed.query, keep_blank_values=True)
    parameters[parameter] = [cursor]
    query = urlencode(parameters, doseq=True)
    return SearchRequest(
        provider=request.provider,
        channel=request.channel,
        query=request.query,
        retrieval_mode=request.retrieval_mode,
        response_format=request.response_format,
        locale=request.locale,
        url=urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, "")),
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
        access_stage: RetrievalFailureStage = failure_stage or (
            "rate_limit" if http_status == 429 else "challenge"
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
            failure_stage=access_stage,
            assessment="access_gap",
            observation=transport_observation(
                provider=provider,
                retrieval_mode=retrieval_mode,
                observed_at=observed_at,
                http_status=http_status,
                failure_stage=access_stage,
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
            else parse_laudatio_api(body)
            if response_format is ResponseFormat.API
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
    assessment, detail = _inspection_outcome(results, inspections)
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


def replace_result_inspections(
    record: SearchAssessmentRecord,
    inspections: tuple[ResultInspection, ...],
) -> SearchAssessmentRecord:
    """Replace verdicts and refresh the assessment fields derived from them."""

    if len(record.results) != len(inspections):
        raise ValueError("inspection count must match the result count")
    if not record.results:
        return record
    previous_detail = _inspection_outcome(record.results, record.inspections)[1]
    if previous_detail not in record.observation:
        raise ValueError("record observation does not describe its inspections")
    assessment, detail = _inspection_outcome(record.results, inspections)
    return replace(
        record,
        assessment=assessment,
        observation=record.observation.replace(previous_detail, detail, 1),
        inspections=inspections,
    )


def _inspection_outcome(
    results: tuple[SearchResult, ...],
    inspections: tuple[ResultInspection, ...],
) -> tuple[Assessment, str]:
    if not results:
        return "empty", "provider returned no parseable result items"
    if all(item.classification == "unrelated" for item in inspections):
        return (
            "unrelated",
            f"all {len(results)} result items were inspected as unrelated",
        )
    return (
        "results",
        f"inspected all {len(results)} result items; retained untrusted leads",
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


def parse_laudatio_api(document: str) -> tuple[SearchResult, ...]:
    """Parse the verified LAUDATIO corpus-search envelope as untrusted leads."""

    try:
        envelope = json.loads(document)
    except json.JSONDecodeError as error:
        raise ValueError("invalid LAUDATIO JSON response") from error
    if not isinstance(envelope, dict) or envelope.get("success") is not True:
        raise ValueError("invalid LAUDATIO response envelope")
    data = envelope.get("data")
    if not isinstance(data, list):
        raise ValueError("invalid LAUDATIO result container")
    results: list[SearchResult] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("invalid LAUDATIO result item")
        identifier = item.get("_id")
        index = item.get("_index")
        source = item.get("_source")
        if (
            not isinstance(identifier, str)
            or not identifier.strip()
            or "/" in identifier
            or not isinstance(index, str)
            or index != "corpora"
            or not isinstance(source, dict)
        ):
            raise ValueError("invalid LAUDATIO corpus identifier")
        title = _first_text(source.get("corpus_title"))
        if not title:
            raise ValueError("LAUDATIO corpus has no usable title")
        metadata = [
            _first_text(source.get(name))
            for name in (
                "description",
                "historical_language",
                "authors",
                "editors",
                "genre",
                "publication_year",
                "version",
            )
        ]
        snippet = _bounded_text(
            "; ".join(dict.fromkeys(value for value in metadata if value))
        )
        results.append(
            SearchResult(
                position=len(results) + 1,
                url=f"https://www.laudatio-repository.org/browse/corpus/{identifier}/{index}",
                title=_bounded_text(title, 300),
                snippet=snippet or None,
            )
        )
    return tuple(results)


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
    if http_status == 429:
        return (
            "provider rate limit encountered; "
            "no retry or challenge bypass was attempted"
        )
    if provider is SearchProvider.GOOGLE and http_status in {403, 429}:
        return "Google access gap encountered; no challenge was bypassed"
    return None


def _validate_pagination_limits(*, max_pages: int, max_results: int) -> None:
    if not 1 <= max_pages <= MAX_PROVIDER_PAGES:
        raise ValueError(f"max_pages must be between 1 and {MAX_PROVIDER_PAGES}")
    if not 1 <= max_results <= MAX_PROVIDER_RESULTS:
        raise ValueError(f"max_results must be between 1 and {MAX_PROVIDER_RESULTS}")


def _stable_result_key(result: SearchResult) -> str:
    parsed = urlsplit(result.url)
    host = (parsed.hostname or "").casefold()
    port = parsed.port
    default_port = (parsed.scheme == "https" and port == 443) or (
        parsed.scheme == "http" and port == 80
    )
    authority = host if port is None or default_port else f"{host}:{port}"
    path = parsed.path or "/"
    return urlunsplit(
        (parsed.scheme.casefold(), authority, path, parsed.query, "")
    ).casefold()


def _pagination_record(
    request: SearchRequest,
    *,
    state: PaginationState,
    stop_reason: PaginationStopReason,
    detail: str,
    attempts: list[SearchAssessmentRecord] | None = None,
    results: list[SearchResult] | None = None,
) -> PaginatedSearchAssessment:
    attempt_records = tuple(
        replace(
            attempt,
            page_number=index,
            pagination_state=state,
            pagination_stop_reason=stop_reason,
        )
        for index, attempt in enumerate(attempts or (), start=1)
    )
    statuses = ", ".join(
        "none" if attempt.http_status is None else str(attempt.http_status)
        for attempt in attempt_records
    )
    status_detail = statuses or "no attempts"
    return PaginatedSearchAssessment(
        provider=request.provider,
        channel=request.channel,
        query=request.query,
        locale=request.locale,
        retrieval_mode=request.retrieval_mode,
        state=state,
        stop_reason=stop_reason,
        observation=(
            f"{detail}; {len(attempt_records)} attempt(s), HTTP statuses "
            f"[{status_detail}]"
        ),
        attempts=attempt_records,
        results=tuple(results or ()),
    )


def _element_text(element: ElementTree.Element | None) -> str:
    if element is None:
        return ""
    return _clean_text("".join(element.itertext()))


def _clean_text(value: str | None) -> str:
    if value is None:
        return ""
    return " ".join(html.unescape(value).split())


def _first_text(value: object) -> str:
    values = value if isinstance(value, list) else [value]
    for item in values:
        if isinstance(item, str) and (cleaned := _clean_text(item)):
            return cleaned
    return ""


def _bounded_text(value: str, limit: int = 600) -> str:
    return value[:limit].rstrip()


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
    "MAX_PROVIDER_PAGES",
    "MAX_PROVIDER_RESULTS",
    "PaginatedSearchAssessment",
    "PaginationState",
    "PaginationStopReason",
    "ResultClassification",
    "ResultInspection",
    "ResponseFormat",
    "RetrievalMode",
    "SearchAssessmentRecord",
    "SearchPageFetcher",
    "SearchPageResponse",
    "SearchProvider",
    "SearchRequest",
    "SearchResult",
    "assess_paginated_search",
    "assess_search_response",
    "build_provider_page_request",
    "build_provider_request",
    "parse_bing_rss",
    "parse_laudatio_api",
    "parse_search_html",
    "supports_pagination",
    "transport_observation",
]
