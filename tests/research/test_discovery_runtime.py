from __future__ import annotations

import socket
from datetime import UTC, datetime
from typing import Any

import pytest

from histgerm.research.discovery_runtime import (
    MAX_PROVIDER_BYTES,
    MAX_VOCABULARY_BYTES,
    load_runtime_capabilities,
)
from histgerm.research.fetching import (
    FetchedMetadata,
    MetadataFetchError,
    fetch_public_metadata,
)
from histgerm.research.models import RequestDestination
from histgerm.research.search_providers import (
    ResponseFormat,
    SearchProvider,
    SearchRequest,
)


def request() -> SearchRequest:
    return SearchRequest(
        provider=SearchProvider.GOOGLE,
        channel="general_web_google",
        query="Mittelhochdeutsch Tagger",
        retrieval_mode="bounded_http",
        response_format=ResponseFormat.HTML,
        locale="de-DE",
        url="https://www.google.com/search?q=test",
    )


def clock() -> datetime:
    return datetime(2026, 8, 12, tzinfo=UTC)


def test_provider_adapter_returns_bounded_response_and_no_guessed_pagination() -> None:
    seen: list[tuple[str, int]] = []

    def fetch(url: str, /, *, max_bytes: int) -> FetchedMetadata:
        seen.append((url, max_bytes))
        return FetchedMetadata(url, "text/html", b"<a href='https://e.org/x'>X</a>")

    capabilities = load_runtime_capabilities(fetch=fetch, clock=clock)
    response = capabilities.provider_fetch(request())
    assert seen == [("https://www.google.com/search?q=test", MAX_PROVIDER_BYTES)]
    assert response.http_status == 200
    assert response.retrieval_mode == "bounded_http"
    assert response.observed_at == clock()
    assert response.next_cursor is None and response.exhausted is False
    assert "https://e.org/x" in response.body
    with pytest.raises(TypeError):
        capabilities.provider_fetch("https://www.google.com/search")  # type: ignore[arg-type]


def test_provider_adapter_reports_exact_access_gaps_without_bodies() -> None:
    def fetch(url: str, /, *, max_bytes: int) -> FetchedMetadata:
        raise MetadataFetchError(
            "metadata request returned HTTP 429",
            stage="rate_limit",
            status=429,
        )

    capabilities = load_runtime_capabilities(fetch=fetch, clock=clock)
    response = capabilities.provider_fetch(request())
    assert response.http_status == 429
    assert response.failure_stage == "rate_limit"
    assert response.body == ""


def test_vocabulary_adapter_applies_the_smaller_byte_ceiling() -> None:
    seen: list[int] = []

    def fetch(url: str, /, *, max_bytes: int) -> FetchedMetadata:
        seen.append(max_bytes)
        return FetchedMetadata(url, "text/html", b"<h1>MHG</h1>")

    capabilities = load_runtime_capabilities(fetch=fetch, clock=clock)
    document = capabilities.vocabulary_transport(
        "https://example.org/tool", max_bytes=MAX_VOCABULARY_BYTES * 4
    )
    assert seen == [MAX_VOCABULARY_BYTES]
    assert document.body == b"<h1>MHG</h1>"


def test_default_adapters_use_the_pinned_bounded_transport() -> None:
    resolved: list[str] = []
    requested: list[tuple[str, str]] = []

    def resolver(host: str, port: int, *arguments: Any, **keywords: Any) -> list[Any]:
        resolved.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    class Response:
        status = 200

        def __init__(self) -> None:
            self.remaining = b"<main>ok</main>"

        def getheader(self, name: str, default: str | None = None) -> str | None:
            return {"Content-Type": "text/html"}.get(name, default)

        def read(self, amount: int | None = None) -> bytes:
            chunk, self.remaining = self.remaining[:amount], self.remaining[amount:]
            return chunk

    class Connection:
        def __init__(self, destination: RequestDestination) -> None:
            self.destination = destination
            self.response = Response()

        def request(
            self, method: str, url: str, body: bytes | None, headers: dict[str, str]
        ) -> None:
            requested.append((str(self.destination.connect_ip), headers["Host"]))

        def getresponse(self) -> Response:
            return self.response

        def close(self) -> None:
            return None

    def fetch(url: str, /, *, max_bytes: int) -> FetchedMetadata:
        return fetch_public_metadata(
            url,
            max_bytes=max_bytes,
            resolver=resolver,
            connection_factory=lambda destination, timeout: Connection(destination),
        )

    capabilities = load_runtime_capabilities(fetch=fetch, clock=clock)
    response = capabilities.provider_fetch(request())
    assert resolved == ["www.google.com"]
    assert requested == [("93.184.216.34", "www.google.com")]
    assert response.body == "<main>ok</main>"
