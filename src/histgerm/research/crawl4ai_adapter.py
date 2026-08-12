"""Policy boundary for one exact, public Crawl4AI metadata URL.

Crawl4AI's own persistent cache is the only page cache.  It lives below the
configured external ``cache_base_directory`` and is pruned before every run:
files older than ``cache_ttl_seconds`` are removed, then oldest files are
removed until ``cache_max_bytes`` is satisfied.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import importlib.metadata
import os
import re
import socket
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urljoin

from .fetching import PinnedMetadataResponse, fetch_pinned_metadata
from .models import AddressResolver, RequestDestination, resolve_request_destination

CRAWLER_PACKAGE = "crawl4ai"
PINNED_CRAWLER_VERSION = "0.9.2"
EXTRACTOR_VERSION = 1
DEFAULT_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
DEFAULT_CACHE_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_PAGE_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_RESOURCES = 128
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_BASE_DIRECTORY_ENV = "CRAWL4_AI_BASE_DIRECTORY"
_LOADED_BASE_DIRECTORY: Path | None = None
_ROBOTS_CONSTRUCTION_LOCK = threading.Lock()
_CHALLENGE_MARKERS = (
    "access denied",
    "accept all cookies",
    "authentication required",
    "captcha",
    "consent required",
    "cookie consent",
    "log in to continue",
    "paywall",
    "sign in to continue",
    "verify you are human",
)
_SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authenticate",
    "proxy-authorization",
    "set-cookie",
    "www-authenticate",
}
_DENIED_MIME_PARTS = (
    "application/gzip",
    "application/octet-stream",
    "application/pdf",
    "application/vnd",
    "application/zip",
    "audio/",
    "font/",
    "image/",
    "model/",
    "video/",
)
_OPTIONAL_RESOURCE_TYPES = {"font", "image", "media"}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_FORM_GUARD = """
(() => {
  const stop = event => {
    event.preventDefault();
    event.stopImmediatePropagation();
  };
  document.addEventListener("submit", stop, true);
  HTMLFormElement.prototype.submit = function() {
    throw new Error("HistGerm form submission disabled");
  };
  HTMLFormElement.prototype.requestSubmit = function() {
    throw new Error("HistGerm form submission disabled");
  };
})();
""".strip()

type JSONValue = (
    str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]
)


class CacheDisposition(StrEnum):
    """Describe orchestration-visible use of Crawl4AI's persistent cache."""

    HIT = "hit"
    MISS = "miss"
    REFRESH = "refresh"


class Crawl4AIAdapterError(ValueError):
    """Report a deterministic adapter policy or runtime failure."""

    def __init__(self, message: str, *, stage: str) -> None:
        self.stage = stage
        super().__init__(message)


@dataclass(frozen=True)
class Crawl4AIConfig:
    """Configure one external Crawl4AI cache and positive resource ceilings."""

    cache_base_directory: Path = field(default_factory=lambda: _default_cache_base())
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS
    cache_max_bytes: int = DEFAULT_CACHE_MAX_BYTES
    page_timeout_seconds: float = DEFAULT_PAGE_TIMEOUT_SECONDS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    max_resources: int = DEFAULT_MAX_RESOURCES

    def __post_init__(self) -> None:
        base = self.cache_base_directory.expanduser().resolve()
        if not base.is_absolute():
            raise ValueError("Crawl4AI cache base directory must be absolute")
        if base == _REPOSITORY_ROOT or _REPOSITORY_ROOT in base.parents:
            raise ValueError("Crawl4AI cache must be outside the repository")
        if (
            self.cache_ttl_seconds <= 0
            or self.cache_max_bytes <= 0
            or self.page_timeout_seconds <= 0
            or self.max_response_bytes <= 0
            or self.max_resources <= 0
        ):
            raise ValueError("Crawl4AI cache and resource ceilings must be positive")
        object.__setattr__(self, "cache_base_directory", base)

    @property
    def cache_root(self) -> Path:
        """Return Crawl4AI's sole persistent state directory."""

        return self.cache_base_directory / ".crawl4ai"


@dataclass(frozen=True)
class CrawlInvocation:
    """Describe exactly one URL handoff without importing Crawl4AI."""

    url: str
    cache_base_directory: Path
    browser_options: dict[str, Any]
    run_options: dict[str, Any]
    request_gate: NetworkGate


@dataclass(frozen=True)
class NetworkRequest:
    """Describe one browser request before any network transmission."""

    url: str
    method: str = "GET"
    resource_type: str = "document"
    is_navigation: bool = False
    is_frame: bool = False
    is_worker: bool = False
    has_upload: bool = False


@dataclass(frozen=True)
class NetworkResponse:
    """Contain one policy-fetched response used to fulfill a browser route."""

    url: str
    status: int
    headers: dict[str, str]
    body: bytes


class NetworkTransport(Protocol):
    """Fetch one already validated and DNS-pinned destination."""

    def fetch(
        self, destination: RequestDestination, *, max_bytes: int
    ) -> PinnedMetadataResponse: ...


class NetworkGate:
    """Validate and bound every browser request before pinned transmission."""

    def __init__(
        self,
        *,
        resolver: AddressResolver,
        transport: NetworkTransport,
        max_resources: int,
        max_bytes: int,
        selected_url: str,
    ) -> None:
        self._resolver = resolver
        self._transport = transport
        self._max_resources = max_resources
        self._max_bytes = max_bytes
        self._allowed_main_navigations = {selected_url}
        self._resources = 0
        self._bytes = 0
        self._lock = threading.Lock()

    @property
    def resource_count(self) -> int:
        """Return the number of policy-approved transmission attempts."""

        return self._resources

    @property
    def response_bytes(self) -> int:
        """Return bytes received through the gate."""

        return self._bytes

    def handle(self, request: NetworkRequest) -> NetworkResponse | None:
        """Resolve, pin, fetch, and account for one request in strict order."""

        if request.resource_type in _OPTIONAL_RESOURCE_TYPES:
            return None
        if request.method.upper() != "GET" or request.has_upload:
            raise Crawl4AIAdapterError(
                "browser request method or upload is not allowed",
                stage="browser_policy",
            )
        is_main_navigation = request.is_navigation and not request.is_frame
        if is_main_navigation and request.url not in self._allowed_main_navigations:
            raise Crawl4AIAdapterError(
                "page-initiated main-frame navigation is not allowed",
                stage="browser_policy",
            )
        try:
            destination = resolve_request_destination(
                request.url, resolver=self._resolver
            )
        except ValueError as error:
            raise Crawl4AIAdapterError(
                str(error), stage="destination_validation"
            ) from error
        with self._lock:
            if self._resources >= self._max_resources:
                raise Crawl4AIAdapterError(
                    "Crawl4AI resource count limit exceeded",
                    stage="resource_limit",
                )
            remaining = self._max_bytes - self._bytes
            if remaining <= 0:
                raise Crawl4AIAdapterError(
                    "Crawl4AI response byte limit exceeded",
                    stage="resource_limit",
                )
            self._resources += 1
            try:
                response = self._transport.fetch(
                    destination,
                    max_bytes=remaining,
                )
            except Crawl4AIAdapterError:
                raise
            except Exception as error:
                if bool(getattr(error, "limit_exceeded", False)):
                    raise Crawl4AIAdapterError(
                        "Crawl4AI response byte limit exceeded",
                        stage="resource_limit",
                    ) from error
                raise Crawl4AIAdapterError(
                    f"policy-controlled browser request failed: {error}",
                    stage="response",
                ) from error
            if len(response.body) > remaining:
                raise Crawl4AIAdapterError(
                    "Crawl4AI response byte limit exceeded",
                    stage="resource_limit",
                )
            self._bytes += len(response.body)
            if is_main_navigation:
                self._allowed_main_navigations.discard(request.url)
                if response.status in _REDIRECT_STATUSES:
                    location = _header(response.headers, "location")
                    if location is None:
                        raise Crawl4AIAdapterError(
                            "redirect response has no Location",
                            stage="redirect",
                        )
                    redirect_url = urljoin(request.url, location)
                    try:
                        redirect_destination = resolve_request_destination(
                            redirect_url,
                            resolver=self._resolver,
                        )
                    except ValueError as error:
                        raise Crawl4AIAdapterError(
                            f"unsafe redirect: {error}",
                            stage="destination_validation",
                        ) from error
                    self._allowed_main_navigations.add(str(redirect_destination.url))
            if 200 <= response.status < 300:
                content_type = _header(response.headers, "content-type")
                if content_type is None:
                    raise Crawl4AIAdapterError(
                        "browser response has no Content-Type",
                        stage="response",
                    )
                media_type = content_type.partition(";")[0].strip().casefold()
                if any(part in media_type for part in _DENIED_MIME_PARTS):
                    raise Crawl4AIAdapterError(
                        f"browser response type {media_type!r} is not allowed",
                        stage="response",
                    )
                disposition = (
                    _header(response.headers, "content-disposition") or ""
                ).casefold()
                if "attachment" in disposition:
                    raise Crawl4AIAdapterError(
                        "browser download is not allowed",
                        stage="browser_policy",
                    )
        return NetworkResponse(
            response.url,
            response.status,
            dict(response.headers),
            response.body,
        )


class _PinnedNetworkTransport:
    """Use the existing no-redirect, DNS-pinned bounded transport."""

    def fetch(
        self, destination: RequestDestination, *, max_bytes: int
    ) -> PinnedMetadataResponse:
        return fetch_pinned_metadata(
            destination,
            max_bytes=max_bytes,
            allow_browser_script=True,
        )


@dataclass(frozen=True)
class RawCrawlResult:
    """Small dependency-injection surface mapped from a Crawl4AI result."""

    success: bool
    url: str
    redirected_url: str | None
    html: str
    cleaned_markdown: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    links: Mapping[str, Sequence[Mapping[str, Any]]] = field(default_factory=dict)
    response_headers: Mapping[str, Any] = field(default_factory=dict)
    status_code: int | None = None
    cache_status: str | None = None
    error_message: str | None = None
    downloaded_files: Sequence[str] = ()
    resource_count: int = 0


class CrawlRunner(Protocol):
    """Run and clean up one dependency-injected Crawl4AI invocation."""

    async def run(self, invocation: CrawlInvocation) -> RawCrawlResult: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class Crawl4AIResult:
    """Contain deterministic extraction and response metadata for one source."""

    source_url: str
    final_url: str
    cleaned_markdown: str
    structured_metadata: dict[str, JSONValue]
    page_links: tuple[str, ...]
    response_metadata: dict[str, JSONValue]
    raw_content_sha256: str
    cleaned_content_sha256: str
    crawl_cache_key: str
    cache_disposition: CacheDisposition
    crawler_version: str
    extractor_version: int = EXTRACTOR_VERSION


class Crawl4AIAdapter:
    """Validate, render, and extract one selected canonical public URL."""

    def __init__(
        self,
        *,
        config: Crawl4AIConfig | None = None,
        runner: CrawlRunner | None = None,
        resolver: AddressResolver = socket.getaddrinfo,
        network_transport: NetworkTransport | None = None,
        crawler_version_provider: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config or Crawl4AIConfig()
        self._runner = runner
        self._resolver = resolver
        self._network_transport = network_transport or _PinnedNetworkTransport()
        self._crawler_version_provider = (
            crawler_version_provider or _installed_crawler_version
        )
        self._clock = clock

    async def render(self, url: str, *, refresh: bool = False) -> Crawl4AIResult:
        """Render exactly one canonical URL and never schedule extracted links."""

        canonical = self._validate_selected_url(url)
        _enforce_cache_policy(self.config, now=self._clock())
        crawler_version = self._crawler_version_provider()
        if crawler_version != PINNED_CRAWLER_VERSION:
            raise Crawl4AIAdapterError(
                f"expected Crawl4AI {PINNED_CRAWLER_VERSION}, got {crawler_version}",
                stage="dependency",
            )
        runner = self._runner or Crawl4AIRuntimeRunner()
        invocation = _invocation(
            canonical,
            self.config,
            refresh=refresh,
            resolver=self._resolver,
            transport=self._network_transport,
        )
        primary_error: BaseException | None = None
        try:
            try:
                raw = await asyncio.wait_for(
                    runner.run(invocation),
                    timeout=self.config.page_timeout_seconds + 5,
                )
            except TimeoutError as error:
                raise Crawl4AIAdapterError(
                    "Crawl4AI page time limit exceeded", stage="resource_limit"
                ) from error
            return self._map_result(
                canonical,
                raw,
                refresh=refresh,
                crawler_version=crawler_version,
            )
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                await runner.close()
            except Exception as error:
                if primary_error is None:
                    raise Crawl4AIAdapterError(
                        f"Crawl4AI cleanup failed: {error}", stage="cleanup"
                    ) from error

    def _validate_selected_url(self, url: str) -> str:
        if "#" in url:
            raise Crawl4AIAdapterError(
                "selected source URL must not contain a fragment",
                stage="destination_validation",
            )
        try:
            destination = resolve_request_destination(url, resolver=self._resolver)
        except ValueError as error:
            raise Crawl4AIAdapterError(
                str(error), stage="destination_validation"
            ) from error
        canonical = str(destination.url)
        if canonical != url:
            raise Crawl4AIAdapterError(
                f"selected source URL is not canonical; use {canonical!r}",
                stage="destination_validation",
            )
        return canonical

    def _map_result(
        self,
        source_url: str,
        raw: RawCrawlResult,
        *,
        refresh: bool,
        crawler_version: str,
    ) -> Crawl4AIResult:
        if raw.downloaded_files:
            raise Crawl4AIAdapterError(
                "Crawl4AI attempted a download", stage="browser_policy"
            )
        if raw.resource_count > self.config.max_resources:
            raise Crawl4AIAdapterError(
                "Crawl4AI resource count limit exceeded", stage="resource_limit"
            )
        raw_bytes = raw.html.encode("utf-8")
        if len(raw_bytes) > self.config.max_response_bytes:
            raise Crawl4AIAdapterError(
                "Crawl4AI response byte limit exceeded", stage="resource_limit"
            )
        if not raw.success:
            raise Crawl4AIAdapterError(
                raw.error_message or "Crawl4AI rendering failed", stage="render"
            )
        if raw.status_code is not None and not 200 <= raw.status_code < 300:
            raise Crawl4AIAdapterError(
                f"Crawl4AI returned HTTP {raw.status_code}", stage="response"
            )
        final_url = raw.redirected_url or raw.url or source_url
        try:
            final_url = str(
                resolve_request_destination(final_url, resolver=self._resolver).url
            )
        except ValueError as error:
            raise Crawl4AIAdapterError(
                f"unsafe final redirect: {error}", stage="redirect"
            ) from error
        headers = _clean_headers(raw.response_headers)
        content_type = str(headers.get("content-type", "")).casefold()
        if any(part in content_type for part in _DENIED_MIME_PARTS):
            raise Crawl4AIAdapterError(
                f"Crawl4AI response type {content_type!r} is not allowed",
                stage="response",
            )
        markdown = _clean_markdown(raw.cleaned_markdown)
        challenge_text = f"{markdown}\n{raw.error_message or ''}".casefold()
        marker = next(
            (item for item in _CHALLENGE_MARKERS if item in challenge_text), None
        )
        if raw.status_code in {401, 403, 407, 429} or marker is not None:
            detail = f": {marker}" if marker else ""
            raise Crawl4AIAdapterError(
                f"Crawl4AI stopped at an access barrier{detail}",
                stage="challenge",
            )
        if not markdown:
            raise Crawl4AIAdapterError(
                "Crawl4AI produced no cleaned Markdown", stage="extraction"
            )
        structured = _json_mapping(raw.metadata)
        links = _page_links(raw.links)
        structured["untrusted_page_links"] = list(links)
        cache_status = (raw.cache_status or "").casefold()
        disposition = (
            CacheDisposition.REFRESH
            if refresh
            else (
                CacheDisposition.HIT
                if cache_status.startswith("hit")
                else CacheDisposition.MISS
            )
        )
        cache_material = (
            f"{CRAWLER_PACKAGE}:{crawler_version}:"
            f"extractor:{EXTRACTOR_VERSION}:{source_url}"
        )
        response_metadata: dict[str, JSONValue] = {
            "headers": headers,
            "status_code": raw.status_code,
        }
        return Crawl4AIResult(
            source_url=source_url,
            final_url=final_url,
            cleaned_markdown=markdown,
            structured_metadata=structured,
            page_links=links,
            response_metadata=response_metadata,
            raw_content_sha256=hashlib.sha256(raw_bytes).hexdigest(),
            cleaned_content_sha256=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
            crawl_cache_key=hashlib.sha256(cache_material.encode()).hexdigest(),
            cache_disposition=disposition,
            crawler_version=crawler_version,
        )


type RenderFunction = Callable[[CrawlInvocation], Awaitable[NetworkResponse]]


class Crawl4AIRuntimeRunner:
    """Use Crawl4AI's canonical-URL cache with a gated crawler strategy."""

    def __init__(self, *, renderer: RenderFunction | None = None) -> None:
        self._renderer = renderer or _render_through_gate
        self._runtime: Any = None

    async def run(self, invocation: CrawlInvocation) -> RawCrawlResult:
        module = _load_runtime(invocation.cache_base_directory)
        self._runtime = module
        browser_config = module.BrowserConfig(**invocation.browser_options)
        run_options = dict(invocation.run_options)
        run_options["cache_mode"] = getattr(
            module.CacheMode, str(run_options["cache_mode"]).upper()
        )
        run_options["capture_network_requests"] = False
        run_config = module.CrawlerRunConfig(**run_options)
        strategy = _GatedCrawlerStrategy(
            invocation=invocation,
            renderer=self._renderer,
        )
        crawler = _construct_runtime_crawler(
            module,
            crawler_strategy=strategy,
            browser_config=browser_config,
            base_directory=invocation.cache_base_directory,
        )
        async with crawler:
            result = await crawler.arun(
                url=invocation.url,
                config=run_config,
            )
        markdown_value = getattr(result, "markdown", "")
        markdown = getattr(markdown_value, "raw_markdown", str(markdown_value))
        return RawCrawlResult(
            success=bool(getattr(result, "success", False)),
            url=invocation.url,
            redirected_url=getattr(result, "redirected_url", None),
            html=str(getattr(result, "html", "") or ""),
            cleaned_markdown=str(markdown or ""),
            metadata=getattr(result, "metadata", None) or {},
            links=getattr(result, "links", None) or {},
            response_headers=getattr(result, "response_headers", None) or {},
            status_code=getattr(result, "status_code", None),
            cache_status=getattr(result, "cache_status", None),
            error_message=getattr(result, "error_message", None),
            downloaded_files=getattr(result, "downloaded_files", None) or (),
            resource_count=invocation.request_gate.resource_count,
        )

    async def close(self) -> None:
        """Close Crawl4AI's persistent-cache database connections."""

        if self._runtime is not None:
            database = importlib.import_module("crawl4ai.async_database")
            await database.async_db_manager.cleanup()
            self._runtime = None


class _GatedCrawlerStrategy:
    """Minimal Crawl4AI strategy returning only policy-rendered HTML."""

    def __init__(
        self,
        *,
        invocation: CrawlInvocation,
        renderer: RenderFunction,
    ) -> None:
        self._response_type = importlib.import_module(
            "crawl4ai.models"
        ).AsyncCrawlResponse
        self._invocation = invocation
        self._renderer = renderer

    async def __aenter__(self) -> _GatedCrawlerStrategy:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: Any,
    ) -> None:
        return None

    def update_user_agent(self, user_agent: str) -> None:
        """Crawl4AI compatibility; the gated browser owns its fixed agent."""

    async def crawl(self, url: str, **kwargs: Any) -> Any:
        if url != self._invocation.url:
            raise Crawl4AIAdapterError(
                "Crawl4AI requested an unexpected URL",
                stage="browser_policy",
            )
        rendered = await self._renderer(self._invocation)
        return self._response_type(
            html=rendered.body.decode("utf-8"),
            response_headers=rendered.headers,
            status_code=rendered.status,
            redirected_url=rendered.url,
            network_requests=[],
            downloaded_files=[],
        )


class _DisabledRuntimeRobotsParser:
    """Avoid Crawl4AI 0.9.2's unclosed constructor-only SQLite connection."""

    async def can_fetch(self, url: str, user_agent: str = "*") -> bool:
        """The adapter's pinned gate owns network policy; runtime robots is disabled."""

        return True


def _construct_runtime_crawler(
    module: Any,
    *,
    crawler_strategy: _GatedCrawlerStrategy,
    browser_config: Any,
    base_directory: Path,
) -> Any:
    """Construct pinned Crawl4AI without its leaking, unused RobotsParser."""

    runtime_module = importlib.import_module("crawl4ai.async_webcrawler")
    runtime_any = cast(Any, runtime_module)
    with _ROBOTS_CONSTRUCTION_LOCK:
        parser_type = getattr(runtime_module, "RobotsParser", None)
        if (
            getattr(parser_type, "__module__", None) != "crawl4ai.utils"
            or getattr(parser_type, "__name__", None) != "RobotsParser"
            or module.AsyncWebCrawler is not runtime_module.AsyncWebCrawler
        ):
            raise Crawl4AIAdapterError(
                "unsupported Crawl4AI RobotsParser lifecycle",
                stage="dependency",
            )
        runtime_any.RobotsParser = _DisabledRuntimeRobotsParser
        try:
            return module.AsyncWebCrawler(
                crawler_strategy=crawler_strategy,
                config=browser_config,
                base_directory=str(base_directory),
            )
        finally:
            runtime_any.RobotsParser = parser_type


async def _render_through_gate(invocation: CrawlInvocation) -> NetworkResponse:
    """Render with Chromium networking disabled except fulfilled gated routes."""

    try:
        async_playwright = importlib.import_module(
            "playwright.async_api"
        ).async_playwright
    except ImportError as error:
        raise Crawl4AIAdapterError(
            "Playwright is unavailable through the research dependency group",
            stage="dependency",
        ) from error
    route_errors: list[Crawl4AIAdapterError] = []
    main_response: NetworkResponse | None = None
    final_url = invocation.url
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-extensions",
                "--disable-sync",
                "--host-resolver-rules=MAP * ~NOTFOUND",
                "--no-first-run",
            ],
        )
        context = await browser.new_context(
            accept_downloads=False,
            service_workers="block",
            storage_state=None,
        )
        try:
            await context.clear_cookies()

            async def handle_route(route: Any, request: Any) -> None:
                nonlocal main_response
                resource_type = str(request.resource_type)
                frame = request.frame
                browser_request = NetworkRequest(
                    url=request.url,
                    method=request.method,
                    resource_type=resource_type,
                    is_navigation=bool(request.is_navigation_request()),
                    is_frame=(
                        resource_type == "document" and frame != frame.page.main_frame
                    ),
                    is_worker=resource_type in {"worker", "serviceworker"},
                    has_upload=request.post_data is not None,
                )
                try:
                    response = await asyncio.to_thread(
                        invocation.request_gate.handle, browser_request
                    )
                except Crawl4AIAdapterError as error:
                    route_errors.append(error)
                    await route.abort("blockedbyclient")
                    return
                if response is None:
                    await route.abort("blockedbyclient")
                    return
                if browser_request.is_navigation and not browser_request.is_frame:
                    main_response = response
                await route.fulfill(
                    status=response.status,
                    headers=response.headers,
                    body=response.body,
                )

            await context.route("**/*", handle_route)
            if not hasattr(context, "route_web_socket"):
                raise Crawl4AIAdapterError(
                    "Playwright cannot intercept WebSockets",
                    stage="browser_policy",
                )
            await context.route_web_socket("**/*", lambda websocket: websocket.close())
            await context.add_init_script(_FORM_GUARD)
            page = await context.new_page()
            page.on("download", lambda download: asyncio.create_task(download.cancel()))
            try:
                await page.goto(
                    invocation.url,
                    wait_until="domcontentloaded",
                    timeout=int(invocation.run_options["page_timeout"]),
                )
            except Exception as error:
                if route_errors:
                    raise route_errors[0] from error
                raise Crawl4AIAdapterError(
                    f"policy-controlled rendering failed: {error}",
                    stage="render",
                ) from error
            if route_errors:
                raise route_errors[0]
            final_url = page.url
            html = await page.content()
            if main_response is None:
                raise Crawl4AIAdapterError(
                    "main navigation produced no gated response",
                    stage="response",
                )
            return NetworkResponse(
                final_url,
                main_response.status,
                main_response.headers,
                html.encode("utf-8"),
            )
        finally:
            await context.close()
            await browser.close()


def _default_cache_base() -> Path:
    if os.name == "nt":
        parent = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        parent = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return parent / "HistGerm" / "crawl4ai"


def _installed_crawler_version() -> str:
    try:
        return importlib.metadata.version(CRAWLER_PACKAGE)
    except importlib.metadata.PackageNotFoundError as error:
        raise Crawl4AIAdapterError(
            "Crawl4AI is unavailable; install the locked research dependency group",
            stage="dependency",
        ) from error


def _load_runtime(base_directory: Path) -> Any:
    global _LOADED_BASE_DIRECTORY
    resolved = base_directory.resolve()
    if _LOADED_BASE_DIRECTORY is not None and resolved != _LOADED_BASE_DIRECTORY:
        raise Crawl4AIAdapterError(
            "Crawl4AI already uses a different process cache path",
            stage="cache_policy",
        )
    previous = os.environ.get(_BASE_DIRECTORY_ENV)
    os.environ[_BASE_DIRECTORY_ENV] = str(resolved)
    try:
        try:
            module = importlib.import_module(CRAWLER_PACKAGE)
        except ImportError as error:
            raise Crawl4AIAdapterError(
                "Crawl4AI is unavailable; install the locked research dependency group",
                stage="dependency",
            ) from error
    finally:
        if previous is None:
            os.environ.pop(_BASE_DIRECTORY_ENV, None)
        else:
            os.environ[_BASE_DIRECTORY_ENV] = previous
    _LOADED_BASE_DIRECTORY = resolved
    return module


def _invocation(
    url: str,
    config: Crawl4AIConfig,
    *,
    refresh: bool,
    resolver: AddressResolver,
    transport: NetworkTransport,
) -> CrawlInvocation:
    page_timeout_ms = max(1, int(config.page_timeout_seconds * 1000))
    return CrawlInvocation(
        url=url,
        cache_base_directory=config.cache_base_directory,
        browser_options={
            "accept_downloads": False,
            "browser_mode": "dedicated",
            "browser_type": "chromium",
            "cookies": [],
            "create_isolated_context": True,
            "downloads_path": None,
            "enable_stealth": False,
            "extra_args": [
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-extensions",
                "--disable-sync",
                "--no-first-run",
            ],
            "headless": True,
            "ignore_https_errors": False,
            "init_scripts": [_FORM_GUARD],
            "storage_state": None,
            "text_mode": True,
            "use_managed_browser": False,
            "use_persistent_context": False,
            "user_data_dir": None,
            "verbose": False,
        },
        run_options={
            "cache_mode": "write_only" if refresh else "enabled",
            "capture_console_messages": False,
            "capture_mhtml": False,
            "capture_network_requests": True,
            "deep_crawl_strategy": None,
            "exclude_all_images": True,
            "fetch_ssl_certificate": False,
            "js_code": None,
            "magic": False,
            "max_retries": 0,
            "method": "GET",
            "page_timeout": page_timeout_ms,
            "pdf": False,
            "process_iframes": False,
            "remove_consent_popups": False,
            "remove_forms": True,
            "scan_full_page": False,
            "screenshot": False,
            "simulate_user": False,
            "stream": False,
            "verbose": False,
            "wait_for_images": False,
        },
        request_gate=NetworkGate(
            resolver=resolver,
            transport=transport,
            max_resources=config.max_resources,
            max_bytes=config.max_response_bytes,
            selected_url=url,
        ),
    )


def _clean_markdown(value: str) -> str:
    lines = [
        re.sub(r"[ \t]+$", "", line)
        for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    output: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if not blank:
                output.append("")
            blank = True
        else:
            output.append(line)
            blank = False
    return "\n".join(output)


def _json_value(value: Any) -> JSONValue | None:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return _json_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [
            converted for item in value if (converted := _json_value(item)) is not None
        ]
    return None


def _json_mapping(value: Mapping[Any, Any]) -> dict[str, JSONValue]:
    output: dict[str, JSONValue] = {}
    for original_key, item in sorted(
        value.items(), key=lambda pair: str(pair[0]).casefold()
    ):
        key = str(original_key)
        converted = _json_value(item)
        if converted is not None:
            output[key] = converted
    return output


def _clean_headers(value: Mapping[str, Any]) -> dict[str, JSONValue]:
    headers: dict[str, JSONValue] = {}
    for name, header_value in sorted(
        value.items(), key=lambda item: item[0].casefold()
    ):
        normalized = name.casefold().strip()
        if normalized and normalized not in _SENSITIVE_HEADERS:
            headers[normalized] = str(header_value).strip()
    return headers


def _header(value: Mapping[str, Any], name: str) -> str | None:
    expected = name.casefold()
    for header_name, header_value in value.items():
        if header_name.casefold() == expected:
            return str(header_value)
    return None


def _page_links(
    value: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[str, ...]:
    links = {
        href.strip()
        for entries in value.values()
        for entry in entries
        if isinstance((href := entry.get("href")), str) and href.strip()
    }
    return tuple(sorted(links))


def _enforce_cache_policy(config: Crawl4AIConfig, *, now: float) -> None:
    root = config.cache_root
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise Crawl4AIAdapterError(
            "Crawl4AI cache root must not be a symbolic link", stage="cache_policy"
        )
    files: list[tuple[float, int, Path]] = []
    cutoff = now - config.cache_ttl_seconds
    for path in root.rglob("*"):
        if path.is_symlink():
            raise Crawl4AIAdapterError(
                "Crawl4AI cache must not contain symbolic links",
                stage="cache_policy",
            )
        if not path.is_file():
            continue
        stat = path.stat()
        if stat.st_mtime < cutoff:
            path.unlink()
        else:
            files.append((stat.st_mtime, stat.st_size, path))
    total = sum(size for _, size, _ in files)
    for _, size, path in sorted(files):
        if total <= config.cache_max_bytes:
            break
        path.unlink(missing_ok=True)
        total -= size
    for path in sorted(
        (item for item in root.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        with contextlib.suppress(OSError):
            path.rmdir()


__all__ = [
    "CRAWLER_PACKAGE",
    "DEFAULT_CACHE_MAX_BYTES",
    "DEFAULT_CACHE_TTL_SECONDS",
    "EXTRACTOR_VERSION",
    "PINNED_CRAWLER_VERSION",
    "CacheDisposition",
    "Crawl4AIAdapter",
    "Crawl4AIAdapterError",
    "Crawl4AIConfig",
    "Crawl4AIResult",
    "Crawl4AIRuntimeRunner",
    "CrawlInvocation",
    "CrawlRunner",
    "NetworkGate",
    "NetworkRequest",
    "NetworkResponse",
    "NetworkTransport",
    "RawCrawlResult",
]
