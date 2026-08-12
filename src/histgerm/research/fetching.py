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
from typing import Protocol
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


@dataclass(frozen=True)
class FetchedMetadata:
    """Contain one safely retrieved metadata response."""

    url: str
    content_type: str
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


def _content_type(response: ResponseLike) -> str:
    header = response.getheader("Content-Type")
    if header is None:
        raise MetadataFetchError("metadata response has no Content-Type")
    media_type = header.partition(";")[0].strip().lower()
    allowed = (
        media_type.startswith("text/")
        or media_type in _ALLOWED_APPLICATION_TYPES
        or media_type.endswith("+json")
        or media_type.endswith("+xml")
    )
    if not allowed:
        raise MetadataFetchError(
            f"metadata response type {media_type!r} is not allowed"
        )
    disposition = (response.getheader("Content-Disposition") or "").casefold()
    if "attachment" in disposition:
        raise MetadataFetchError("metadata response is an attachment")
    return header


def _read_bounded(response: ResponseLike, max_bytes: int) -> bytes:
    declared = response.getheader("Content-Length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError as error:
            raise MetadataFetchError("invalid Content-Length") from error
        if length < 0 or length > max_bytes:
            raise MetadataFetchError(f"metadata response exceeds {max_bytes} bytes")
    chunks: list[bytes] = []
    total = 0
    while chunk := response.read(min(64 * 1024, max_bytes - total + 1)):
        total += len(chunk)
        if total > max_bytes:
            raise MetadataFetchError(f"metadata response exceeds {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_public_metadata(
    url: str,
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
    current_url = url
    for redirect_count in range(max_redirects + 1):
        destination = resolve_request_destination(current_url, resolver=resolver)
        current_url = str(destination.url)
        connection = connection_factory(destination, timeout)
        try:
            connection.request(
                "GET",
                _request_target(current_url),
                None,
                {
                    "Accept": "text/html, text/plain, application/json, "
                    "application/ld+json, application/xml",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                    "Host": _host_header(destination),
                    "User-Agent": "HistGerm-Metadata-Curator/1",
                },
            )
            response = connection.getresponse()
            if response.status in _REDIRECT_STATUSES:
                location = response.getheader("Location")
                if location is None:
                    raise MetadataFetchError("redirect response has no Location")
                if redirect_count == max_redirects:
                    raise MetadataFetchError("metadata redirect limit exceeded")
                current_url = urljoin(current_url, location)
                continue
            if not 200 <= response.status < 300:
                raise MetadataFetchError(
                    f"metadata request returned HTTP {response.status}"
                )
            content_type = _content_type(response)
            body = _read_bounded(response, max_bytes)
            return FetchedMetadata(current_url, content_type, body)
        except OSError as error:
            raise MetadataFetchError(f"metadata request failed: {error}") from error
        finally:
            connection.close()
    raise AssertionError("redirect loop must return or raise")


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
