"""Bounded public-metadata retrieval for inventory research."""

from __future__ import annotations

import argparse
import http.client
import io
import json
import socket
import ssl
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast
from urllib.parse import urljoin, urlsplit

from .models import AddressResolver, RequestDestination, resolve_request_destination

MAX_METADATA_BYTES = 10 * 1024 * 1024
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_ALLOWED_APPLICATION_TYPES = {
    "application/json",
    "application/ld+json",
    "application/xml",
    "application/xhtml+xml",
}


class MetadataFetchError(ValueError):
    """Report a public-metadata policy or transport failure."""

    def __init__(
        self,
        message: str,
        *,
        mode: RetrievalMode = "bounded_http",
        stage: RetrievalFailureStage,
        status: int | None = None,
        limit_exceeded: bool = False,
    ) -> None:
        self.mode = mode
        self.stage = stage
        self.failure_stage = stage
        self.status = status
        self.limit_exceeded = limit_exceeded
        super().__init__(message)


type RetrievalMode = Literal["bounded_http", "controlled_browser"]
type RetrievalFailureStage = Literal[
    "destination_validation",
    "connection",
    "request",
    "response_status",
    "response_headers",
    "response_body",
    "redirect",
    "robots_fetch",
    "robots_parse",
    "robots_policy",
    "rate_limit",
    "browser_launch",
    "browser_context",
    "browser_request",
    "browser_response",
    "session_budget",
    "challenge",
    "render",
    "cleanup",
]


@dataclass(frozen=True)
class FetchedMetadata:
    """Contain one safely retrieved metadata response."""

    url: str
    content_type: str
    body: bytes
    mode: RetrievalMode = "bounded_http"
    failure_stage: RetrievalFailureStage | None = None


@dataclass(frozen=True)
class PinnedMetadataResponse:
    """Contain one response fetched from an already validated destination."""

    url: str
    status: int
    headers: dict[str, str]
    body: bytes


class _Response(Protocol):
    status: int

    def getheader(self, name: str, default: str | None = None) -> str | None: ...

    def read(self, amount: int | None = None) -> bytes: ...


type ResponseLike = http.client.HTTPResponse | _Response


class _Connection(Protocol):
    def request(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> None: ...

    def getresponse(self) -> ResponseLike: ...

    def close(self) -> None: ...


type ConnectionFactory = Callable[[RequestDestination, float], _Connection]


class _SearchPostRequest(Protocol):
    provider: object
    method: object
    url: object
    body: object
    headers: object


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to one resolved IP while validating the original TLS hostname."""

    def __init__(self, destination: RequestDestination, timeout: float) -> None:
        context = ssl.create_default_context()
        super().__init__(
            destination.hostname,
            destination.port,
            timeout=timeout,
            context=context,
        )
        self._connect_ip = str(destination.connect_ip)
        self._ssl_context = context

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._connect_ip, self.port),
            self.timeout,
        )
        self.sock = self._ssl_context.wrap_socket(raw_socket, server_hostname=self.host)


def _connection(destination: RequestDestination, timeout: float) -> _Connection:
    if destination.url.scheme == "https":
        return _PinnedHTTPSConnection(destination, timeout)
    return http.client.HTTPConnection(
        str(destination.connect_ip), destination.port, timeout=timeout
    )


def _request_target(url: str) -> str:
    parsed = urlsplit(url)
    target = parsed.path or "/"
    return f"{target}?{parsed.query}" if parsed.query else target


def _host_header(destination: RequestDestination) -> str:
    hostname = destination.hostname
    if ":" in hostname:
        hostname = f"[{hostname}]"
    default_port = 443 if destination.url.scheme == "https" else 80
    return (
        hostname
        if destination.port == default_port
        else f"{hostname}:{destination.port}"
    )


def _content_type(response: ResponseLike, *, allow_browser_script: bool = False) -> str:
    header = response.getheader("Content-Type")
    if header is None:
        raise MetadataFetchError(
            "metadata response has no Content-Type", stage="response_headers"
        )
    media_type = header.partition(";")[0].strip().lower()
    allowed = (
        media_type.startswith("text/")
        or media_type in _ALLOWED_APPLICATION_TYPES
        or (
            allow_browser_script
            and media_type
            in {
                "application/javascript",
                "application/ecmascript",
                "application/x-javascript",
            }
        )
        or media_type.endswith("+json")
        or media_type.endswith("+xml")
    )
    if not allowed:
        raise MetadataFetchError(
            f"metadata response type {media_type!r} is not allowed",
            stage="response_headers",
        )
    disposition = (response.getheader("Content-Disposition") or "").casefold()
    if "attachment" in disposition:
        raise MetadataFetchError(
            "metadata response is an attachment", stage="response_headers"
        )
    return header


def _read_bounded(response: ResponseLike, max_bytes: int) -> bytes:
    declared = response.getheader("Content-Length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError as error:
            raise MetadataFetchError(
                "invalid Content-Length", stage="response_headers"
            ) from error
        if length < 0 or length > max_bytes:
            raise MetadataFetchError(
                f"metadata response exceeds {max_bytes} bytes",
                stage="response_headers",
                limit_exceeded=True,
            )
    chunks: list[bytes] = []
    total = 0
    while chunk := response.read(min(64 * 1024, max_bytes - total + 1)):
        total += len(chunk)
        if total > max_bytes:
            raise MetadataFetchError(
                f"metadata response exceeds {max_bytes} bytes",
                stage="response_body",
                limit_exceeded=True,
            )
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_pinned_metadata(
    destination: RequestDestination,
    *,
    method: Literal["GET", "POST"] = "GET",
    body: bytes | None = None,
    request_headers: tuple[tuple[str, str], ...] = (),
    max_bytes: int = MAX_METADATA_BYTES,
    timeout: float = 30,
    connection_factory: ConnectionFactory = _connection,
    allow_browser_script: bool = False,
) -> PinnedMetadataResponse:
    """Fetch one hop from a validated IP without redirects or DNS fallback."""
    if max_bytes < 1 or timeout <= 0:
        raise ValueError("fetch limits must be positive")
    current_url = str(destination.url)
    connection = connection_factory(destination, timeout)
    try:
        try:
            headers = {
                "Accept": "text/html, text/plain, application/json, "
                "application/ld+json, application/xml",
                "Accept-Encoding": "identity",
                "Connection": "close",
                "Host": _host_header(destination),
                "User-Agent": "HistGerm-Metadata-Curator/1",
            }
            if method == "POST":
                headers = _validated_post_headers(request_headers, body)
                headers.update(
                    {
                        "Accept-Encoding": "identity",
                        "Connection": "close",
                        "Host": _host_header(destination),
                        "User-Agent": "HistGerm-Metadata-Curator/1",
                    }
                )
            connection.request(
                method,
                _request_target(current_url),
                body,
                headers,
            )
        except OSError as error:
            raise MetadataFetchError(
                f"metadata request failed: {error}", stage="request"
            ) from error
        try:
            response = connection.getresponse()
        except OSError as error:
            raise MetadataFetchError(
                f"metadata response failed: {error}", stage="connection"
            ) from error
        headers = {
            name: value
            for name in (
                "Content-Type",
                "Content-Disposition",
                "Content-Length",
                "Location",
                "Retry-After",
            )
            if (value := response.getheader(name)) is not None
        }
        if 200 <= response.status < 300:
            _content_type(response, allow_browser_script=allow_browser_script)
        try:
            body = _read_bounded(response, max_bytes)
        except OSError as error:
            raise MetadataFetchError(
                f"metadata response body failed: {error}",
                mode="bounded_http",
                stage="response_body",
            ) from error
        return PinnedMetadataResponse(current_url, response.status, headers, body)
    finally:
        connection.close()


def fetch_public_metadata(
    url: str | object,
    *,
    max_bytes: int = MAX_METADATA_BYTES,
    max_redirects: int = 5,
    timeout: float = 30,
    resolver: AddressResolver = socket.getaddrinfo,
    connection_factory: ConnectionFactory = _connection,
) -> FetchedMetadata:
    """Fetch public metadata with request-time DNS pinning and a streaming cap."""
    if max_bytes < 1 or max_redirects < 0 or timeout <= 0:
        raise ValueError("fetch limits must be positive")
    method: Literal["GET", "POST"] = "GET"
    body: bytes | None = None
    request_headers: tuple[tuple[str, str], ...] = ()
    if isinstance(url, str):
        current_url = url
    else:
        current_url, method, body, request_headers = _validated_search_request(url)
    for redirect_count in range(max_redirects + 1):
        try:
            destination = resolve_request_destination(current_url, resolver=resolver)
        except ValueError as error:
            raise MetadataFetchError(
                str(error), stage="destination_validation"
            ) from error
        current_url = str(destination.url)
        response = fetch_pinned_metadata(
            destination,
            max_bytes=max_bytes,
            timeout=timeout,
            connection_factory=connection_factory,
            method=method,
            body=body,
            request_headers=request_headers,
        )
        if response.status in _REDIRECT_STATUSES:
            if method == "POST" and response.status in {301, 302, 303}:
                raise MetadataFetchError(
                    "POST redirect does not preserve the verified request",
                    stage="redirect",
                    status=response.status,
                )
            location = response.headers.get("Location")
            if location is None:
                raise MetadataFetchError(
                    "redirect response has no Location", stage="redirect"
                )
            if redirect_count == max_redirects:
                raise MetadataFetchError(
                    "metadata redirect limit exceeded", stage="redirect"
                )
            current_url = urljoin(current_url, location)
            continue
        if not 200 <= response.status < 300:
            raise MetadataFetchError(
                f"metadata request returned HTTP {response.status}",
                stage="response_status",
                status=response.status,
            )
        return FetchedMetadata(
            current_url,
            response.headers["Content-Type"],
            response.body,
        )
    raise AssertionError("redirect loop must return or raise")


def _validated_search_request(
    request: object,
) -> tuple[str, Literal["POST"], bytes, tuple[tuple[str, str], ...]]:
    """Accept only the checked-in LAUDATIO JSON POST request shape."""

    candidate = cast(_SearchPostRequest, request)
    if (
        candidate.provider != "laudatio"
        or candidate.method != "POST"
        or candidate.url
        != "https://www.laudatio-repository.org/api/elasticapi/v1/corpora/latest/searchMain"
        or not isinstance(candidate.body, bytes)
        or not isinstance(candidate.headers, tuple)
    ):
        raise ValueError("only the fixed LAUDATIO JSON POST request is supported")
    body = candidate.body
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("LAUDATIO request body must be JSON") from error
    search_data = payload.get("searchData") if isinstance(payload, dict) else None
    if (
        not isinstance(search_data, dict)
        or set(search_data) != {"from", "size", "query"}
        or not isinstance(search_data["from"], int)
        or search_data["from"] < 0
        or search_data["size"] != 20
        or not isinstance(search_data["query"], str)
        or body
        != json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ):
        raise ValueError("LAUDATIO request body is not deterministic")
    headers = candidate.headers
    _validated_post_headers(headers, body)
    return candidate.url, "POST", body, headers


def _validated_post_headers(
    headers: tuple[tuple[str, str], ...], body: bytes | None
) -> dict[str, str]:
    if body is None or headers != (
        ("Accept", "application/json"),
        ("Content-Type", "application/json"),
        ("Api-Version", "v1"),
    ):
        raise ValueError("LAUDATIO POST headers are not allowlisted")
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Api-Version": "v1",
        "Content-Length": str(len(body)),
    }


def main(argv: list[str] | None = None) -> int:
    """Fetch metadata to an explicitly external temporary file."""
    parser = argparse.ArgumentParser(prog="python -m histgerm.research.fetching")
    parser.add_argument("url")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    output = arguments.output.resolve()
    if output == Path.cwd().resolve() or Path.cwd().resolve() in output.parents:
        parser.error("--output must be outside the repository working directory")
    try:
        fetched = fetch_public_metadata(arguments.url)
        with output.open("xb") as stream:
            stream.write(fetched.body)
    except (MetadataFetchError, OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, separators=(",", ":")))
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "url": fetched.url,
                "content_type": fetched.content_type,
                "bytes": len(fetched.body),
                "output": str(output),
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
