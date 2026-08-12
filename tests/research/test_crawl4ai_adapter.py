from __future__ import annotations

import asyncio
import gc
import hashlib
import importlib.metadata
import socket
import sqlite3
import sys
import tomllib
from contextlib import closing
from pathlib import Path

import pytest

from histgerm.research.crawl4ai_adapter import (
    DEFAULT_CACHE_MAX_BYTES,
    DEFAULT_CACHE_TTL_SECONDS,
    CacheDisposition,
    Crawl4AIAdapter,
    Crawl4AIAdapterError,
    Crawl4AIConfig,
    Crawl4AIResult,
    Crawl4AIRuntimeRunner,
    CrawlInvocation,
    NetworkRequest,
    NetworkResponse,
    RawCrawlResult,
)
from histgerm.research.fetching import MetadataFetchError, PinnedMetadataResponse
from histgerm.research.models import RequestDestination

ROOT = Path(__file__).parents[2]


def resolver(
    host: str, port: int, **kwargs: object
) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    addresses = {
        "example.org": ["93.184.216.34"],
        "other.org": ["93.184.216.35"],
        "mixed.example.org": ["93.184.216.34", "127.0.0.1"],
        "private.example.org": ["10.0.0.1"],
    }[host]
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
        for address in addresses
    ]


class FakeRunner:
    def __init__(
        self,
        result: RawCrawlResult | Exception,
        *,
        close_error: Exception | None = None,
    ) -> None:
        self.result = result
        self.close_error = close_error
        self.invocations: list[CrawlInvocation] = []
        self.closed = 0

    async def run(self, invocation: CrawlInvocation) -> RawCrawlResult:
        self.invocations.append(invocation)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


class FakeNetworkTransport:
    def __init__(
        self,
        responses: dict[str, PinnedMetadataResponse],
        *,
        events: list[str] | None = None,
    ) -> None:
        self.responses = responses
        self.events = events if events is not None else []
        self.calls: list[str] = []
        self.max_bytes: list[int] = []

    def fetch(
        self, destination: RequestDestination, *, max_bytes: int
    ) -> PinnedMetadataResponse:
        url = str(destination.url)
        self.events.append(f"transport:{url}")
        self.calls.append(url)
        self.max_bytes.append(max_bytes)
        response = self.responses[url]
        if len(response.body) > max_bytes:
            raise MetadataFetchError(
                "response exceeds streaming limit",
                stage="response_body",
                limit_exceeded=True,
            )
        return response


class GatedRunner(FakeRunner):
    def __init__(
        self,
        requests: list[NetworkRequest],
        result: RawCrawlResult | None = None,
    ) -> None:
        super().__init__(result or raw())
        self.requests = requests
        self.allowed: list[str] = []

    async def run(self, invocation: CrawlInvocation) -> RawCrawlResult:
        self.invocations.append(invocation)
        for request in self.requests:
            if invocation.request_gate.handle(request) is not None:
                self.allowed.append(request.url)
        assert isinstance(self.result, RawCrawlResult)
        return self.result


def raw(**overrides: object) -> RawCrawlResult:
    values: dict[str, object] = {
        "success": True,
        "url": "https://example.org/page",
        "redirected_url": None,
        "html": "<html>raw</html>",
        "cleaned_markdown": "\r\n# Title  \r\n\r\n\r\nText\t \r\n",
        "metadata": {"z": "last", "title": "Page", "ignored": object()},
        "links": {
            "external": [
                {"href": "https://other.org/lead"},
                {"href": "https://other.org/lead"},
            ],
            "internal": [{"href": "https://example.org/about"}],
        },
        "response_headers": {
            "Content-Type": "text/html; charset=utf-8",
            "ETag": "abc",
            "Set-Cookie": "secret=1",
        },
        "status_code": 200,
        "cache_status": "miss",
    }
    values.update(overrides)
    return RawCrawlResult(**values)  # type: ignore[arg-type]


def config(tmp_path: Path, **overrides: object) -> Crawl4AIConfig:
    values: dict[str, object] = {"cache_base_directory": tmp_path / "external"}
    values.update(overrides)
    return Crawl4AIConfig(**values)  # type: ignore[arg-type]


def adapter(
    tmp_path: Path, runner: FakeRunner, **config_overrides: object
) -> Crawl4AIAdapter:
    return Crawl4AIAdapter(
        config=config(tmp_path, **config_overrides),
        runner=runner,
        resolver=resolver,
        crawler_version_provider=lambda: "0.9.2",
        clock=lambda: 10_000.0,
    )


def test_maps_one_exact_url_deterministically_and_never_schedules_links(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(raw())
    result = asyncio.run(adapter(tmp_path, runner).render("https://example.org/page"))

    assert [call.url for call in runner.invocations] == ["https://example.org/page"]
    invocation = runner.invocations[0]
    assert invocation.run_options["deep_crawl_strategy"] is None
    assert invocation.run_options["stream"] is False
    assert invocation.run_options["method"] == "GET"
    assert invocation.run_options["js_code"] is None
    assert invocation.run_options["remove_forms"] is True
    assert invocation.browser_options["accept_downloads"] is False
    assert invocation.browser_options["use_persistent_context"] is False
    assert invocation.browser_options["storage_state"] is None
    assert invocation.browser_options["cookies"] == []
    assert "form submission disabled" in invocation.browser_options["init_scripts"][0]
    assert result.source_url == "https://example.org/page"
    assert result.final_url == result.source_url
    assert result.cleaned_markdown == "# Title\n\nText"
    assert result.structured_metadata == {
        "title": "Page",
        "z": "last",
        "untrusted_page_links": [
            "https://example.org/about",
            "https://other.org/lead",
        ],
    }
    assert result.page_links == (
        "https://example.org/about",
        "https://other.org/lead",
    )
    assert result.response_metadata == {
        "headers": {
            "content-type": "text/html; charset=utf-8",
            "etag": "abc",
        },
        "status_code": 200,
    }
    assert result.raw_content_sha256 == hashlib.sha256(b"<html>raw</html>").hexdigest()
    assert (
        result.cleaned_content_sha256 == hashlib.sha256(b"# Title\n\nText").hexdigest()
    )
    assert len(result.crawl_cache_key) == 64
    assert result.crawler_version == "0.9.2"
    assert result.extractor_version == 1
    assert runner.closed == 1


@pytest.mark.parametrize(
    ("cache_status", "refresh", "expected", "mode"),
    [
        ("hit", False, CacheDisposition.HIT, "enabled"),
        ("hit_validated", False, CacheDisposition.HIT, "enabled"),
        ("miss", False, CacheDisposition.MISS, "enabled"),
        (None, False, CacheDisposition.MISS, "enabled"),
        ("hit", True, CacheDisposition.REFRESH, "write_only"),
    ],
)
def test_exposes_cache_hit_miss_and_refresh(
    tmp_path: Path,
    cache_status: str | None,
    refresh: bool,
    expected: CacheDisposition,
    mode: str,
) -> None:
    runner = FakeRunner(raw(cache_status=cache_status))
    result = asyncio.run(
        adapter(tmp_path, runner).render("https://example.org/page", refresh=refresh)
    )
    assert result.cache_disposition is expected
    assert runner.invocations[0].run_options["cache_mode"] == mode


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/page",
        "https://private.example.org/page",
        "https://mixed.example.org/page",
        "https://user:password@example.org/page",
        "https://example.org/page#fragment",
        "https://EXAMPLE.org/page",
    ],
)
def test_rejects_unsafe_or_noncanonical_destination_before_handoff(
    tmp_path: Path, url: str
) -> None:
    runner = FakeRunner(raw())
    with pytest.raises(Crawl4AIAdapterError) as captured:
        asyncio.run(adapter(tmp_path, runner).render(url))
    assert captured.value.stage == "destination_validation"
    assert runner.invocations == []
    assert runner.closed == 0


def test_redirect_is_metadata_only_and_is_revalidated(tmp_path: Path) -> None:
    runner = FakeRunner(raw(redirected_url="https://other.org/landing"))
    result = asyncio.run(adapter(tmp_path, runner).render("https://example.org/page"))
    assert result.final_url == "https://other.org/landing"
    assert [call.url for call in runner.invocations] == ["https://example.org/page"]

    unsafe = FakeRunner(raw(redirected_url="http://127.0.0.1/private"))
    with pytest.raises(Crawl4AIAdapterError, match="unsafe final redirect") as captured:
        asyncio.run(adapter(tmp_path, unsafe).render("https://example.org/page"))
    assert captured.value.stage == "redirect"
    assert unsafe.closed == 1


@pytest.mark.parametrize(
    ("overrides", "stage"),
    [
        ({"status_code": 401}, "response"),
        ({"cleaned_markdown": "Please verify you are human"}, "challenge"),
        ({"cleaned_markdown": "Cookie consent required"}, "challenge"),
        ({"cleaned_markdown": "Sign in to continue"}, "challenge"),
        ({"cleaned_markdown": "This article is behind a paywall"}, "challenge"),
        ({"downloaded_files": ["file.pdf"]}, "browser_policy"),
        ({"response_headers": {"Content-Type": "application/pdf"}}, "response"),
        ({"success": False, "error_message": "failed"}, "render"),
        ({"cleaned_markdown": " \r\n "}, "extraction"),
    ],
)
def test_stops_safely_on_barriers_payloads_and_failures(
    tmp_path: Path, overrides: dict[str, object], stage: str
) -> None:
    runner = FakeRunner(raw(**overrides))
    with pytest.raises(Crawl4AIAdapterError) as captured:
        asyncio.run(adapter(tmp_path, runner).render("https://example.org/page"))
    assert captured.value.stage == stage
    assert runner.closed == 1


def test_enforces_positive_byte_resource_and_time_ceilings(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="positive"):
        config(tmp_path, max_response_bytes=0)
    with pytest.raises(ValueError, match="positive"):
        config(tmp_path, max_resources=0)
    with pytest.raises(ValueError, match="positive"):
        config(tmp_path, page_timeout_seconds=0)

    too_large = FakeRunner(raw(html="abcdef"))
    with pytest.raises(Crawl4AIAdapterError) as captured:
        asyncio.run(
            adapter(tmp_path, too_large, max_response_bytes=5).render(
                "https://example.org/page"
            )
        )
    assert captured.value.stage == "resource_limit"

    too_many = FakeRunner(raw(resource_count=3))
    with pytest.raises(Crawl4AIAdapterError) as captured:
        asyncio.run(
            adapter(tmp_path, too_many, max_resources=2).render(
                "https://example.org/page"
            )
        )
    assert captured.value.stage == "resource_limit"


def test_cleanup_occurs_after_runner_failure_and_cleanup_failure_is_reported(
    tmp_path: Path,
) -> None:
    failed = FakeRunner(RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(adapter(tmp_path, failed).render("https://example.org/page"))
    assert failed.closed == 1

    cleanup = FakeRunner(raw(), close_error=RuntimeError("close boom"))
    with pytest.raises(Crawl4AIAdapterError, match="cleanup failed") as captured:
        asyncio.run(adapter(tmp_path, cleanup).render("https://example.org/page"))
    assert captured.value.stage == "cleanup"


def test_external_cache_ttl_and_max_size_policy_is_enforced(
    tmp_path: Path,
) -> None:
    cache_config = config(tmp_path, cache_ttl_seconds=100, cache_max_bytes=5)
    root = cache_config.cache_root
    root.mkdir(parents=True)
    stale = root / "stale.bin"
    old = root / "old.bin"
    new = root / "new.bin"
    stale.write_bytes(b"stale")
    old.write_bytes(b"old")
    new.write_bytes(b"newer")
    stale.touch()
    old.touch()
    new.touch()
    import os

    os.utime(stale, (9_000, 9_000))
    os.utime(old, (9_950, 9_950))
    os.utime(new, (9_990, 9_990))

    runner = FakeRunner(raw())
    crawler = Crawl4AIAdapter(
        config=cache_config,
        runner=runner,
        resolver=resolver,
        crawler_version_provider=lambda: "0.9.2",
        clock=lambda: 10_000.0,
    )
    asyncio.run(crawler.render("https://example.org/page"))
    assert not stale.exists()
    assert not old.exists()
    assert new.read_bytes() == b"newer"
    assert runner.invocations[0].cache_base_directory == tmp_path / "external"
    assert DEFAULT_CACHE_TTL_SECONDS == 2_592_000
    assert DEFAULT_CACHE_MAX_BYTES == 536_870_912


def test_cache_path_must_be_explicitly_external_and_not_symlinked(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="outside"):
        Crawl4AIConfig(cache_base_directory=ROOT / "crawler-cache")

    external = tmp_path / "external"
    root = external / ".crawl4ai"
    target = tmp_path / "target"
    target.mkdir()
    try:
        root.parent.mkdir()
        root.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    runner = FakeRunner(raw())
    crawler = Crawl4AIAdapter(
        config=Crawl4AIConfig(cache_base_directory=external),
        runner=runner,
        resolver=resolver,
        crawler_version_provider=lambda: "0.9.2",
    )
    with pytest.raises(Crawl4AIAdapterError, match="symbolic link"):
        asyncio.run(crawler.render("https://example.org/page"))


def test_crawl4ai_runtime_import_is_lazy_and_absence_error_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = (ROOT / "src" / "histgerm" / "research" / "crawl4ai_adapter.py").read_text(
        encoding="utf-8"
    )
    assert "from crawl4ai" not in source
    assert "import crawl4ai" not in source
    sys.modules.pop("crawl4ai", None)
    assert "crawl4ai" not in sys.modules

    def missing(package: str) -> str:
        raise importlib.metadata.PackageNotFoundError(package)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    crawler = Crawl4AIAdapter(
        config=Crawl4AIConfig(cache_base_directory=tmp_path / "external"),
        resolver=resolver,
    )
    with pytest.raises(Crawl4AIAdapterError, match="unavailable") as captured:
        asyncio.run(crawler.render("https://example.org/page"))
    assert captured.value.stage == "dependency"


def test_pinned_version_is_required_before_handoff(tmp_path: Path) -> None:
    runner = FakeRunner(raw())
    crawler = Crawl4AIAdapter(
        config=config(tmp_path),
        runner=runner,
        resolver=resolver,
        crawler_version_provider=lambda: "9.9.9",
    )
    with pytest.raises(Crawl4AIAdapterError, match="expected Crawl4AI"):
        asyncio.run(crawler.render("https://example.org/page"))
    assert runner.invocations == []


def test_gate_resolves_before_each_transmission_and_uses_pinned_destination(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    def ordered_resolver(
        host: str, port: int, **kwargs: object
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        events.append(f"resolve:{host}")
        return resolver(host, port, **kwargs)

    page = "https://example.org/page"
    script = "https://other.org/app.js"
    transport = FakeNetworkTransport(
        {
            page: PinnedMetadataResponse(
                page, 200, {"Content-Type": "text/html"}, b"<html></html>"
            ),
            script: PinnedMetadataResponse(
                script,
                200,
                {"Content-Type": "application/javascript"},
                b"ok",
            ),
        },
        events=events,
    )
    runner = GatedRunner(
        [
            NetworkRequest(page, is_navigation=True),
            NetworkRequest(script, resource_type="script"),
        ]
    )
    crawler = Crawl4AIAdapter(
        config=config(tmp_path),
        runner=runner,
        resolver=ordered_resolver,
        network_transport=transport,
        crawler_version_provider=lambda: "0.9.2",
    )
    asyncio.run(crawler.render(page))
    assert transport.calls == [page, script]
    assert events[1:5] == [
        "resolve:example.org",
        f"transport:{page}",
        "resolve:other.org",
        f"transport:{script}",
    ]
    assert runner.allowed == [page, script]
    assert runner.closed == 1


@pytest.mark.parametrize(
    "unsafe_request",
    [
        NetworkRequest("https://private.example.org/redirect", is_navigation=True),
        NetworkRequest(
            "https://mixed.example.org/frame",
            resource_type="document",
            is_navigation=True,
            is_frame=True,
        ),
        NetworkRequest(
            "https://private.example.org/worker",
            resource_type="worker",
            is_worker=True,
        ),
        NetworkRequest(
            "https://mixed.example.org/app.js",
            resource_type="script",
        ),
    ],
)
def test_unsafe_redirect_frame_worker_or_subresource_is_never_transmitted(
    tmp_path: Path, unsafe_request: NetworkRequest
) -> None:
    page = "https://example.org/page"
    is_redirect = unsafe_request.is_navigation and not unsafe_request.is_frame
    transport = FakeNetworkTransport(
        {
            page: PinnedMetadataResponse(
                page,
                302 if is_redirect else 200,
                (
                    {"Location": unsafe_request.url}
                    if is_redirect
                    else {"Content-Type": "text/html"}
                ),
                b"" if is_redirect else b"<html></html>",
            )
        }
    )
    runner = GatedRunner([NetworkRequest(page, is_navigation=True), unsafe_request])
    crawler = Crawl4AIAdapter(
        config=config(tmp_path),
        runner=runner,
        resolver=resolver,
        network_transport=transport,
        crawler_version_provider=lambda: "0.9.2",
    )
    with pytest.raises(Crawl4AIAdapterError) as captured:
        asyncio.run(crawler.render(page))
    assert captured.value.stage == "destination_validation"
    assert transport.calls == [page]
    assert unsafe_request.url not in transport.calls
    assert runner.allowed == ([] if is_redirect else [page])
    assert runner.closed == 1


def test_resource_ceiling_aborts_before_excess_request_transmission(
    tmp_path: Path,
) -> None:
    urls = [
        "https://example.org/page",
        "https://other.org/one.js",
        "https://other.org/two.js",
    ]
    transport = FakeNetworkTransport(
        {
            url: PinnedMetadataResponse(
                url, 200, {"Content-Type": "application/javascript"}, b"x"
            )
            for url in urls
        }
    )
    runner = GatedRunner(
        [
            NetworkRequest(urls[0], is_navigation=True),
            NetworkRequest(urls[1], resource_type="script"),
            NetworkRequest(urls[2], resource_type="script"),
        ]
    )
    crawler = Crawl4AIAdapter(
        config=config(tmp_path, max_resources=2),
        runner=runner,
        resolver=resolver,
        network_transport=transport,
        crawler_version_provider=lambda: "0.9.2",
    )
    with pytest.raises(Crawl4AIAdapterError) as captured:
        asyncio.run(crawler.render(urls[0]))
    assert captured.value.stage == "resource_limit"
    assert transport.calls == urls[:2]
    assert runner.allowed == urls[:2]
    assert runner.closed == 1


def test_byte_ceiling_is_passed_as_remaining_streaming_budget(
    tmp_path: Path,
) -> None:
    page = "https://example.org/page"
    script = "https://other.org/app.js"
    transport = FakeNetworkTransport(
        {
            page: PinnedMetadataResponse(
                page, 200, {"Content-Type": "text/html"}, b"1234"
            ),
            script: PinnedMetadataResponse(
                script,
                200,
                {"Content-Type": "application/javascript"},
                b"5678",
            ),
        }
    )
    runner = GatedRunner(
        [
            NetworkRequest(page, is_navigation=True),
            NetworkRequest(script, resource_type="script"),
        ]
    )
    crawler = Crawl4AIAdapter(
        config=config(tmp_path, max_response_bytes=6),
        runner=runner,
        resolver=resolver,
        network_transport=transport,
        crawler_version_provider=lambda: "0.9.2",
    )
    with pytest.raises(Crawl4AIAdapterError) as captured:
        asyncio.run(crawler.render(page))
    assert captured.value.stage == "resource_limit"
    assert transport.max_bytes == [6, 2]
    assert runner.allowed == [page]
    assert runner.closed == 1


def test_optional_assets_abort_before_resolution_fetch_or_budget(
    tmp_path: Path,
) -> None:
    page = "https://example.org/page"
    optional = [
        NetworkRequest("https://private.example.org/image.png", resource_type="image"),
        NetworkRequest("https://private.example.org/font.woff2", resource_type="font"),
        NetworkRequest("https://private.example.org/video.mp4", resource_type="media"),
    ]
    transport = FakeNetworkTransport(
        {
            page: PinnedMetadataResponse(
                page, 200, {"Content-Type": "text/html"}, b"<html></html>"
            )
        }
    )
    runner = GatedRunner([NetworkRequest(page, is_navigation=True), *optional])
    crawler = Crawl4AIAdapter(
        config=config(tmp_path, max_resources=1),
        runner=runner,
        resolver=resolver,
        network_transport=transport,
        crawler_version_provider=lambda: "0.9.2",
    )
    asyncio.run(crawler.render(page))
    assert transport.calls == [page]
    assert runner.allowed == [page]
    assert runner.invocations[0].request_gate.resource_count == 1


def test_form_and_script_main_frame_replacements_are_blocked_before_fetch(
    tmp_path: Path,
) -> None:
    page = "https://example.org/page"
    replacement = "https://other.org/replacement"
    transport = FakeNetworkTransport(
        {
            page: PinnedMetadataResponse(
                page, 200, {"Content-Type": "text/html"}, b"<html></html>"
            ),
            replacement: PinnedMetadataResponse(
                replacement, 200, {"Content-Type": "text/html"}, b"replacement"
            ),
        }
    )
    form_runner = GatedRunner(
        [
            NetworkRequest(page, is_navigation=True),
            NetworkRequest(
                replacement,
                method="POST",
                is_navigation=True,
                has_upload=True,
            ),
        ]
    )
    form_crawler = Crawl4AIAdapter(
        config=config(tmp_path),
        runner=form_runner,
        resolver=resolver,
        network_transport=transport,
        crawler_version_provider=lambda: "0.9.2",
    )
    with pytest.raises(Crawl4AIAdapterError) as captured:
        asyncio.run(form_crawler.render(page))
    assert captured.value.stage == "browser_policy"
    assert transport.calls == [page]

    transport.calls.clear()
    script_runner = GatedRunner(
        [
            NetworkRequest(page, is_navigation=True),
            NetworkRequest(replacement, is_navigation=True),
        ]
    )
    script_crawler = Crawl4AIAdapter(
        config=config(tmp_path),
        runner=script_runner,
        resolver=resolver,
        network_transport=transport,
        crawler_version_provider=lambda: "0.9.2",
    )
    with pytest.raises(Crawl4AIAdapterError) as captured:
        asyncio.run(script_crawler.render(page))
    assert captured.value.stage == "browser_policy"
    assert transport.calls == [page]


def test_only_transport_redirect_chain_can_replace_main_frame(
    tmp_path: Path,
) -> None:
    page = "https://example.org/page"
    landing = "https://other.org/landing"
    transport = FakeNetworkTransport(
        {
            page: PinnedMetadataResponse(page, 302, {"Location": landing}, b""),
            landing: PinnedMetadataResponse(
                landing, 200, {"Content-Type": "text/html"}, b"landing"
            ),
        }
    )
    runner = GatedRunner(
        [
            NetworkRequest(page, is_navigation=True),
            NetworkRequest(landing, is_navigation=True),
        ],
        raw(redirected_url=landing),
    )
    crawler = Crawl4AIAdapter(
        config=config(tmp_path),
        runner=runner,
        resolver=resolver,
        network_transport=transport,
        crawler_version_provider=lambda: "0.9.2",
    )
    result = asyncio.run(crawler.render(page))
    assert transport.calls == [page, landing]
    assert result.source_url == page
    assert result.final_url == landing


def test_required_subresource_mime_policy_remains_fatal(tmp_path: Path) -> None:
    page = "https://example.org/page"
    script = "https://other.org/app.js"
    transport = FakeNetworkTransport(
        {
            page: PinnedMetadataResponse(
                page, 200, {"Content-Type": "text/html"}, b"<html></html>"
            ),
            script: PinnedMetadataResponse(
                script, 200, {"Content-Type": "application/pdf"}, b"%PDF"
            ),
        }
    )
    runner = GatedRunner(
        [
            NetworkRequest(page, is_navigation=True),
            NetworkRequest(script, resource_type="script"),
        ]
    )
    crawler = Crawl4AIAdapter(
        config=config(tmp_path),
        runner=runner,
        resolver=resolver,
        network_transport=transport,
        crawler_version_provider=lambda: "0.9.2",
    )
    with pytest.raises(Crawl4AIAdapterError) as captured:
        asyncio.run(crawler.render(page))
    assert captured.value.stage == "response"
    assert transport.calls == [page, script]


@pytest.mark.filterwarnings("error::ResourceWarning")
@pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")
def test_real_crawl4ai_cache_uses_canonical_url_without_sqlite_leaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = "https://example.org/page"
    first_text = "First version historical metadata " * 30
    refreshed_text = "Refreshed version historical metadata " * 30
    rendered_bodies = [
        f"<html><body><h1>First version</h1><p>{first_text}</p></body></html>".encode(),
        (
            f"<html><body><h1>Refreshed version</h1>"
            f"<p>{refreshed_text}</p></body></html>"
        ).encode(),
    ]
    render_calls: list[str] = []

    async def offline_renderer(invocation: CrawlInvocation) -> NetworkResponse:
        render_calls.append(invocation.url)
        if len(render_calls) > len(rendered_bodies):
            raise RuntimeError("synthetic renderer failure")
        body = rendered_bodies[len(render_calls) - 1]
        return NetworkResponse(
            page,
            200,
            {"Content-Type": "text/html"},
            body,
        )

    runtime = Crawl4AIRuntimeRunner(renderer=offline_renderer)
    crawler = Crawl4AIAdapter(
        config=config(tmp_path),
        runner=runtime,
        resolver=resolver,
        crawler_version_provider=lambda: "0.9.2",
    )

    async def exercise() -> tuple[
        Crawl4AIResult,
        Crawl4AIResult,
        Crawl4AIResult,
        Crawl4AIResult,
        Crawl4AIAdapterError,
    ]:
        first = await crawler.render(page)
        hit = await crawler.render(page)
        refreshed = await crawler.render(page, refresh=True)
        refreshed_hit = await crawler.render(page)
        try:
            await crawler.render(page, refresh=True)
        except Crawl4AIAdapterError as error:
            failure = error
        else:
            raise AssertionError("synthetic renderer failure was not propagated")
        return first, hit, refreshed, refreshed_hit, failure

    first, hit, refreshed, refreshed_hit, failure = asyncio.run(exercise())
    gc.collect()
    assert first.cache_disposition is CacheDisposition.MISS
    assert hit.cache_disposition is CacheDisposition.HIT
    assert refreshed.cache_disposition is CacheDisposition.REFRESH
    assert refreshed_hit.cache_disposition is CacheDisposition.HIT
    assert render_calls == [page, page, page]
    assert failure.stage == "render"
    assert first.cleaned_markdown == hit.cleaned_markdown
    assert refreshed.cleaned_markdown == refreshed_hit.cleaned_markdown
    assert first.cleaned_content_sha256 != refreshed.cleaned_content_sha256
    database = tmp_path / "external" / ".crawl4ai" / "crawl4ai.db"
    assert database.is_file()
    with closing(sqlite3.connect(database)) as connection:
        cached_urls = connection.execute(
            "SELECT url FROM crawled_data ORDER BY url"
        ).fetchall()
    assert cached_urls == [(page,)]
    moved_database = database.with_suffix(".moved")
    database.replace(moved_database)
    moved_database.replace(database)
    assert not list((tmp_path / "external").rglob("robots_cache.db"))

    runtime_module = importlib.import_module("crawl4ai.async_webcrawler")
    monkeypatch.setattr(runtime_module, "RobotsParser", object)
    guarded_runner = Crawl4AIRuntimeRunner(renderer=offline_renderer)
    guarded_crawler = Crawl4AIAdapter(
        config=config(tmp_path),
        runner=guarded_runner,
        resolver=resolver,
        crawler_version_provider=lambda: "0.9.2",
    )
    with pytest.raises(
        Crawl4AIAdapterError, match="unsupported Crawl4AI RobotsParser"
    ) as captured:
        asyncio.run(guarded_crawler.render(page))
    assert captured.value.stage == "dependency"
    gc.collect()


def test_form_guard_is_installed_in_gated_rendering_context() -> None:
    source = (ROOT / "src" / "histgerm" / "research" / "crawl4ai_adapter.py").read_text(
        encoding="utf-8"
    )
    install = source.index("await context.add_init_script(_FORM_GUARD)")
    new_page = source.index("page = await context.new_page()")
    assert install < new_page
    assert 'document.addEventListener("submit", stop, true)' in source
    assert "HTMLFormElement.prototype.submit" in source
    assert "HTMLFormElement.prototype.requestSubmit" in source


def test_dependency_setup_exclusions_and_single_runtime_import_boundary() -> None:
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert configuration["dependency-groups"]["research"] == [
        "crawl4ai==0.9.2",
        "playwright==1.54.0",
    ]
    assert all(
        not dependency.casefold().startswith(("crawl4ai", "playwright"))
        for dependency in configuration["project"]["dependencies"]
    )
    exclusions = set(configuration["tool"]["uv"]["build-backend"]["source-exclude"])
    assert {
        "**/.config/**",
        "**/.crawl4ai/**",
        "**/browser-cache/**",
        "**/browser-pages/**",
        "**/browser-profiles/**",
        "**/crawl4ai-cache/**",
        "**/crawler-cache/**",
        "**/fetched-pages/**",
        "**/generated-markdown/**",
        "**/generated-pages/**",
        "**/*.db",
        "**/*.sqlite",
        "**/*.sqlite3",
        "research/discovery-vocabulary.yaml",
    } <= exclusions
    setup = (ROOT / ".github" / "workflows" / "copilot-setup-steps.yml").read_text(
        encoding="utf-8"
    )
    assert "uv sync --frozen --all-groups" in setup
    assert "uv run python -m playwright install --with-deps chromium" in setup
    assert "CRAWL4_AI_BASE_DIRECTORY" in setup

    imports = []
    for path in (ROOT / "src" / "histgerm").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "import_module(CRAWLER_PACKAGE)" in text:
            imports.append(path.relative_to(ROOT).as_posix())
        assert "from crawl4ai" not in text
        assert "import crawl4ai" not in text
    assert imports == ["src/histgerm/research/crawl4ai_adapter.py"]
