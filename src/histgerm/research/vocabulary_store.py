"""Strict, deterministic storage for untrusted discovery vocabulary state."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, timedelta
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import SplitResult, urlsplit, urlunsplit

import yaml  # type: ignore[import-untyped]
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from histgerm.loading import HistGermLoadingError, load_yaml_mapping_bytes
from histgerm.models import LanguageStage

from ._persistence import (
    bounded_file_lock,
    replace_atomically,
    stable_lock_path,
    write_durable_temporary,
)
from .inventory_vocabulary import InventoryURL, VocabularyKind, normalize_term
from .models import ResourceCategory

type VocabularyPath = str | os.PathLike[str]

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_SOURCES = 512
MAX_TERMS = 2048
MAX_PROVENANCE_ITEMS = 64
MAX_DECISIONS_PER_SOURCE = 512
MAX_CONTEXTS_PER_TERM = 9
MAX_WORDINGS_PER_TERM = 32
MAX_URLS_PER_WORDING = 64
DEFAULT_REFRESH_DAYS = 30
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RESOURCE_ID_RE = re.compile(r"^(?:corpus|tool|dictionary)-[a-z0-9]+(?:-[a-z0-9]+)*$")
_SOURCE_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9]+(?:[-_][a-z0-9]+)*)*$")
_NON_PUBLIC_NAMES = (
    ".localhost",
    ".local",
    ".internal",
    ".home.arpa",
    ".test",
    ".invalid",
    ".example",
)
_EMBEDDED_IPV4_RE = re.compile(r"(?<!\d)(?:\d{1,3}[.-]){3}\d{1,3}(?!\d)")
_LOCK_TIMEOUT_SECONDS = 10.0
_UTF8_BOM = b"\xef\xbb\xbf"


class VocabularyRevisionError(ValueError):
    """Report an optimistic-concurrency revision mismatch."""


class VocabularyPolicyError(ValueError):
    """Report a vocabulary update that violates storage policy."""


class VocabularyValidationError(ValueError):
    """Report a vocabulary document that cannot be safely loaded."""


class VocabularyWriteError(OSError):
    """Report failure to durably replace vocabulary state."""


def canonical_public_url(value: str) -> str:
    """Return one canonical public HTTP(S) URL without credentials or fragments."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("URL must be a valid public HTTP(S) URL") from error
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must use HTTP(S) and include a public host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URLs must not contain credentials")
    host = parsed.hostname.rstrip(".").casefold()
    if host == "localhost" or host.endswith(_NON_PUBLIC_NAMES):
        raise ValueError("URL host must be public")
    try:
        address = ip_address(host.strip("[]"))
    except ValueError:
        if "." not in host:
            raise ValueError("URL host must be a public DNS name") from None
        for match in _EMBEDDED_IPV4_RE.finditer(host):
            try:
                embedded = ip_address(match.group().replace("-", "."))
            except ValueError:
                continue
            if not embedded.is_global:
                raise ValueError("URL host embeds a non-public address") from None
    else:
        if not address.is_global:
            raise ValueError("URL host must be a public address")
    authority = host
    if ":" in host:
        authority = f"[{host}]"
    if port is not None and port != (443 if scheme == "https" else 80):
        authority = f"{authority}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit(SplitResult(scheme, authority, path, parsed.query, ""))


def _canonical_url(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("URL must be a string")
    return canonical_public_url(value)


PublicCanonicalUrl = Annotated[str, AfterValidator(_canonical_url)]


def _reject_empty_strings(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        raise ValueError("empty strings are not allowed")
    if isinstance(value, list):
        for item in value:
            _reject_empty_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_empty_strings(key)
            _reject_empty_strings(item)
    return value


def _unique(values: Sequence[Any], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")


class _VocabularyModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=False,
        validate_assignment=True,
        populate_by_name=False,
    )

    @field_validator("*", mode="before")
    @classmethod
    def reject_empty_strings(cls, value: Any) -> Any:
        return _reject_empty_strings(value)


class VocabularyGap(_VocabularyModel):
    """One transport/classifier gap, never an inventory availability claim."""

    attempted_on: date
    kind: Literal["access", "classifier"]
    reason: str = Field(max_length=500)
    retry_after: date | None = None
    inventory_availability_claim: Literal[False] = False

    @model_validator(mode="after")
    def validate_retry(self) -> VocabularyGap:
        if self.retry_after is not None and self.retry_after < self.attempted_on:
            raise ValueError("gap retry_after must not precede attempted_on")
        return self


class VocabularyDecision(_VocabularyModel):
    """One rationale-free Boolean classification decision for a source."""

    normalized: str = Field(min_length=1, max_length=200)
    suggested_kind: VocabularyKind
    accepted: bool
    active: bool
    first_seen_on: date
    last_seen_on: date

    @field_validator("accepted", "active", mode="before")
    @classmethod
    def require_boolean(cls, value: Any) -> Any:
        if type(value) is not bool:
            raise ValueError("classification decisions must be Boolean")
        return value

    @model_validator(mode="after")
    def validate_decision(self) -> VocabularyDecision:
        if normalize_term(self.normalized) != self.normalized:
            raise ValueError("decision normalized value must be canonical")
        if self.last_seen_on < self.first_seen_on:
            raise ValueError("decision last_seen_on must not precede first_seen_on")
        return self


class VocabularySource(_VocabularyModel):
    """One trusted-inventory URL and its untrusted extraction metadata."""

    url: PublicCanonicalUrl
    resource_ids: list[str] = Field(max_length=MAX_PROVENANCE_ITEMS)
    source_fields: list[str] = Field(max_length=MAX_PROVENANCE_ITEMS)
    last_attempted_on: date | None = None
    last_successful_on: date | None = None
    refresh_after: date
    status: Literal["active", "orphaned"]
    etag: str | None = Field(default=None, max_length=500)
    last_modified: str | None = Field(default=None, max_length=500)
    crawl_cache_key: str | None = Field(default=None, max_length=500)
    raw_content_sha256: str | None = Field(default=None, pattern=_SHA256_RE.pattern)
    cleaned_content_sha256: str | None = Field(default=None, pattern=_SHA256_RE.pattern)
    crawler_version: str | None = Field(default=None, max_length=100)
    extractor_version: int = Field(ge=1)
    gap: VocabularyGap | None = None
    decisions: list[VocabularyDecision] = Field(
        default_factory=list, max_length=MAX_DECISIONS_PER_SOURCE
    )
    untrusted_extracted_content: Literal[True] = True

    @field_validator("resource_ids")
    @classmethod
    def validate_resource_ids(cls, values: list[str]) -> list[str]:
        _unique(values, "source resource_ids")
        if any(_RESOURCE_ID_RE.fullmatch(value) is None for value in values):
            raise ValueError("resource_ids must use inventory category-prefixed IDs")
        return values

    @field_validator("source_fields")
    @classmethod
    def validate_source_fields(cls, values: list[str]) -> list[str]:
        _unique(values, "source source_fields")
        if any(_SOURCE_FIELD_RE.fullmatch(value) is None for value in values):
            raise ValueError("source_fields must use dotted inventory field paths")
        return values

    @model_validator(mode="after")
    def validate_source(self) -> VocabularySource:
        if self.status == "active" and (
            not self.resource_ids or not self.source_fields
        ):
            raise ValueError("active sources require inventory provenance")
        if self.status == "orphaned" and (self.resource_ids or self.source_fields):
            raise ValueError("orphaned sources must not retain current provenance")
        if self.status == "orphaned" and any(
            decision.active for decision in self.decisions
        ):
            raise ValueError("orphaned sources cannot have active decisions")
        if self.last_successful_on is not None and self.last_attempted_on is None:
            raise ValueError("last_successful_on requires last_attempted_on")
        if (
            self.last_attempted_on is not None
            and self.last_successful_on is not None
            and self.last_successful_on > self.last_attempted_on
        ):
            raise ValueError("last_successful_on must not follow last_attempted_on")
        if (
            self.last_attempted_on is not None
            and self.refresh_after < self.last_attempted_on
        ):
            raise ValueError("refresh_after must not precede last_attempted_on")
        if self.gap is not None and self.gap.attempted_on != self.last_attempted_on:
            raise ValueError("gap must describe the source's latest attempt")
        keys = [(item.suggested_kind, item.normalized) for item in self.decisions]
        _unique(keys, "source decisions")
        return self


class VocabularyContext(_VocabularyModel):
    """An untrusted observation context, not a factual inventory claim."""

    category: ResourceCategory
    stage: LanguageStage
    untrusted_observation: Literal[True] = True


class VocabularyWording(_VocabularyModel):
    """Exact wording with active and historical inactive source associations."""

    value: str = Field(min_length=1, max_length=500)
    source_urls: list[PublicCanonicalUrl] = Field(
        default_factory=list, max_length=MAX_URLS_PER_WORDING
    )
    inactive_source_urls: list[PublicCanonicalUrl] = Field(
        default_factory=list, max_length=MAX_URLS_PER_WORDING
    )
    first_seen_on: date
    last_seen_on: date

    @model_validator(mode="after")
    def validate_wording(self) -> VocabularyWording:
        _unique(self.source_urls, "wording active source URLs")
        _unique(self.inactive_source_urls, "wording inactive source URLs")
        if set(self.source_urls) & set(self.inactive_source_urls):
            raise ValueError(
                "wording source associations cannot be active and inactive"
            )
        if not self.source_urls and not self.inactive_source_urls:
            raise ValueError("wording requires at least one source association")
        if self.last_seen_on < self.first_seen_on:
            raise ValueError("wording last_seen_on must not precede first_seen_on")
        return self


class VocabularyTerm(_VocabularyModel):
    """One normalized untrusted discovery lead with exact source wordings."""

    normalized: str = Field(min_length=1, max_length=200)
    kind: VocabularyKind
    active: bool
    contexts: list[VocabularyContext] = Field(
        min_length=1, max_length=MAX_CONTEXTS_PER_TERM
    )
    wordings: list[VocabularyWording] = Field(
        min_length=1, max_length=MAX_WORDINGS_PER_TERM
    )
    trusted_inventory_evidence: Literal[False] = False

    @field_validator("active", mode="before")
    @classmethod
    def require_boolean(cls, value: Any) -> Any:
        if type(value) is not bool:
            raise ValueError("term active state must be Boolean")
        return value

    @model_validator(mode="after")
    def validate_term(self) -> VocabularyTerm:
        if normalize_term(self.normalized) != self.normalized:
            raise ValueError("term normalized value must be canonical")
        context_keys = [(item.category, item.stage) for item in self.contexts]
        _unique(context_keys, "term contexts")
        _unique([item.value for item in self.wordings], "term wording values")
        aggregate_active = any(item.source_urls for item in self.wordings)
        if self.active != aggregate_active:
            raise ValueError("term active must equal its aggregate active associations")
        return self


class DiscoveryVocabulary(_VocabularyModel):
    """Complete revisioned discovery-vocabulary document."""

    schema_version: Literal[1]
    revision: int = Field(ge=0)
    updated_on: date
    sources: list[VocabularySource] = Field(max_length=MAX_SOURCES)
    terms: list[VocabularyTerm] = Field(max_length=MAX_TERMS)

    @model_validator(mode="after")
    def validate_document(self) -> DiscoveryVocabulary:
        source_urls = [item.url for item in self.sources]
        _unique(source_urls, "vocabulary source URLs")
        _unique(
            [(item.kind, item.normalized) for item in self.terms],
            "vocabulary term keys",
        )
        known = set(source_urls)
        active_sources = {item.url for item in self.sources if item.status == "active"}
        for term in self.terms:
            for wording in term.wordings:
                referenced = set(wording.source_urls) | set(
                    wording.inactive_source_urls
                )
                unknown = referenced - known
                if unknown:
                    raise ValueError(
                        "term wording references unknown source URLs "
                        f"{sorted(unknown)!r}"
                    )
                if set(wording.source_urls) - active_sources:
                    raise ValueError(
                        "active term associations require active inventory sources"
                    )
        observed_dates = [
            day
            for source in self.sources
            for day in (source.last_attempted_on, source.last_successful_on)
            if day is not None
        ]
        observed_dates.extend(
            decision.last_seen_on
            for source in self.sources
            for decision in source.decisions
        )
        observed_dates.extend(
            wording.last_seen_on for term in self.terms for wording in term.wordings
        )
        if observed_dates and max(observed_dates) > self.updated_on:
            raise ValueError("updated_on must cover all recorded observations")
        return self


VocabularyDocument = DiscoveryVocabulary


def reconcile_inventory_sources(
    vocabulary: DiscoveryVocabulary,
    inventory_urls: Sequence[InventoryURL],
    *,
    on: date,
    refresh_days: int = DEFAULT_REFRESH_DAYS,
    extractor_version: int = 1,
) -> DiscoveryVocabulary:
    """Reconcile current inventory provenance without fetching or deleting history."""

    if not 1 <= refresh_days <= 365:
        raise VocabularyPolicyError("refresh_days must be between 1 and 365")
    if extractor_version < 1:
        raise VocabularyPolicyError("extractor_version must be positive")
    if len(inventory_urls) > MAX_SOURCES:
        raise VocabularyPolicyError(
            f"inventory URLs exceed the {MAX_SOURCES} source bound"
        )
    incoming: dict[str, InventoryURL] = {}
    for item in inventory_urls:
        url = canonical_public_url(item.url)
        if url in incoming:
            raise VocabularyPolicyError("inventory URLs must be canonical and unique")
        incoming[url] = item
    existing = {source.url: source for source in vocabulary.sources}
    sources: list[VocabularySource] = []
    for url, item in incoming.items():
        current = existing.get(url)
        update = {
            "url": url,
            "resource_ids": list(item.resource_ids),
            "source_fields": list(item.source_fields),
            "status": "active",
        }
        if current is None:
            sources.append(
                VocabularySource(
                    **update,
                    refresh_after=on,
                    extractor_version=extractor_version,
                )
            )
        else:
            sources.append(
                VocabularySource.model_validate(current.model_copy(update=update))
            )
    orphaned_urls = set(existing) - set(incoming)
    for url in orphaned_urls:
        current = existing[url]
        sources.append(
            VocabularySource.model_validate(
                current.model_copy(
                    update={
                        "resource_ids": [],
                        "source_fields": [],
                        "status": "orphaned",
                        "decisions": [
                            decision.model_copy(update={"active": False})
                            for decision in current.decisions
                        ],
                    }
                )
            )
        )
    terms: list[VocabularyTerm] = []
    for term in vocabulary.terms:
        wordings: list[VocabularyWording] = []
        for wording in term.wordings:
            newly_inactive = set(wording.source_urls) & orphaned_urls
            wordings.append(
                VocabularyWording.model_validate(
                    wording.model_copy(
                        update={
                            "source_urls": [
                                url
                                for url in wording.source_urls
                                if url not in orphaned_urls
                            ],
                            "inactive_source_urls": sorted(
                                set(wording.inactive_source_urls) | newly_inactive
                            ),
                        }
                    )
                )
            )
        terms.append(
            VocabularyTerm.model_validate(
                term.model_copy(
                    update={
                        "active": any(wording.source_urls for wording in wordings),
                        "wordings": wordings,
                    }
                )
            )
        )
    return _canonicalize(
        DiscoveryVocabulary.model_validate(
            vocabulary.model_copy(
                update={
                    "updated_on": max(vocabulary.updated_on, on),
                    "sources": sources,
                    "terms": terms,
                }
            )
        )
    )


def select_sources_for_refresh(
    vocabulary: DiscoveryVocabulary,
    *,
    on: date,
    explicit_urls: Sequence[str] = (),
    max_sources: int = 32,
) -> tuple[VocabularySource, ...]:
    """Select bounded active sources that are new, expired, retriable, or explicit."""

    if not 1 <= max_sources <= MAX_SOURCES:
        raise VocabularyPolicyError(f"max_sources must be between 1 and {MAX_SOURCES}")
    requested = {canonical_public_url(url) for url in explicit_urls}
    known = {source.url for source in vocabulary.sources}
    unknown = requested - known
    if unknown:
        raise VocabularyPolicyError(
            f"explicit refresh references unknown sources {sorted(unknown)!r}"
        )
    eligible = [
        source
        for source in vocabulary.sources
        if source.status == "active"
        and (
            source.url in requested
            or source.last_attempted_on is None
            or source.refresh_after <= on
            or (
                source.gap is not None
                and source.gap.retry_after is not None
                and source.gap.retry_after <= on
            )
        )
    ]
    return tuple(sorted(eligible, key=lambda source: source.url)[:max_sources])


def mark_source_access_gap(
    vocabulary: DiscoveryVocabulary,
    *,
    source_url: str,
    attempted_on: date,
    reason: str,
    retry_days: int = 7,
) -> DiscoveryVocabulary:
    """Record one access gap while preserving prior active term associations."""

    if not 1 <= retry_days <= 30:
        raise VocabularyPolicyError("retry_days must be between 1 and 30")
    url = canonical_public_url(source_url)
    if url not in {source.url for source in vocabulary.sources}:
        raise VocabularyPolicyError(f"unknown vocabulary source {url!r}")
    sources = []
    for source in vocabulary.sources:
        if source.url != url:
            sources.append(source)
            continue
        if source.status != "active":
            raise VocabularyPolicyError("cannot refresh an orphaned vocabulary source")
        if (
            source.last_attempted_on is not None
            and attempted_on < source.last_attempted_on
        ):
            raise VocabularyPolicyError("source attempt dates must not move backwards")
        retry_after = attempted_on + timedelta(days=retry_days)
        sources.append(
            VocabularySource.model_validate(
                source.model_copy(
                    update={
                        "last_attempted_on": attempted_on,
                        "refresh_after": retry_after,
                        "gap": VocabularyGap(
                            attempted_on=attempted_on,
                            kind="access",
                            reason=reason,
                            retry_after=retry_after,
                        ),
                    }
                )
            )
        )
    return _canonicalize(
        DiscoveryVocabulary.model_validate(
            vocabulary.model_copy(
                update={
                    "updated_on": max(vocabulary.updated_on, attempted_on),
                    "sources": sources,
                }
            )
        )
    )


def _canonicalize(vocabulary: DiscoveryVocabulary) -> DiscoveryVocabulary:
    sources: list[VocabularySource] = []
    for source in sorted(vocabulary.sources, key=lambda item: item.url):
        decisions = sorted(
            source.decisions,
            key=lambda item: (item.suggested_kind.value, item.normalized),
        )
        sources.append(
            source.model_copy(
                update={
                    "resource_ids": sorted(source.resource_ids),
                    "source_fields": sorted(source.source_fields),
                    "decisions": decisions,
                }
            )
        )
    terms: list[VocabularyTerm] = []
    category_order = {"corpus": 0, "tool": 1, "dictionary": 2}
    stage_order = {stage: index for index, stage in enumerate(LanguageStage)}
    for term in sorted(
        vocabulary.terms, key=lambda item: (item.kind.value, item.normalized)
    ):
        contexts = sorted(
            term.contexts,
            key=lambda item: (category_order[item.category], stage_order[item.stage]),
        )
        wordings = [
            wording.model_copy(
                update={
                    "source_urls": sorted(wording.source_urls),
                    "inactive_source_urls": sorted(wording.inactive_source_urls),
                }
            )
            for wording in sorted(
                term.wordings, key=lambda item: (item.value.casefold(), item.value)
            )
        ]
        terms.append(
            term.model_copy(update={"contexts": contexts, "wordings": wordings})
        )
    return DiscoveryVocabulary.model_validate(
        vocabulary.model_copy(update={"sources": sources, "terms": terms})
    )


def serialize_vocabulary(vocabulary: DiscoveryVocabulary) -> bytes:
    """Serialize a validated document in one byte-stable canonical form."""

    canonical = _canonicalize(DiscoveryVocabulary.model_validate(vocabulary))
    text = str(
        yaml.safe_dump(
            canonical.model_dump(mode="json"),
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )
    )
    return text.encode("utf-8")


def load_vocabulary(path: VocabularyPath) -> DiscoveryVocabulary:
    """Load and fully validate a required vocabulary document."""

    vocabulary_path = Path(path)
    return parse_vocabulary_bytes(
        vocabulary_path.read_bytes(), source_path=str(vocabulary_path)
    )


def parse_vocabulary_bytes(
    data: bytes, *, source_path: str = "<memory>"
) -> DiscoveryVocabulary:
    """Parse one bounded restricted YAML or JSON vocabulary document."""

    if len(data) > MAX_FILE_BYTES:
        raise VocabularyValidationError(
            f"discovery vocabulary exceeds {MAX_FILE_BYTES} bytes"
        )
    if data.startswith(_UTF8_BOM):
        raise VocabularyValidationError(
            "discovery vocabulary must not contain a UTF-8 BOM"
        )
    try:
        raw = load_yaml_mapping_bytes(data, source_path=source_path)
        return _canonicalize(DiscoveryVocabulary.model_validate(raw))
    except HistGermLoadingError as error:
        raise VocabularyValidationError(str(error)) from error


def validate_vocabulary(path: VocabularyPath) -> DiscoveryVocabulary:
    """Validate vocabulary state without bootstrapping or mutation."""

    return load_vocabulary(path)


def vocabulary_status(vocabulary: DiscoveryVocabulary) -> dict[str, int]:
    """Return deterministic counts without treating terms as trusted evidence."""

    active_sources = sum(source.status == "active" for source in vocabulary.sources)
    gaps = sum(source.gap is not None for source in vocabulary.sources)
    active_terms = sum(term.active for term in vocabulary.terms)
    active_associations = sum(
        len(wording.source_urls)
        for term in vocabulary.terms
        for wording in term.wordings
    )
    inactive_associations = sum(
        len(wording.inactive_source_urls)
        for term in vocabulary.terms
        for wording in term.wordings
    )
    return {
        "sources": len(vocabulary.sources),
        "active_sources": active_sources,
        "orphaned_sources": len(vocabulary.sources) - active_sources,
        "access_gaps": gaps,
        "terms": len(vocabulary.terms),
        "active_terms": active_terms,
        "inactive_terms": len(vocabulary.terms) - active_terms,
        "active_associations": active_associations,
        "inactive_associations": inactive_associations,
    }


def apply_vocabulary(
    path: VocabularyPath,
    vocabulary: DiscoveryVocabulary | Mapping[str, Any],
    *,
    expected_revision: int,
) -> DiscoveryVocabulary:
    """Atomically replace vocabulary state after optimistic revision validation."""

    if expected_revision < 0:
        raise VocabularyRevisionError("expected revision must be non-negative")
    proposed = DiscoveryVocabulary.model_validate(vocabulary)
    vocabulary_path = Path(path)
    with _vocabulary_lock(vocabulary_path):
        current = load_vocabulary(vocabulary_path)
        if current.revision != expected_revision:
            raise VocabularyRevisionError(
                f"expected revision {expected_revision}, found {current.revision}"
            )
        if proposed.revision != expected_revision:
            raise VocabularyPolicyError(
                "input revision must equal expected_revision before "
                "the single increment"
            )
        if proposed.updated_on < current.updated_on:
            raise VocabularyPolicyError("updated_on must not move backwards")
        updated = _canonicalize(
            DiscoveryVocabulary.model_validate(
                proposed.model_copy(update={"revision": expected_revision + 1})
            )
        )
        temporary = _write_temporary(vocabulary_path, updated)
        try:
            replace_atomically(temporary, vocabulary_path)
        except OSError as error:
            raise VocabularyWriteError(
                f"could not atomically replace vocabulary {vocabulary_path}"
            ) from error
        return load_vocabulary(vocabulary_path)


def _vocabulary_lock_path(path: Path) -> Path:
    """Return stable external lock state unique to this vocabulary path."""

    return stable_lock_path(path, namespace="vocabulary-locks-v1")


@contextmanager
def _vocabulary_lock(path: Path) -> Iterator[None]:
    """Hold an OS-backed exclusive lock; a crashed owner never leaves it stale."""

    with bounded_file_lock(
        _vocabulary_lock_path(path),
        label="vocabulary",
        timeout=_LOCK_TIMEOUT_SECONDS,
        on_timeout=VocabularyWriteError,
    ):
        yield


def _write_temporary(path: Path, vocabulary: DiscoveryVocabulary) -> Path:
    try:
        payload = serialize_vocabulary(vocabulary)
        return write_durable_temporary(
            path, payload, prefix=f".{path.name}.", suffix=".tmp"
        )
    except (OSError, yaml.YAMLError, ValueError) as error:
        raise VocabularyWriteError(
            f"could not write temporary vocabulary for {path}"
        ) from error


def vocabulary_digest(vocabulary: DiscoveryVocabulary) -> str:
    """Return the SHA-256 digest of canonical serialized state."""

    return hashlib.sha256(serialize_vocabulary(vocabulary)).hexdigest()


__all__ = [
    "DiscoveryVocabulary",
    "DEFAULT_REFRESH_DAYS",
    "MAX_FILE_BYTES",
    "MAX_SOURCES",
    "MAX_TERMS",
    "PublicCanonicalUrl",
    "VocabularyContext",
    "VocabularyDecision",
    "VocabularyDocument",
    "VocabularyGap",
    "VocabularyPolicyError",
    "VocabularyRevisionError",
    "VocabularySource",
    "VocabularyTerm",
    "VocabularyValidationError",
    "VocabularyWording",
    "VocabularyWriteError",
    "apply_vocabulary",
    "canonical_public_url",
    "load_vocabulary",
    "mark_source_access_gap",
    "parse_vocabulary_bytes",
    "reconcile_inventory_sources",
    "select_sources_for_refresh",
    "serialize_vocabulary",
    "validate_vocabulary",
    "vocabulary_digest",
    "vocabulary_status",
]
