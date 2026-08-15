"""Strict, versioned exchange and checkpoint models for resumable discovery.

A prompt-hosted coordinator cannot pass live Python callbacks into a separate
CLI process, so the CLI keeps every deterministic capability (resolver-pinned
retrieval, parsing, pagination, bounds, and state) and exchanges only bounded
model elicitation and item inspection judgments as validated JSON files.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ._persistence import replace_atomically, write_durable_temporary
from .fetching import RetrievalFailureStage, RetrievalMode
from .inventory_vocabulary import (
    InventoryURL,
    InventoryVocabulary,
    MiningGap,
    URLKind,
    VocabularyKind,
    VocabularyTerm,
)
from .search_providers import (
    Assessment,
    PaginationState,
    PaginationStopReason,
    ResponseFormat,
    ResultClassification,
    ResultInspection,
    SearchAssessmentRecord,
    SearchProvider,
    SearchResult,
)

CHECKPOINT_SCHEMA_VERSION = 1
MAX_CHECKPOINT_BYTES = 8 * 1024 * 1024
MAX_EXCHANGE_BYTES = 512 * 1024
MAX_ELICITATION_OUTPUT_CHARS = 32_000
MAX_INSPECTION_ITEMS = 100


class DiscoveryProtocolError(ValueError):
    """Report an unsafe, stale, or malformed capability exchange."""


class StaleCheckpointError(DiscoveryProtocolError):
    """Report a response that does not target the current checkpoint revision.

    The run is never discarded on a stale or future response: the error carries
    the current expected revision and the checkpoint's outstanding capability
    requests so the coordinator can re-answer the current checkpoint without
    repeating any confirmed work. No response is applied, so no confirmed item
    or run is ever mutated by a mismatched exchange.
    """

    def __init__(
        self,
        message: str,
        *,
        run_id: str,
        expected_revision: int,
        requests: tuple[CapabilityRequest, ...],
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.expected_revision = expected_revision
        self.requests = requests


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ModelElicitationRequest(_Strict):
    """One exact bounded elicitation prompt for the hosting agent."""

    kind: Literal["model_elicitation"] = "model_elicitation"
    request_id: str = Field(min_length=1, max_length=200)
    iteration: int = Field(ge=1, le=10)
    prompt_kind: Literal["broad", "follow_up"]
    prompt: str = Field(min_length=1, max_length=50_000)
    max_output_chars: int = Field(ge=1, le=MAX_ELICITATION_OUTPUT_CHARS)
    max_candidates: int = Field(ge=1, le=50)


class ModelElicitationResponse(_Strict):
    """Raw name-and-alias JSON text returned for one elicitation request."""

    kind: Literal["model_elicitation"]
    request_id: str = Field(min_length=1, max_length=200)
    output: str = Field(min_length=1, max_length=MAX_ELICITATION_OUTPUT_CHARS)


class InspectionItem(_Strict):
    """One normalized, untrusted search result offered for inspection."""

    position: int = Field(ge=1)
    url: str = Field(min_length=1, max_length=2_000)
    title: str = Field(max_length=1_000)
    snippet: str | None = Field(default=None, max_length=2_000)


class ResultInspectionRequest(_Strict):
    """One bounded batch of items awaiting lead/unrelated classification."""

    kind: Literal["result_inspection"] = "result_inspection"
    request_id: str = Field(min_length=1, max_length=200)
    category: Literal["corpus", "tool", "dictionary"]
    stage: str = Field(min_length=1, max_length=32)
    query: str = Field(min_length=1, max_length=2_000)
    provider: str = Field(min_length=1, max_length=64)
    channel: str = Field(min_length=1, max_length=64)
    locale: str = Field(min_length=1, max_length=16)
    items: list[InspectionItem] = Field(min_length=1, max_length=MAX_INSPECTION_ITEMS)


class InspectionVerdict(_Strict):
    """Exactly one classification and concise reason for one item position."""

    position: int = Field(ge=1)
    classification: ResultClassification
    reason: str = Field(min_length=1, max_length=300)


class ResultInspectionResponse(_Strict):
    """Complete per-position verdicts for one inspection request."""

    kind: Literal["result_inspection"]
    request_id: str = Field(min_length=1, max_length=200)
    verdicts: list[InspectionVerdict] = Field(
        min_length=1, max_length=MAX_INSPECTION_ITEMS
    )


type CapabilityRequest = Annotated[
    ModelElicitationRequest | ResultInspectionRequest,
    Field(discriminator="kind"),
]
type CapabilityResponse = Annotated[
    ModelElicitationResponse | ResultInspectionResponse,
    Field(discriminator="kind"),
]


class DiscoveryExchange(_Strict):
    """One coordinator response file for exactly one paused checkpoint."""

    schema_version: int
    run_id: str = Field(min_length=1, max_length=64)
    checkpoint_revision: int = Field(ge=1)
    responses: list[CapabilityResponse] = Field(min_length=1, max_length=64)


class ElicitationRecord(_Strict):
    prompt_digest: str = Field(min_length=64, max_length=64)
    output: str = Field(min_length=1, max_length=MAX_ELICITATION_OUTPUT_CHARS)


class InspectionRecord(_Strict):
    item_digest: str = Field(min_length=64, max_length=64)
    classification: ResultClassification
    reason: str = Field(min_length=1, max_length=300)


class _SearchResultModel(_Strict):
    position: int
    url: str
    title: str
    snippet: str | None = None


class _InspectionModel(_Strict):
    position: int
    classification: ResultClassification
    reason: str


class _AssessmentModel(_Strict):
    provider: str
    channel: str
    query: str
    retrieval_mode: RetrievalMode
    response_format: str
    locale: str
    observed_at: str
    http_status: int | None
    failure_stage: RetrievalFailureStage | None
    assessment: Assessment
    observation: str
    results: list[_SearchResultModel]
    inspections: list[_InspectionModel]
    page_number: int
    pagination_state: PaginationState | None
    pagination_stop_reason: PaginationStopReason | None


class ExecutionRecord(_Strict):
    key: str
    records: list[_AssessmentModel]


class _TermModel(_Strict):
    normalized: str
    kind: str
    wordings: list[str]
    source_urls: list[str]
    source_fields: list[str]


class _InventoryURLModel(_Strict):
    url: str
    kinds: list[str]
    resource_ids: list[str]
    source_fields: list[str]


class _GapModel(_Strict):
    url: str
    reason: str


class VocabularyState(_Strict):
    """Normalized vocabulary phase outcome retained for safe resumption."""

    terms: list[_TermModel]
    urls: list[_InventoryURLModel]
    gaps: list[_GapModel]
    fetched_pages: int
    fetched_bytes: int
    classifier_gap: str | None = None
    revision: int | None = None
    refreshed_sources: int = 0
    reused_sources: int = 0
    new_terms: int = 0
    reused_decisions: int = 0
    inactive_associations: int = 0
    access_gaps: int = 0


class RunParameters(_Strict):
    """Immutable run scope and bounds that a resume must never change."""

    category: Literal["corpus", "tool", "dictionary"]
    stage: str
    qualifiers: list[str]
    max_mined_terms: int
    max_exclusion_groups: int
    run_on: str

    def digest(self) -> str:
        """Return a canonical digest of the immutable run parameters."""

        return _digest(
            json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        )


class DiscoveryCheckpoint(_Strict):
    """Ephemeral operational state for one resumable discovery run."""

    schema_version: int = CHECKPOINT_SCHEMA_VERSION
    run_id: str = Field(min_length=1, max_length=64)
    revision: int = Field(ge=0)
    parameters_digest: str = Field(min_length=64, max_length=64)
    parameters: RunParameters
    vocabulary: VocabularyState | None = None
    elicitations: list[ElicitationRecord] = Field(default_factory=list)
    inspections: list[InspectionRecord] = Field(default_factory=list)
    executions: list[ExecutionRecord] = Field(default_factory=list)
    pending: list[CapabilityRequest] = Field(default_factory=list)
    consumed_request_ids: list[str] = Field(default_factory=list)


def prompt_digest(prompt: str) -> str:
    """Return the stable digest identifying one exact elicitation prompt."""

    return _digest(prompt)


def item_digest(url: str, title: str, snippet: str | None) -> str:
    """Return the stable digest identifying one normalized result item."""

    return _digest("\x1f".join((url, title, snippet or "")))


def request_id(run_id: str, kind: str, discriminator: str) -> str:
    """Return one deterministic request identity for a run step."""

    return f"{run_id}:{kind}:{_digest(discriminator)[:16]}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def encode_records(
    records: tuple[SearchAssessmentRecord, ...],
) -> list[_AssessmentModel]:
    """Serialize normalized assessment records without any raw response body."""

    return [
        _AssessmentModel(
            provider=record.provider.value,
            channel=record.channel,
            query=record.query,
            retrieval_mode=record.retrieval_mode,
            response_format=record.response_format.value,
            locale=record.locale,
            observed_at=record.observed_at.isoformat(),
            http_status=record.http_status,
            failure_stage=record.failure_stage,
            assessment=record.assessment,
            observation=record.observation,
            results=[
                _SearchResultModel(
                    position=result.position,
                    url=result.url,
                    title=result.title,
                    snippet=result.snippet,
                )
                for result in record.results
            ],
            inspections=[
                _InspectionModel(
                    position=inspection.position,
                    classification=inspection.classification,
                    reason=inspection.reason,
                )
                for inspection in record.inspections
            ],
            page_number=record.page_number,
            pagination_state=record.pagination_state,
            pagination_stop_reason=record.pagination_stop_reason,
        )
        for record in records
    ]


def decode_records(
    models: list[_AssessmentModel],
) -> tuple[SearchAssessmentRecord, ...]:
    """Rebuild assessment records exactly as the deterministic phase produced."""

    return tuple(
        SearchAssessmentRecord(
            provider=SearchProvider(model.provider),
            channel=model.channel,
            query=model.query,
            retrieval_mode=model.retrieval_mode,
            response_format=ResponseFormat(model.response_format),
            locale=model.locale,
            observed_at=datetime.fromisoformat(model.observed_at),
            http_status=model.http_status,
            failure_stage=model.failure_stage,
            assessment=model.assessment,
            observation=model.observation,
            results=tuple(
                SearchResult(
                    position=result.position,
                    url=result.url,
                    title=result.title,
                    snippet=result.snippet,
                )
                for result in model.results
            ),
            inspections=tuple(
                ResultInspection(
                    position=inspection.position,
                    classification=inspection.classification,
                    reason=inspection.reason,
                )
                for inspection in model.inspections
            ),
            page_number=model.page_number,
            pagination_state=model.pagination_state,
            pagination_stop_reason=model.pagination_stop_reason,
        )
        for model in models
    )


def encode_vocabulary(vocabulary: InventoryVocabulary) -> VocabularyState:
    """Serialize the normalized vocabulary phase outcome."""

    return VocabularyState(
        terms=[
            _TermModel(
                normalized=term.normalized,
                kind=term.kind.value,
                wordings=list(term.wordings),
                source_urls=list(term.source_urls),
                source_fields=list(term.source_fields),
            )
            for term in vocabulary.terms
        ],
        urls=[
            _InventoryURLModel(
                url=entry.url,
                kinds=[kind.value for kind in entry.kinds],
                resource_ids=list(entry.resource_ids),
                source_fields=list(entry.source_fields),
            )
            for entry in vocabulary.urls
        ],
        gaps=[_GapModel(url=gap.url, reason=gap.reason) for gap in vocabulary.gaps],
        fetched_pages=vocabulary.fetched_pages,
        fetched_bytes=vocabulary.fetched_bytes,
        classifier_gap=vocabulary.classifier_gap,
    )


def decode_vocabulary(state: VocabularyState) -> InventoryVocabulary:
    """Rebuild the vocabulary phase outcome without repeating retrieval."""

    return InventoryVocabulary(
        terms=tuple(
            VocabularyTerm(
                normalized=term.normalized,
                kind=VocabularyKind(term.kind),
                wordings=tuple(term.wordings),
                source_urls=tuple(term.source_urls),
                source_fields=tuple(term.source_fields),
            )
            for term in state.terms
        ),
        urls=tuple(
            InventoryURL(
                url=entry.url,
                kinds=tuple(URLKind(kind) for kind in entry.kinds),
                resource_ids=tuple(entry.resource_ids),
                source_fields=tuple(entry.source_fields),
            )
            for entry in state.urls
        ),
        gaps=tuple(MiningGap(url=gap.url, reason=gap.reason) for gap in state.gaps),
        fetched_pages=state.fetched_pages,
        fetched_bytes=state.fetched_bytes,
        classifier_gap=state.classifier_gap,
    )


def validate_operational_path(path: Path, *, option: str) -> Path:
    """Require one explicit, local, absolute path outside the repository."""

    if not path.is_absolute():
        raise DiscoveryProtocolError(f"{option} must be an absolute path")
    if Path(os.path.normpath(path)) != path:
        raise DiscoveryProtocolError(f"{option} must not contain relative segments")
    if path.is_symlink():
        raise DiscoveryProtocolError(f"{option} must not be a symbolic link")
    if path.exists() and not path.is_file():
        raise DiscoveryProtocolError(f"{option} must be a regular file")
    parent = path.parent
    if not parent.is_dir():
        raise DiscoveryProtocolError(f"{option} directory does not exist")
    repository = Path.cwd().resolve()
    resolved = parent.resolve() / path.name
    if resolved == repository or repository in resolved.parents:
        raise DiscoveryProtocolError(f"{option} must be outside the repository")
    return path


def write_checkpoint(path: Path, checkpoint: DiscoveryCheckpoint) -> None:
    """Write the checkpoint atomically and durably with user-only permissions."""

    payload = json.dumps(
        checkpoint.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(payload) > MAX_CHECKPOINT_BYTES:
        raise DiscoveryProtocolError("discovery checkpoint exceeds the size limit")
    temporary = write_durable_temporary(path, payload, prefix=".histgerm-", mode=0o600)
    replace_atomically(temporary, path)


def read_checkpoint(path: Path) -> DiscoveryCheckpoint:
    """Read and validate one bounded UTF-8 JSON checkpoint."""

    checkpoint = DiscoveryCheckpoint.model_validate(
        _read_json(path, MAX_CHECKPOINT_BYTES, "checkpoint")
    )
    if checkpoint.schema_version != CHECKPOINT_SCHEMA_VERSION:
        raise DiscoveryProtocolError(
            f"checkpoint schema version {checkpoint.schema_version} is unsupported; "
            f"start a new run with schema version {CHECKPOINT_SCHEMA_VERSION}"
        )
    return checkpoint


def read_exchange(path: Path) -> DiscoveryExchange:
    """Read and validate one bounded UTF-8 JSON response file."""

    exchange = DiscoveryExchange.model_validate(
        _read_json(path, MAX_EXCHANGE_BYTES, "response")
    )
    if exchange.schema_version != CHECKPOINT_SCHEMA_VERSION:
        raise DiscoveryProtocolError(
            f"response schema version {exchange.schema_version} is unsupported"
        )
    return exchange


def remove_operational_file(path: Path | None) -> None:
    """Delete one temporary protocol file without failing on absence."""

    if path is not None:
        path.unlink(missing_ok=True)


def _read_json(path: Path, limit: int, label: str) -> Any:
    try:
        size = path.stat().st_size
    except OSError as error:
        raise DiscoveryProtocolError(f"{label} file is unavailable: {error}") from error
    if size > limit:
        raise DiscoveryProtocolError(f"{label} file exceeds the size limit")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise DiscoveryProtocolError(f"{label} file must be UTF-8 JSON") from error
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise DiscoveryProtocolError(f"{label} file must be valid JSON") from error
    if not isinstance(value, dict):
        raise DiscoveryProtocolError(f"{label} file must contain one JSON object")
    return value


__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "CapabilityRequest",
    "CapabilityResponse",
    "DiscoveryCheckpoint",
    "DiscoveryExchange",
    "DiscoveryProtocolError",
    "ElicitationRecord",
    "ExecutionRecord",
    "InspectionItem",
    "InspectionRecord",
    "InspectionVerdict",
    "ModelElicitationRequest",
    "ModelElicitationResponse",
    "ResultInspectionRequest",
    "ResultInspectionResponse",
    "RunParameters",
    "StaleCheckpointError",
    "VocabularyState",
    "read_checkpoint",
    "read_exchange",
    "remove_operational_file",
    "validate_operational_path",
    "write_checkpoint",
]
