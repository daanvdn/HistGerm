from __future__ import annotations

import json
import socket
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from histgerm.research.controlled_browser import (
    BoundedHttpPageState,
    BrowserRequest,
    BrowserResponse,
    ControlledBrowser,
    RenderedPage,
    fetch_with_controlled_browser,
)
from histgerm.research.fetching import (
    FetchedMetadata,
    MetadataFetchError,
    PinnedMetadataResponse,
)
from histgerm.research.models import RequestDestination

FIXTURES = Path(__file__).parents[1] / "fixtures" / "controlled_browser"
ROOT = Path(__file__).parents[2]


def resolver(
    host: str, port: int, **kwargs: object
) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    addresses = {
        "example.org": ["93.184.216.34"],
        "cdn.example.org": ["93.184.216.35"],
        "other.org": ["93.184.216.36"],
        "mixed.example.org": ["93.184.216.34", "127.0.0.1"],
        "private.example.org": ["10.0.0.1"],
    }[host]
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
        for address in addresses
    ]


class FakeTransport:
    def __init__(
        self,
        responses: dict[str, list[PinnedMetadataResponse] | PinnedMetadataResponse],
    ) -> None:
        self.responses = {
            url: value if isinstance(value, list) else [value]
            for url, value in responses.items()
        }
        self.calls: list[str] = []
        self.max_bytes: list[int] = []

    def fetch(
        self, destination: RequestDestination, *, max_bytes: int
    ) -> PinnedMetadataResponse:
        url = str(destination.url)
        self.calls.append(url)
        self.max_bytes.append(max_bytes)
        values = self.responses[url]
        response = values.pop(0) if len(values) > 1 else values[0]
        if isinstance(response, Exception):
            raise response
        return response


class EnforcingFakeTransport(FakeTransport):
    def fetch(
        self, destination: RequestDestination, *, max_bytes: int
    ) -> PinnedMetadataResponse:
        response = super().fetch(destination, max_bytes=max_bytes)
        if len(response.body) > max_bytes:
            raise MetadataFetchError(
                f"response exceeds {max_bytes} bytes",
                stage="response_body",
                limit_exceeded=True,
            )
        return response


class FakeEngine:
    def __init__(
        self,
        requests: list[BrowserRequest],
        page: RenderedPage | None = None,
    ) -> None:
        self.requests = requests
        self.page = page or RenderedPage(
            "https://example.org/page", "Title", "Rendered metadata", {"x": "y"}
        )
        self.responses: list[BrowserResponse] = []
        self.contexts = 0

    def render(
        self,
        url: str,
        request_handler: Callable[[BrowserRequest], BrowserResponse],
    ) -> RenderedPage:
        self.contexts += 1
        self.responses.extend(request_handler(request) for request in self.requests)
        return self.page


def response(
    url: str,
    status: int = 200,
    body: bytes = b"ok",
    content_type: str = "text/html",
    **headers: str,
) -> PinnedMetadataResponse:
    values = {name.replace("_", "-"): value for name, value in headers.items()}
    if content_type:
        values["Content-Type"] = content_type
    return PinnedMetadataResponse(url, status, values, body)


def robots(
    body: bytes | None = None,
    *,
    status: int = 200,
    origin: str = "https://example.org",
    **headers: str,
) -> PinnedMetadataResponse:
    return response(
        f"{origin}/robots.txt",
        status,
        body if body is not None else (FIXTURES / "allow.txt").read_bytes(),
        "text/plain",
        **headers,
    )


def browser(
    requests: list[BrowserRequest],
    responses: dict[str, list[PinnedMetadataResponse] | PinnedMetadataResponse],
    *,
    max_response_bytes: int = 10 * 1024 * 1024,
    max_session_bytes: int = 20 * 1024 * 1024,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> tuple[ControlledBrowser, FakeEngine, FakeTransport]:
    engine = FakeEngine(requests)
    transport = FakeTransport(responses)
    controlled = ControlledBrowser(
        engine,
        transport=transport,
        resolver=resolver,
        max_response_bytes=max_response_bytes,
        max_session_bytes=max_session_bytes,
        clock=clock or (lambda: 0.0),
        sleep=sleep or (lambda delay: None),
    )
    return controlled, engine, transport


@pytest.mark.parametrize("status", [404, 410])
def test_missing_robots_means_no_published_rules(status: int) -> None:
    page = "https://example.org/page"
    controlled, _, transport = browser(
        [BrowserRequest(page, is_navigation=True)],
        {
            "https://example.org/robots.txt": robots(status=status),
            page: response(page),
        },
    )
    result = controlled.fetch(page)
    assert result.mode == "controlled_browser"
    assert json.loads(result.body)["text"] == "Rendered metadata"
    assert transport.calls == ["https://example.org/robots.txt", page]


def test_robots_allow_delay_and_rate_limit_are_honored() -> None:
    page = "https://example.org/page"
    script = "https://example.org/app.js"
    times = iter([0.0, 0.0, 5.0])
    sleeps: list[float] = []
    controlled, _, _ = browser(
        [
            BrowserRequest(page, is_navigation=True),
            BrowserRequest(script, resource_type="script"),
        ],
        {
            "https://example.org/robots.txt": robots(),
            page: response(page),
            script: response(
                script,
                body=b"render()",
                content_type="application/javascript",
            ),
        },
        clock=lambda: next(times),
        sleep=sleeps.append,
    )
    controlled.fetch(page)
    assert sleeps == [5.0]


@pytest.mark.parametrize("path", ["/private", "/private/data"])
def test_robots_disallow_fails_closed(path: str) -> None:
    url = f"https://example.org{path}"
    controlled, _, _ = browser(
        [BrowserRequest(url, is_navigation=True)],
        {
            "https://example.org/robots.txt": robots(
                (FIXTURES / "disallow.txt").read_bytes()
            ),
            url: response(url),
        },
    )
    with pytest.raises(MetadataFetchError) as caught:
        controlled.fetch(url)
    assert caught.value.stage == "robots_policy"


def test_longer_allow_rule_overrides_disallow() -> None:
    url = "https://example.org/private/public"
    controlled, _, _ = browser(
        [BrowserRequest(url)],
        {
            "https://example.org/robots.txt": robots(
                (FIXTURES / "disallow.txt").read_bytes()
            ),
            url: response(url),
        },
    )
    assert controlled.fetch(url).mode == "controlled_browser"


def test_robots_wildcard_and_end_anchor_are_enforced() -> None:
    url = "https://example.org/export/private.json"
    controlled, _, _ = browser(
        [BrowserRequest(url)],
        {
            "https://example.org/robots.txt": robots(
                b"User-agent: *\nDisallow: /*.json$\n"
            ),
            url: response(url, content_type="application/json"),
        },
    )
    with pytest.raises(MetadataFetchError) as caught:
        controlled.fetch(url)
    assert caught.value.stage == "robots_policy"


@pytest.mark.parametrize(
    ("robots_response", "stage"),
    [
        (robots(status=500), "robots_fetch"),
        (
            robots((FIXTURES / "malformed.txt").read_bytes()),
            "robots_parse",
        ),
        (robots(b"\xff"), "robots_parse"),
    ],
)
def test_robots_failure_and_malformed_rules_fail_closed(
    robots_response: PinnedMetadataResponse, stage: str
) -> None:
    page = "https://example.org/page"
    controlled, _, _ = browser(
        [BrowserRequest(page)],
        {
            "https://example.org/robots.txt": robots_response,
            page: response(page),
        },
    )
    with pytest.raises(MetadataFetchError) as caught:
        controlled.fetch(page)
    assert caught.value.stage == stage


def test_robots_redirect_is_revalidated_and_bounded() -> None:
    page = "https://example.org/page"
    redirected = "https://other.org/policy.txt"
    controlled, _, transport = browser(
        [BrowserRequest(page)],
        {
            "https://example.org/robots.txt": response(
                "https://example.org/robots.txt",
                302,
                b"",
                "",
                Location=redirected,
            ),
            redirected: response(
                redirected,
                body=b"User-agent: *\nAllow: /\n",
                content_type="text/plain",
            ),
            page: response(page),
        },
    )
    controlled.fetch(page)
    assert transport.calls[:2] == ["https://example.org/robots.txt", redirected]


def test_redirect_frames_workers_and_cross_origin_subresources_use_policy() -> None:
    start = "https://example.org/start"
    final = "https://other.org/final"
    frame = "https://cdn.example.org/frame"
    script = "https://cdn.example.org/app.js"
    controlled, _, transport = browser(
        [
            BrowserRequest(start, is_navigation=True),
            BrowserRequest(final, is_navigation=True),
            BrowserRequest(frame, resource_type="document", is_frame=True),
            BrowserRequest(script, resource_type="worker", is_worker=True),
        ],
        {
            "https://example.org/robots.txt": robots(),
            "https://other.org/robots.txt": robots(origin="https://other.org"),
            "https://cdn.example.org/robots.txt": robots(
                origin="https://cdn.example.org"
            ),
            start: response(start, 302, b"", "", Location=final),
            final: response(final),
            frame: response(frame),
            script: response(
                script,
                body=b"work()",
                content_type="application/javascript",
            ),
        },
    )
    controlled.fetch(start)
    assert {
        "https://example.org/robots.txt",
        "https://other.org/robots.txt",
        "https://cdn.example.org/robots.txt",
    } <= set(transport.calls)


@pytest.mark.parametrize("host", ["private.example.org", "mixed.example.org"])
def test_private_and_mixed_dns_destinations_are_rejected(host: str) -> None:
    url = f"https://{host}/page"
    controlled, _, transport = browser([BrowserRequest(url)], {})
    with pytest.raises(MetadataFetchError) as caught:
        controlled.fetch(url)
    assert caught.value.stage == "destination_validation"
    assert transport.calls == []


@pytest.mark.parametrize(
    ("browser_request", "page_response"),
    [
        (
            BrowserRequest("https://example.org/file", resource_type="document"),
            response(
                "https://example.org/file",
                Content_Disposition='attachment; filename="file.html"',
            ),
        ),
        (
            BrowserRequest("https://example.org/file", resource_type="document"),
            response(
                "https://example.org/file",
                content_type="application/zip",
            ),
        ),
        (
            BrowserRequest("https://example.org/file", resource_type="serviceworker"),
            response("https://example.org/file"),
        ),
        (
            BrowserRequest("https://example.org/socket", resource_type="websocket"),
            response("https://example.org/socket"),
        ),
        (
            BrowserRequest("https://example.org/form", method="POST"),
            response("https://example.org/form"),
        ),
    ],
)
def test_download_mime_service_worker_websocket_and_forms_are_blocked(
    browser_request: BrowserRequest, page_response: PinnedMetadataResponse
) -> None:
    controlled, _, _ = browser(
        [browser_request],
        {
            "https://example.org/robots.txt": robots(),
            browser_request.url: page_response,
        },
    )
    with pytest.raises(MetadataFetchError):
        controlled.fetch(browser_request.url)


def test_response_and_session_byte_budgets_are_enforced() -> None:
    page = "https://example.org/page"
    script = "https://example.org/app.js"
    controlled, _, _ = browser(
        [BrowserRequest(page), BrowserRequest(script, resource_type="script")],
        {
            "https://example.org/robots.txt": robots(b""),
            page: response(page, body=b"12345678"),
            script: response(
                script,
                body=b"12345678",
                content_type="application/javascript",
            ),
        },
        max_response_bytes=10,
        max_session_bytes=15,
    )
    with pytest.raises(MetadataFetchError) as caught:
        controlled.fetch(page)
    assert caught.value.stage == "session_budget"

    oversized, _, _ = browser(
        [BrowserRequest(page)],
        {
            "https://example.org/robots.txt": robots(b""),
            page: response(page, body=b"12345678901"),
        },
        max_response_bytes=10,
        max_session_bytes=20,
    )
    with pytest.raises(MetadataFetchError) as caught:
        oversized.fetch(page)
    assert caught.value.stage == "browser_response"


@pytest.mark.parametrize("status", [200, 404, 410])
def test_every_robots_body_counts_toward_session_budget(status: int) -> None:
    page = "https://example.org/page"
    robots_body = b"         " if status == 200 else b"123456789"
    controlled, _, transport = browser(
        [BrowserRequest(page)],
        {
            "https://example.org/robots.txt": robots(robots_body, status=status),
            page: response(page, body=b"12"),
        },
        max_response_bytes=10,
        max_session_bytes=10,
    )
    with pytest.raises(MetadataFetchError) as caught:
        controlled.fetch(page)
    assert caught.value.stage == "session_budget"
    assert transport.calls == ["https://example.org/robots.txt", page]
    assert transport.max_bytes == [10, 1]


def test_robots_and_page_body_may_reach_exact_session_limit() -> None:
    page = "https://example.org/page"
    controlled, _, transport = browser(
        [BrowserRequest(page)],
        {
            "https://example.org/robots.txt": robots(b" " * 198),
            page: response(page, body=b"12"),
        },
        max_response_bytes=200,
        max_session_bytes=200,
    )
    assert controlled.fetch(page).mode == "controlled_browser"
    assert transport.calls == ["https://example.org/robots.txt", page]


def test_redirected_robots_bodies_count_before_page_body() -> None:
    page = "https://example.org/page"
    redirected = "https://other.org/policy.txt"
    controlled, _, transport = browser(
        [BrowserRequest(page)],
        {
            "https://example.org/robots.txt": response(
                "https://example.org/robots.txt",
                302,
                b"123456789",
                "",
                Location=redirected,
            ),
            redirected: response(
                redirected,
                body=b"         ",
                content_type="text/plain",
            ),
            page: response(page, body=b"1234567890123"),
        },
        max_response_bytes=20,
        max_session_bytes=30,
    )
    with pytest.raises(MetadataFetchError) as caught:
        controlled.fetch(page)
    assert caught.value.stage == "session_budget"
    assert transport.calls == [
        "https://example.org/robots.txt",
        redirected,
        page,
    ]
    assert transport.max_bytes == [20, 20, 12]


def test_cross_origin_robots_body_cannot_exceed_aggregate_budget() -> None:
    page = "https://example.org/page"
    script = "https://cdn.example.org/app.js"
    controlled, _, transport = browser(
        [BrowserRequest(page), BrowserRequest(script, resource_type="script")],
        {
            "https://example.org/robots.txt": robots(b"User-agent: *\n"),
            page: response(page, body=b"12345"),
            "https://cdn.example.org/robots.txt": robots(
                b"User-agent: *\n", origin="https://cdn.example.org"
            ),
            script: response(
                script,
                body=b"x",
                content_type="application/javascript",
            ),
        },
        max_response_bytes=20,
        max_session_bytes=30,
    )
    with pytest.raises(MetadataFetchError) as caught:
        controlled.fetch(page)
    assert caught.value.stage == "session_budget"
    assert transport.calls == [
        "https://example.org/robots.txt",
        page,
        "https://cdn.example.org/robots.txt",
    ]
    assert script not in transport.calls
    assert transport.max_bytes[-1] == 11


def test_transport_limit_failure_maps_to_cross_origin_session_budget() -> None:
    page = "https://example.org/page"
    script = "https://cdn.example.org/app.js"
    engine = FakeEngine(
        [BrowserRequest(page), BrowserRequest(script, resource_type="script")]
    )
    transport = EnforcingFakeTransport(
        {
            "https://example.org/robots.txt": robots(b"User-agent: *\n"),
            page: response(page, body=b"12345"),
            "https://cdn.example.org/robots.txt": robots(
                b"User-agent: *\n", origin="https://cdn.example.org"
            ),
            script: response(
                script,
                body=b"x",
                content_type="application/javascript",
            ),
        }
    )
    controlled = ControlledBrowser(
        engine,
        transport=transport,
        resolver=resolver,
        max_response_bytes=20,
        max_session_bytes=30,
    )
    with pytest.raises(MetadataFetchError) as caught:
        controlled.fetch(page)
    assert caught.value.stage == "session_budget"
    cause = caught.value.__cause__
    assert isinstance(cause, MetadataFetchError)
    assert cause.limit_exceeded
    assert script not in transport.calls


def test_context_and_policy_state_are_isolated_per_fetch() -> None:
    page = "https://example.org/page"
    controlled, engine, transport = browser(
        [BrowserRequest(page)],
        {
            "https://example.org/robots.txt": [
                robots(status=404),
                robots(status=404),
            ],
            page: response(page),
        },
    )
    controlled.fetch(page)
    controlled.fetch(page)
    assert engine.contexts == 2
    assert transport.calls.count("https://example.org/robots.txt") == 2


def test_challenges_stop_without_interaction() -> None:
    page = "https://example.org/page"
    engine = FakeEngine(
        [BrowserRequest(page)],
        RenderedPage(page, "Verify you are human", "CAPTCHA"),
    )
    controlled = ControlledBrowser(
        engine,
        transport=FakeTransport(
            {
                "https://example.org/robots.txt": robots(status=404),
                page: response(page),
            }
        ),
        resolver=resolver,
    )
    with pytest.raises(MetadataFetchError) as caught:
        controlled.fetch(page)
    assert caught.value.stage == "challenge"


def test_fallback_is_opt_in_and_never_replaces_successful_http() -> None:
    page = "https://example.org/page"
    browser_result, engine, _ = browser(
        [BrowserRequest(page)],
        {
            "https://example.org/robots.txt": robots(status=404),
            page: response(page),
        },
    )
    successful = FetchedMetadata(page, "text/html", b"http")
    assert (
        fetch_with_controlled_browser(
            page,
            enabled=True,
            browser=browser_result,
            http_fetch=lambda url: successful,
            http_page_state=BoundedHttpPageState.COMPLETE,
        )
        is successful
    )
    assert engine.contexts == 0
    assert (
        fetch_with_controlled_browser(
            page,
            enabled=False,
            browser=browser_result,
            http_fetch=lambda url: successful,
            http_page_state=BoundedHttpPageState.INCOMPLETE_JS_SHELL,
        )
        is successful
    )
    assert engine.contexts == 0
    assert (
        fetch_with_controlled_browser(
            page,
            enabled=True,
            browser=browser_result,
            http_fetch=lambda url: successful,
            http_page_state=BoundedHttpPageState.INCOMPLETE_JS_SHELL,
        ).mode
        == "controlled_browser"
    )
    assert engine.contexts == 1

    def failed(url: str) -> FetchedMetadata:
        raise MetadataFetchError("HTTP 403", stage="response_status", status=403)

    with pytest.raises(MetadataFetchError):
        fetch_with_controlled_browser(
            page, enabled=False, browser=browser_result, http_fetch=failed
        )
    assert (
        fetch_with_controlled_browser(
            page, enabled=True, browser=browser_result, http_fetch=failed
        ).mode
        == "controlled_browser"
    )

    def missing(url: str) -> FetchedMetadata:
        raise MetadataFetchError("HTTP 404", stage="response_status", status=404)

    with pytest.raises(MetadataFetchError):
        fetch_with_controlled_browser(
            page, enabled=True, browser=browser_result, http_fetch=missing
        )


def test_playwright_is_scoped_and_cloud_install_is_locked() -> None:
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert all(
        not dependency.casefold().startswith(("crawl4ai", "playwright"))
        for dependency in configuration["project"]["dependencies"]
    )
    assert configuration["dependency-groups"]["research"] == [
        "crawl4ai==0.9.2",
        "playwright==1.54.0",
    ]
    exclusions = configuration["tool"]["uv"]["build-backend"]["source-exclude"]
    assert {"**/.playwright/**", "**/ms-playwright/**"} <= set(exclusions)
    setup = (ROOT / ".github" / "workflows" / "copilot-setup-steps.yml").read_text(
        encoding="utf-8"
    )
    assert "uv sync --frozen --all-groups" in setup
    assert "Install Crawl4AI-compatible Chromium" in setup
    assert "CRAWL4_AI_BASE_DIRECTORY: ${{ runner.temp }}/histgerm-crawl4ai" in setup
    assert "uv run python -m playwright install --with-deps chromium" in setup
