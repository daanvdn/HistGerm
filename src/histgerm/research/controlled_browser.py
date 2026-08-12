"""Policy-enforced, opt-in browser rendering for public metadata pages."""

from __future__ import annotations

import importlib
import json
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from html import unescape
from typing import Any, Protocol
from urllib.parse import unquote, urljoin, urlsplit

from .fetching import (
    MAX_METADATA_BYTES,
    FetchedMetadata,
    MetadataFetchError,
    PinnedMetadataResponse,
    fetch_pinned_metadata,
)
from .models import AddressResolver, RequestDestination, resolve_request_destination

CURATOR_USER_AGENT = "HistGerm-Metadata-Curator/1"
DEFAULT_SESSION_BYTES = 20 * 1024 * 1024
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_ALLOWED_RESOURCE_TYPES = {
    "document",
    "script",
    "stylesheet",
    "xhr",
    "fetch",
    "worker",
}
_DENIED_MIME_PARTS = (
    "archive",
    "audio/",
    "font/",
    "model/",
    "video/",
    "application/gzip",
    "application/octet-stream",
    "application/pdf",
    "application/vnd",
    "application/wasm",
    "application/x-7z",
    "application/x-bzip",
    "application/x-rar",
    "application/x-tar",
    "application/zip",
)
_CHALLENGE_MARKERS = (
    "captcha",
    "verify you are human",
    "access denied",
    "authentication required",
    "sign in to continue",
    "log in to continue",
    "paywall",
    "automation is prohibited",
    "automated access is prohibited",
    "consent required",
    "cookie consent",
    "accept all cookies",
)


@dataclass(frozen=True)
class BrowserRequest:
    """Describe one browser-originated request before network access."""

    url: str
    method: str = "GET"
    resource_type: str = "document"
    is_navigation: bool = False
    is_frame: bool = False
    is_worker: bool = False
    has_upload: bool = False


@dataclass(frozen=True)
class BrowserResponse:
    """Describe a policy-fetched response for browser fulfillment."""

    url: str
    status: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class RenderedPage:
    """Contain sanitized candidates extracted from one fresh browser context."""

    url: str
    title: str
    text: str
    metadata: dict[str, str] = field(default_factory=dict)


class BrowserEngine(Protocol):
    """Render through a fresh isolated context using only the supplied handler."""

    def render(
        self,
        url: str,
        request_handler: Callable[[BrowserRequest], BrowserResponse],
    ) -> RenderedPage: ...


class BrowserTransport(Protocol):
    """Fetch one already validated browser request without following redirects."""

    def fetch(
        self, destination: RequestDestination, *, max_bytes: int
    ) -> PinnedMetadataResponse: ...


class BoundedHttpPageState(StrEnum):
    """Classify whether a successful bounded response still requires rendering."""

    COMPLETE = "complete"
    INCOMPLETE_JS_SHELL = "incomplete_js_shell"


@dataclass(frozen=True)
class _RobotsRule:
    allow: bool
    path: str

    def matches(self, target: str) -> bool:
        anchored = self.path.endswith("$")
        pattern = self.path[:-1] if anchored else self.path
        expression = re.escape(pattern).replace(r"\*", ".*")
        suffix = "$" if anchored else ""
        return re.match(f"^{expression}{suffix}", target) is not None

    @property
    def specificity(self) -> int:
        return len(self.path.replace("*", "").removesuffix("$"))


@dataclass(frozen=True)
class _RobotsPolicy:
    rules: tuple[_RobotsRule, ...] = ()
    crawl_delay: float | None = None
    request_interval: float | None = None

    def permits(self, url: str) -> bool:
        target = unquote(urlsplit(url).path or "/")
        query = urlsplit(url).query
        if query:
            target = f"{target}?{query}"
        matches = [rule for rule in self.rules if rule.matches(target)]
        if not matches:
            return True
        longest = max(rule.specificity for rule in matches)
        return any(rule.allow for rule in matches if rule.specificity == longest)

    @property
    def interval(self) -> float:
        return max(self.crawl_delay or 0, self.request_interval or 0)


@dataclass
class _OriginState:
    policy: _RobotsPolicy
    last_request_at: float | None = None


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port or default_port
    authority = host if port == default_port else f"{host}:{port}"
    return f"{parsed.scheme}://{authority}"


def _parse_robots(body: bytes) -> _RobotsPolicy:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MetadataFetchError(
            "robots.txt is not valid UTF-8",
            mode="controlled_browser",
            stage="robots_parse",
        ) from error
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    fields: list[tuple[str, str]] = []
    seen_rule = False
    for raw_line in text.splitlines():
        line = raw_line.partition("#")[0].strip()
        if not line:
            continue
        if ":" not in line:
            raise MetadataFetchError(
                "robots.txt contains a malformed directive",
                mode="controlled_browser",
                stage="robots_parse",
            )
        name, value = (part.strip() for part in line.split(":", 1))
        key = name.casefold()
        if not name or key not in {
            "user-agent",
            "allow",
            "disallow",
            "crawl-delay",
            "request-rate",
            "sitemap",
        }:
            continue
        if key == "user-agent":
            if seen_rule:
                groups.append((agents, fields))
                agents, fields, seen_rule = [], [], False
            if not value:
                raise MetadataFetchError(
                    "robots.txt has an empty user-agent",
                    mode="controlled_browser",
                    stage="robots_parse",
                )
            agents.append(value.casefold())
        else:
            if not agents:
                raise MetadataFetchError(
                    "robots.txt directive precedes user-agent",
                    mode="controlled_browser",
                    stage="robots_parse",
                )
            fields.append((key, value))
            seen_rule = True
    if agents:
        groups.append((agents, fields))

    product = CURATOR_USER_AGENT.casefold()
    matches = [
        (
            max(
                (len(agent) for agent in group_agents if agent in product),
                default=0,
            ),
            data,
        )
        for group_agents, data in groups
        if "*" in group_agents or any(agent in product for agent in group_agents)
    ]
    if not matches:
        return _RobotsPolicy()
    specificity = max(score for score, _ in matches)
    selected = [
        data
        for score, data in matches
        if score == specificity or (specificity == 0 and score == 0)
    ]
    rules: list[_RobotsRule] = []
    crawl_delay: float | None = None
    request_interval: float | None = None
    for data in selected:
        for key, value in data:
            if key in {"allow", "disallow"}:
                if value or key == "allow":
                    rules.append(_RobotsRule(key == "allow", value))
            elif key == "crawl-delay":
                try:
                    delay = float(value)
                except ValueError as error:
                    raise MetadataFetchError(
                        "robots.txt has an invalid crawl-delay",
                        mode="controlled_browser",
                        stage="robots_parse",
                    ) from error
                if delay < 0:
                    raise MetadataFetchError(
                        "robots.txt has a negative crawl-delay",
                        mode="controlled_browser",
                        stage="robots_parse",
                    )
                crawl_delay = max(crawl_delay or 0, delay)
            elif key == "request-rate":
                match = re.fullmatch(r"(\d+)\s*/\s*(\d+(?:\.\d+)?)\s*([smh]?)", value)
                if match is None or int(match.group(1)) < 1:
                    raise MetadataFetchError(
                        "robots.txt has an invalid request-rate",
                        mode="controlled_browser",
                        stage="robots_parse",
                    )
                seconds = float(match.group(2))
                if match.group(3) == "m":
                    seconds *= 60
                elif match.group(3) == "h":
                    seconds *= 3600
                request_interval = max(
                    request_interval or 0, seconds / int(match.group(1))
                )
    return _RobotsPolicy(tuple(rules), crawl_delay, request_interval)


class _PinnedBrowserTransport:
    def fetch(
        self, destination: RequestDestination, *, max_bytes: int
    ) -> PinnedMetadataResponse:
        return fetch_pinned_metadata(
            destination,
            max_bytes=max_bytes,
            allow_browser_script=True,
        )


class ControlledBrowser:
    """Run a browser behind bounded HTTP, robots, DNS, and payload controls."""

    def __init__(
        self,
        engine: BrowserEngine,
        *,
        transport: BrowserTransport | None = None,
        resolver: AddressResolver = socket.getaddrinfo,
        max_response_bytes: int = MAX_METADATA_BYTES,
        max_session_bytes: int = DEFAULT_SESSION_BYTES,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_response_bytes < 1 or max_session_bytes < max_response_bytes:
            raise ValueError("browser byte limits must be positive and ordered")
        self._engine = engine
        self._transport = transport or _PinnedBrowserTransport()
        self._resolver = resolver
        self._max_response_bytes = max_response_bytes
        self._max_session_bytes = max_session_bytes
        self._clock = clock
        self._sleep = sleep
        self._origins: dict[str, _OriginState] = {}
        self._session_bytes = 0

    def fetch(self, url: str) -> FetchedMetadata:
        """Render one page in a new context and return text/metadata JSON only."""
        self._origins = {}
        self._session_bytes = 0
        try:
            page = self._engine.render(url, self._handle_request)
            text = _sanitize_text(page.text)
            title = _sanitize_text(page.title)
            challenge_text = f"{title}\n{text}".casefold()
            if any(marker in challenge_text for marker in _CHALLENGE_MARKERS):
                raise MetadataFetchError(
                    "browser stopped at an access or automation challenge",
                    mode="controlled_browser",
                    stage="challenge",
                )
            body = json.dumps(
                {
                    "url": page.url,
                    "title": title,
                    "text": text,
                    "metadata": {
                        _sanitize_text(key): _sanitize_text(value)
                        for key, value in page.metadata.items()
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
            if len(body) > self._max_response_bytes:
                raise MetadataFetchError(
                    "rendered metadata exceeds response byte limit",
                    mode="controlled_browser",
                    stage="render",
                )
            return FetchedMetadata(
                page.url,
                "application/json; charset=utf-8",
                body,
                mode="controlled_browser",
            )
        except MetadataFetchError:
            raise
        except Exception as error:
            raise MetadataFetchError(
                f"controlled browser failed: {error}",
                mode="controlled_browser",
                stage="render",
            ) from error
        finally:
            self._origins.clear()
            self._session_bytes = 0

    def _response_budget(self, maximum: int) -> int:
        remaining = self._max_session_bytes - self._session_bytes
        if remaining < 1:
            raise MetadataFetchError(
                "browser session byte limit exhausted",
                mode="controlled_browser",
                stage="session_budget",
            )
        return min(maximum, remaining)

    def _consume_session_body(self, body: bytes) -> None:
        if len(body) > self._max_session_bytes - self._session_bytes:
            raise MetadataFetchError(
                "browser session byte limit exceeded",
                mode="controlled_browser",
                stage="session_budget",
            )
        self._session_bytes += len(body)

    def _robots_policy(self, url: str) -> _OriginState:
        origin = _origin(url)
        cached = self._origins.get(origin)
        if cached is not None:
            return cached
        robots_url = f"{origin}/robots.txt"
        current = robots_url
        for redirect_count in range(6):
            maximum = min(self._max_response_bytes, 1024 * 1024)
            budget = self._response_budget(maximum)
            try:
                destination = resolve_request_destination(
                    current, resolver=self._resolver
                )
                response = self._transport.fetch(
                    destination,
                    max_bytes=budget,
                )
            except (MetadataFetchError, ValueError, OSError) as error:
                if (
                    isinstance(error, MetadataFetchError)
                    and error.stage == "session_budget"
                ):
                    raise
                if (
                    isinstance(error, MetadataFetchError)
                    and error.limit_exceeded
                    and budget < maximum
                ):
                    raise MetadataFetchError(
                        "robots.txt would exceed browser session byte limit",
                        mode="controlled_browser",
                        stage="session_budget",
                    ) from error
                raise MetadataFetchError(
                    f"robots.txt retrieval failed for {origin}: {error}",
                    mode="controlled_browser",
                    stage="robots_fetch",
                ) from error
            self._consume_session_body(response.body)
            if response.status in _REDIRECT_STATUSES:
                location = response.headers.get("Location")
                if location is None or redirect_count == 5:
                    raise MetadataFetchError(
                        "robots.txt redirect is invalid or excessive",
                        mode="controlled_browser",
                        stage="robots_fetch",
                    )
                current = urljoin(current, location)
                continue
            if response.status in {404, 410}:
                policy = _RobotsPolicy()
            elif response.status != 200:
                raise MetadataFetchError(
                    f"robots.txt returned HTTP {response.status}",
                    mode="controlled_browser",
                    stage="robots_fetch",
                )
            else:
                content_type = response.headers.get("Content-Type", "")
                if not content_type.casefold().startswith("text/plain"):
                    raise MetadataFetchError(
                        "robots.txt is not text/plain",
                        mode="controlled_browser",
                        stage="robots_parse",
                    )
                policy = _parse_robots(response.body)
            state = _OriginState(policy)
            self._origins[origin] = state
            return state
        raise AssertionError("robots redirect loop must return or raise")

    def _handle_request(self, request: BrowserRequest) -> BrowserResponse:
        if request.method.upper() != "GET" or request.has_upload:
            raise MetadataFetchError(
                "browser request method or upload is not allowed",
                mode="controlled_browser",
                stage="browser_request",
            )
        if request.resource_type not in _ALLOWED_RESOURCE_TYPES:
            raise MetadataFetchError(
                f"browser resource type {request.resource_type!r} is not allowed",
                mode="controlled_browser",
                stage="browser_request",
            )
        try:
            destination = resolve_request_destination(
                request.url, resolver=self._resolver
            )
        except ValueError as error:
            raise MetadataFetchError(
                str(error),
                mode="controlled_browser",
                stage="destination_validation",
            ) from error
        state = self._robots_policy(str(destination.url))
        if not state.policy.permits(str(destination.url)):
            raise MetadataFetchError(
                f"robots.txt disallows {destination.url}",
                mode="controlled_browser",
                stage="robots_policy",
            )
        interval = state.policy.interval
        now = self._clock()
        if state.last_request_at is not None and interval:
            remaining = interval - (now - state.last_request_at)
            if remaining > 0:
                self._sleep(remaining)
                now = self._clock()
        state.last_request_at = now
        budget = self._response_budget(self._max_response_bytes)
        try:
            response = self._transport.fetch(
                destination,
                max_bytes=budget,
            )
        except MetadataFetchError as error:
            if error.stage == "session_budget":
                raise
            if error.limit_exceeded and budget < self._max_response_bytes:
                raise MetadataFetchError(
                    "browser response would exceed session byte limit",
                    mode="controlled_browser",
                    stage="session_budget",
                ) from error
            raise MetadataFetchError(
                str(error),
                mode="controlled_browser",
                stage="browser_response",
            ) from error
        disposition = response.headers.get("Content-Disposition", "").casefold()
        if "attachment" in disposition:
            raise MetadataFetchError(
                "browser download is not allowed",
                mode="controlled_browser",
                stage="browser_response",
            )
        media_type = response.headers.get("Content-Type", "").partition(";")[0].lower()
        if 200 <= response.status < 300 and not media_type:
            raise MetadataFetchError(
                "browser response has no Content-Type",
                mode="controlled_browser",
                stage="browser_response",
            )
        if any(part in media_type for part in _DENIED_MIME_PARTS):
            raise MetadataFetchError(
                f"browser response type {media_type!r} is not allowed",
                mode="controlled_browser",
                stage="browser_response",
            )
        if (
            response.status not in _REDIRECT_STATUSES
            and not 200 <= response.status < 300
        ):
            raise MetadataFetchError(
                f"browser request returned HTTP {response.status}",
                mode="controlled_browser",
                stage="browser_response",
                status=response.status,
            )
        if len(response.body) > self._max_response_bytes:
            raise MetadataFetchError(
                "browser response byte limit exceeded",
                mode="controlled_browser",
                stage="browser_response",
            )
        self._consume_session_body(response.body)
        return BrowserResponse(
            response.url, response.status, dict(response.headers), response.body
        )


def _sanitize_text(value: str) -> str:
    text = unescape(value)
    text = "".join(
        character for character in text if character >= " " or character in "\n\t"
    )
    return " ".join(text.split())


class PlaywrightEngine:
    """Optional Playwright adapter; importing this module does not require it."""

    def __init__(self, *, timeout_ms: int = 30_000) -> None:
        self._timeout_ms = timeout_ms

    def render(
        self,
        url: str,
        request_handler: Callable[[BrowserRequest], BrowserResponse],
    ) -> RenderedPage:
        try:
            sync_playwright: Any = importlib.import_module(
                "playwright.sync_api"
            ).sync_playwright
        except ImportError as error:
            raise MetadataFetchError(
                "Playwright is unavailable; install the research dependency group",
                mode="controlled_browser",
                stage="browser_launch",
            ) from error
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-background-networking",
                    "--disable-breakpad",
                    "--disable-component-update",
                    "--disable-features=WebRtcHideLocalIpsWithMdns",
                    "--disable-sync",
                    "--disable-webrtc",
                    "--host-resolver-rules=MAP * ~NOTFOUND",
                    "--no-first-run",
                    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                ],
            )
            context = browser.new_context(
                accept_downloads=False,
                service_workers="block",
                storage_state=None,
                user_agent=CURATOR_USER_AGENT,
            )
            try:
                context.clear_cookies()
                route_errors: list[MetadataFetchError] = []

                def handle_route(route: Any, request: Any) -> None:
                    try:
                        _fulfill_route(route, request, request_handler)
                    except MetadataFetchError as error:
                        route_errors.append(error)
                        route.abort("blockedbyclient")

                context.route("**/*", handle_route)
                if not hasattr(context, "route_web_socket"):
                    raise MetadataFetchError(
                        "Playwright cannot intercept WebSockets",
                        mode="controlled_browser",
                        stage="browser_context",
                    )
                context.route_web_socket("**/*", lambda websocket: websocket.close())
                page = context.new_page()
                page.add_init_script(
                    """Object.defineProperties(globalThis, {
                      RTCPeerConnection: {value: undefined},
                      webkitRTCPeerConnection: {value: undefined}
                    });"""
                )
                page.on("download", lambda download: download.cancel())
                try:
                    page.goto(url, wait_until="networkidle", timeout=self._timeout_ms)
                except Exception as error:
                    if route_errors:
                        raise route_errors[0] from error
                    raise
                if route_errors:
                    raise route_errors[0]
                if page.locator(
                    'input[type="password"], iframe[src*="captcha" i], '
                    '[class*="captcha" i], [id*="captcha" i]'
                ).count():
                    raise MetadataFetchError(
                        "browser stopped at an authentication or CAPTCHA barrier",
                        mode="controlled_browser",
                        stage="challenge",
                    )
                metadata = page.locator("meta[name], meta[property]").evaluate_all(
                    """elements => Object.fromEntries(elements.map(
                        e => [e.getAttribute('name') || e.getAttribute('property'),
                              e.getAttribute('content') || '']))"""
                )
                return RenderedPage(
                    page.url,
                    page.title(),
                    page.locator("body").inner_text(timeout=self._timeout_ms),
                    dict(metadata),
                )
            finally:
                context.close()
                browser.close()


def _fulfill_route(
    route: Any,
    request: Any,
    handler: Callable[[BrowserRequest], BrowserResponse],
) -> None:
    resource_type = str(request.resource_type)
    frame = request.frame
    browser_request = BrowserRequest(
        request.url,
        request.method,
        resource_type,
        bool(request.is_navigation_request()),
        resource_type == "document" and frame != frame.page.main_frame,
        resource_type in {"worker", "serviceworker"},
        request.post_data is not None,
    )
    response = handler(browser_request)
    route.fulfill(
        status=response.status,
        headers=response.headers,
        body=response.body,
    )


def fetch_with_controlled_browser(
    url: str,
    *,
    enabled: bool = False,
    browser: ControlledBrowser | None = None,
    http_fetch: Callable[..., FetchedMetadata] | None = None,
    http_page_state: BoundedHttpPageState = BoundedHttpPageState.COMPLETE,
) -> FetchedMetadata:
    """Use the browser after an eligible failure or explicit incomplete JS shell."""
    if http_fetch is None:
        from .fetching import fetch_public_metadata

        http_fetch = fetch_public_metadata
    try:
        fetched = http_fetch(url)
    except MetadataFetchError as error:
        eligible = error.stage in {"connection", "request"} or (
            error.stage == "response_status" and error.status in {403, 406}
        )
        if not enabled or not eligible:
            raise
        renderer = browser or ControlledBrowser(PlaywrightEngine())
        return renderer.fetch(url)
    if not enabled or http_page_state is BoundedHttpPageState.COMPLETE:
        return fetched
    renderer = browser or ControlledBrowser(PlaywrightEngine())
    return renderer.fetch(url)


__all__ = [
    "BrowserEngine",
    "BrowserRequest",
    "BrowserResponse",
    "BoundedHttpPageState",
    "ControlledBrowser",
    "PlaywrightEngine",
    "RenderedPage",
    "fetch_with_controlled_browser",
]
