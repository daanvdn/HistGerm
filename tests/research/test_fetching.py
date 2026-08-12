from __future__ import annotations

import io
import socket
from collections.abc import Callable
from pathlib import Path

import pytest

from histgerm.research.fetching import (
    MetadataFetchError,
    fetch_pinned_metadata,
    fetch_public_metadata,
    main,
)
from histgerm.research.models import (
    RequestDestination,
    resolve_request_destination,
)


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"", **headers: str) -> None:
        self.status = status
        self._body = io.BytesIO(body)
        self._headers = {
            name.replace("_", "-"): value for name, value in headers.items()
        }

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self._headers.get(name, default)

    def read(self, amount: int | None = None) -> bytes:
        return self._body.read(amount)


class FakeConnection:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.request_data: tuple[str, str, dict[str, str]] | None = None
        self.closed = False

    def request(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> None:
        assert body is None
        self.request_data = method, url, headers

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class FailingReadResponse(FakeResponse):
    def __init__(
        self,
        status: int,
        body: bytes,
        failure: OSError,
        *,
        fail_after_reads: int,
        **headers: str,
    ) -> None:
        super().__init__(status, body, **headers)
        self.failure = failure
        self.fail_after_reads = fail_after_reads
        self.read_count = 0

    def read(self, amount: int | None = None) -> bytes:
        if self.read_count >= self.fail_after_reads:
            raise self.failure
        self.read_count += 1
        return super().read(amount)


def resolver(
    host: str, port: int, **kwargs: object
) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    addresses = {
        "example.org": "93.184.216.34",
        "other.example.org": "93.184.216.35",
    }
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addresses[host], port))]


def factory(
    *responses: FakeResponse,
) -> tuple[
    Callable[[RequestDestination, float], FakeConnection],
    list[tuple[RequestDestination, FakeConnection]],
]:
    pending = iter(responses)
    calls: list[tuple[RequestDestination, FakeConnection]] = []

    def create(destination: RequestDestination, timeout: float) -> FakeConnection:
        assert timeout == 30
        connection = FakeConnection(next(pending))
        calls.append((destination, connection))
        return connection

    return create, calls


def test_missing_content_length_is_streamed_within_limit() -> None:
    create, calls = factory(
        FakeResponse(200, b"metadata", Content_Type="application/json")
    )
    result = fetch_public_metadata(
        "https://example.org/data?q=mhg",
        max_bytes=8,
        resolver=resolver,
        connection_factory=create,
    )
    assert result.body == b"metadata"
    assert result.mode == "bounded_http"
    assert result.failure_stage is None
    _, connection = calls[0]
    assert connection.request_data == (
        "GET",
        "/data?q=mhg",
        {
            "Accept": "text/html, text/plain, application/json, "
            "application/ld+json, application/xml",
            "Accept-Encoding": "identity",
            "Connection": "close",
            "Host": "example.org",
            "User-Agent": "HistGerm-Metadata-Curator/1",
        },
    )
    assert connection.closed


@pytest.mark.parametrize(
    "headers",
    [
        {"Content_Type": "application/zip"},
        {
            "Content_Type": "text/html",
            "Content_Disposition": 'attachment; filename="data.html"',
        },
        {"Content_Type": "text/html", "Content_Length": "11"},
    ],
)
def test_payload_and_declared_size_are_rejected(headers: dict[str, str]) -> None:
    create, _ = factory(FakeResponse(200, b"content", **headers))
    with pytest.raises(MetadataFetchError):
        fetch_public_metadata(
            "https://example.org/data",
            max_bytes=10,
            resolver=resolver,
            connection_factory=create,
        )


def test_undeclared_oversize_body_is_rejected_while_streaming() -> None:
    create, calls = factory(
        FakeResponse(200, b"12345678901", Content_Type="text/plain")
    )
    with pytest.raises(MetadataFetchError, match="exceeds 10 bytes") as caught:
        fetch_public_metadata(
            "https://example.org/data",
            max_bytes=10,
            resolver=resolver,
            connection_factory=create,
        )
    assert caught.value.mode == "bounded_http"
    assert caught.value.stage == "response_body"
    assert caught.value.limit_exceeded
    assert calls[0][1].closed


def test_exact_streaming_limit_is_allowed() -> None:
    create, _ = factory(FakeResponse(200, b"1234567890", Content_Type="text/plain"))
    result = fetch_public_metadata(
        "https://example.org/data",
        max_bytes=10,
        resolver=resolver,
        connection_factory=create,
    )
    assert result.body == b"1234567890"


def test_http_status_failure_has_exact_stage_and_status() -> None:
    create, _ = factory(FakeResponse(404))
    with pytest.raises(MetadataFetchError) as caught:
        fetch_public_metadata(
            "https://example.org/missing",
            resolver=resolver,
            connection_factory=create,
        )
    assert caught.value.stage == "response_status"
    assert caught.value.status == 404


@pytest.mark.parametrize(
    ("status", "headers"),
    [
        (200, {"Content_Type": "text/plain"}),
        (302, {"Location": "https://other.example.org/final"}),
        (500, {}),
    ],
)
@pytest.mark.parametrize("fail_after_reads", [0, 1])
@pytest.mark.parametrize(
    "failure",
    [TimeoutError("body timed out"), ConnectionResetError("body reset")],
)
def test_response_body_socket_failures_are_wrapped_with_exact_audit_stage(
    status: int,
    headers: dict[str, str],
    fail_after_reads: int,
    failure: OSError,
) -> None:
    response = FailingReadResponse(
        status,
        b"partial",
        failure,
        fail_after_reads=fail_after_reads,
        **headers,
    )
    create, calls = factory(response)
    destination = resolve_request_destination(
        "https://example.org/data",
        resolver=resolver,
    )
    with pytest.raises(MetadataFetchError) as caught:
        fetch_pinned_metadata(destination, connection_factory=create)
    assert caught.value.mode == "bounded_http"
    assert caught.value.stage == "response_body"
    assert caught.value.failure_stage == "response_body"
    assert caught.value.__cause__ is failure
    assert str(failure) in str(caught.value)
    assert calls[0][1].closed


def test_redirect_is_resolved_and_pinned_again() -> None:
    create, calls = factory(
        FakeResponse(302, Location="https://other.example.org/final"),
        FakeResponse(200, b"done", Content_Type="text/plain"),
    )
    result = fetch_public_metadata(
        "https://example.org/start",
        resolver=resolver,
        connection_factory=create,
    )
    assert result.url == "https://other.example.org/final"
    assert result.body == b"done"
    assert [call[0].hostname for call in calls] == [
        "example.org",
        "other.example.org",
    ]
    assert all(call[1].closed for call in calls)


def test_cli_rejects_output_inside_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="2"):
        main(["https://example.org/data", "--output", str(tmp_path / "data.html")])
    assert not (tmp_path / "data.html").exists()
