"""Production-safe deterministic capabilities for command-line discovery.

Every request uses the checked-in resolver-pinned, byte-bounded transport in
:mod:`histgerm.research.fetching`. No provider SDK, credential, cache,
registry, hostname fallback, or environment-selected callable is used, and no
retrieved body is written to disk.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from histgerm.catalog import Catalog, load_catalog

from .discovery_orchestration import ProviderResponse
from .fetching import FetchedMetadata, MetadataFetchError, fetch_public_metadata
from .inventory_vocabulary import BoundedTransport, FetchedDocument
from .search_providers import SearchRequest

MAX_PROVIDER_BYTES = 2 * 1024 * 1024
MAX_VOCABULARY_BYTES = 512 * 1024


class MetadataFetch(Protocol):
    """Fetch one public metadata URL through the pinned bounded transport."""

    def __call__(self, url: str, /, *, max_bytes: int) -> FetchedMetadata: ...


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    """Deterministic capabilities the CLI owns without any injected callback."""

    catalog: Catalog
    provider_fetch: Callable[[SearchRequest], ProviderResponse]
    vocabulary_transport: BoundedTransport


def load_runtime_capabilities(
    *,
    fetch: MetadataFetch | None = None,
    clock: Callable[[], datetime] | None = None,
) -> RuntimeCapabilities:
    """Build deterministic discovery capabilities from checked-in code only."""

    transport = fetch if fetch is not None else fetch_public_metadata
    now = clock if clock is not None else _now
    return RuntimeCapabilities(
        catalog=load_catalog(),
        provider_fetch=_provider_fetch(transport, now),
        vocabulary_transport=_vocabulary_transport(transport),
    )


def _provider_fetch(
    transport: MetadataFetch,
    now: Callable[[], datetime],
) -> Callable[[SearchRequest], ProviderResponse]:
    def fetch_provider(request: SearchRequest) -> ProviderResponse:
        """Fetch one provider request, turning transport failures into gaps.

        A wrong argument type is a programming error and still raises. Every
        bounded transport failure, whether a policy or HTTP ``MetadataFetchError``
        or a lower-level ``OSError``/``TimeoutError``, becomes a structured
        provider-gap ``ProviderResponse`` with no body so the channel is recorded
        as an access gap and the remaining channels keep running instead of the
        whole discovery aborting.
        """

        if not isinstance(request, SearchRequest):
            raise TypeError("provider transport accepts only SearchRequest")
        try:
            fetched = transport(request.url, max_bytes=MAX_PROVIDER_BYTES)
        except MetadataFetchError as error:
            return ProviderResponse(
                retrieval_mode=error.mode,
                observed_at=now(),
                http_status=error.status,
                failure_stage=error.stage,
                body="",
            )
        except OSError:
            return ProviderResponse(
                retrieval_mode="bounded_http",
                observed_at=now(),
                http_status=None,
                failure_stage="connection",
                body="",
            )
        body = fetched.body.decode("utf-8", errors="replace")
        return ProviderResponse(
            retrieval_mode="bounded_http",
            observed_at=now(),
            http_status=200,
            body=body,
            next_cursor=None,
            exhausted=False,
        )

    return fetch_provider


def _vocabulary_transport(transport: MetadataFetch) -> BoundedTransport:
    def fetch_document(url: str, *, max_bytes: int) -> FetchedDocument:
        fetched = transport(url, max_bytes=min(max_bytes, MAX_VOCABULARY_BYTES))
        return FetchedDocument(fetched.url, fetched.content_type, fetched.body)

    return fetch_document


def _now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "MAX_PROVIDER_BYTES",
    "MAX_VOCABULARY_BYTES",
    "MetadataFetch",
    "RuntimeCapabilities",
    "load_runtime_capabilities",
]
