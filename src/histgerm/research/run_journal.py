"""Typed, append-only run-journal event schema and deterministic replay.

The run journal is a fixed, versioned, append-only sequence of typed events for
one discovery run. Each event carries the same envelope fields
(``schema_version``, ``run_id``, ``sequence``, ``recorded_at``, ``kind``) and a
discriminated ``payload`` whose shape is fixed by ``kind``. This module owns the
event model, the canonical byte-stable line encoding, the journal content hash,
the generated JSON Schema artifact, and the deterministic replay reduction. The
durable store (:mod:`histgerm.research.journal_store`) owns file I/O, locking,
idempotent append, and integrity enforcement.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter

from .models import CandidateDisposition, ResourceCategory, SearchLanguage

JOURNAL_SCHEMA_VERSION = 1

JOURNAL_EVENT_KINDS: tuple[str, ...] = (
    "run_started",
    "query_planned",
    "query_executed",
    "provider_gap",
    "lead_found",
    "model_response_invalid",
    "retry_scheduled",
    "candidate_blocked",
    "candidate_researched",
    "ledger_revision_observed",
    "ledger_mutation_proposed",
    "checkpoint",
    "run_completed",
)


def _valid_iso_datetime(value: str) -> str:
    try:
        datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("recorded_at must be an ISO 8601 datetime") from error
    return value


def _valid_iso_date(value: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("run_on must be an ISO 8601 date") from error
    return value


IsoDatetime = Annotated[
    str, Field(min_length=1, max_length=64), AfterValidator(_valid_iso_datetime)
]
IsoDate = Annotated[
    str, Field(min_length=8, max_length=10), AfterValidator(_valid_iso_date)
]
RunId = Annotated[str, Field(min_length=1, max_length=64)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=200)]
ShortText = Annotated[str, Field(min_length=1, max_length=300)]
Query = Annotated[str, Field(min_length=1, max_length=2000)]
Url = Annotated[str, Field(min_length=1, max_length=2000)]
Name = Annotated[str, Field(min_length=1, max_length=1000)]
Channel = Annotated[str, Field(min_length=1, max_length=64)]
Provider = Annotated[str, Field(min_length=1, max_length=64)]
Stage = Annotated[str, Field(min_length=1, max_length=32)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RunStartedPayload(_Strict):
    """Immutable run scope and bounds captured when a run begins."""

    category: ResourceCategory
    stage: Stage
    qualifiers: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=16
    )
    parameters_digest: Digest
    run_on: IsoDate


class QueryPlannedPayload(_Strict):
    """One planned search query for a provider channel."""

    provider: Provider
    channel: Channel
    language: SearchLanguage
    query: Query
    intent_id: Identifier | None = None


class QueryExecutedPayload(_Strict):
    """The bounded outcome of executing one planned query."""

    provider: Provider
    channel: Channel
    query: Query
    result_count: int = Field(ge=0)
    lead_count: int = Field(default=0, ge=0)
    http_status: int | None = Field(default=None, ge=100, le=599)
    assessment: ShortText | None = None


class ProviderGapPayload(_Strict):
    """A structured, body-less provider transport or access gap."""

    provider: Provider
    channel: Channel
    reason: ShortText
    failure_stage: ShortText | None = None


class LeadFoundPayload(_Strict):
    """One untrusted candidate lead surfaced by inspection."""

    name: Name
    url: Url
    category: ResourceCategory
    channel: Channel
    position: int = Field(ge=1)


class ModelResponseInvalidPayload(_Strict):
    """A malformed or quarantined model elicitation response."""

    iteration: int = Field(ge=1)
    scope: Literal["candidate", "response"]
    reason: ShortText
    position: int | None = Field(default=None, ge=1)


class RetryScheduledPayload(_Strict):
    """A bounded, deterministic retry of a recoverable failure."""

    iteration: int = Field(ge=1)
    reason: ShortText


class CandidateBlockedPayload(_Strict):
    """A candidate blocked by an evidence gap rather than discarded."""

    candidate_id: Identifier
    name: Name
    evidence_gaps: list[ShortText] = Field(default_factory=list, max_length=32)


class CandidateResearchedPayload(_Strict):
    """A validated worker disposition proposed for one candidate."""

    candidate_id: Identifier
    disposition: CandidateDisposition
    resource_id: Identifier | None = None


class LedgerRevisionObservedPayload(_Strict):
    """The ledger revision observed before a proposed mutation."""

    revision: int = Field(ge=0)


class LedgerMutationProposedPayload(_Strict):
    """A proposed optimistic-concurrency ledger mutation."""

    operation: Literal["record-search", "upsert-candidate", "apply-result"]
    target_id: Identifier
    expected_revision: int = Field(ge=0)


class CheckpointPayload(_Strict):
    """A compact snapshot retaining the prior journal hash and last sequence."""

    content_hash: Digest
    last_sequence: int = Field(ge=0)


class RunCompletedPayload(_Strict):
    """The terminal, deterministic outcome summary of one run."""

    status: Literal["complete", "aborted"]
    leads: int = Field(default=0, ge=0)
    candidates: int = Field(default=0, ge=0)
    blocked: int = Field(default=0, ge=0)


class _EventBase(_Strict):
    schema_version: int = JOURNAL_SCHEMA_VERSION
    run_id: RunId
    sequence: int = Field(ge=0)
    recorded_at: IsoDatetime


class RunStartedEvent(_EventBase):
    kind: Literal["run_started"] = "run_started"
    payload: RunStartedPayload


class QueryPlannedEvent(_EventBase):
    kind: Literal["query_planned"] = "query_planned"
    payload: QueryPlannedPayload


class QueryExecutedEvent(_EventBase):
    kind: Literal["query_executed"] = "query_executed"
    payload: QueryExecutedPayload


class ProviderGapEvent(_EventBase):
    kind: Literal["provider_gap"] = "provider_gap"
    payload: ProviderGapPayload


class LeadFoundEvent(_EventBase):
    kind: Literal["lead_found"] = "lead_found"
    payload: LeadFoundPayload


class ModelResponseInvalidEvent(_EventBase):
    kind: Literal["model_response_invalid"] = "model_response_invalid"
    payload: ModelResponseInvalidPayload


class RetryScheduledEvent(_EventBase):
    kind: Literal["retry_scheduled"] = "retry_scheduled"
    payload: RetryScheduledPayload


class CandidateBlockedEvent(_EventBase):
    kind: Literal["candidate_blocked"] = "candidate_blocked"
    payload: CandidateBlockedPayload


class CandidateResearchedEvent(_EventBase):
    kind: Literal["candidate_researched"] = "candidate_researched"
    payload: CandidateResearchedPayload


class LedgerRevisionObservedEvent(_EventBase):
    kind: Literal["ledger_revision_observed"] = "ledger_revision_observed"
    payload: LedgerRevisionObservedPayload


class LedgerMutationProposedEvent(_EventBase):
    kind: Literal["ledger_mutation_proposed"] = "ledger_mutation_proposed"
    payload: LedgerMutationProposedPayload


class CheckpointEvent(_EventBase):
    kind: Literal["checkpoint"] = "checkpoint"
    payload: CheckpointPayload


class RunCompletedEvent(_EventBase):
    kind: Literal["run_completed"] = "run_completed"
    payload: RunCompletedPayload


AnyJournalEvent = (
    RunStartedEvent
    | QueryPlannedEvent
    | QueryExecutedEvent
    | ProviderGapEvent
    | LeadFoundEvent
    | ModelResponseInvalidEvent
    | RetryScheduledEvent
    | CandidateBlockedEvent
    | CandidateResearchedEvent
    | LedgerRevisionObservedEvent
    | LedgerMutationProposedEvent
    | CheckpointEvent
    | RunCompletedEvent
)

RunJournalEvent = Annotated[AnyJournalEvent, Field(discriminator="kind")]

EVENT_ADAPTER: TypeAdapter[AnyJournalEvent] = TypeAdapter(RunJournalEvent)


def parse_event(mapping: Mapping[str, Any]) -> AnyJournalEvent:
    """Validate one JSON object into exactly one typed journal event."""

    return EVENT_ADAPTER.validate_python(dict(mapping), strict=True)


def encode_event(event: AnyJournalEvent) -> str:
    """Return the canonical, byte-stable single-line JSON for one event."""

    return json.dumps(
        event.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def encode_events(events: Sequence[AnyJournalEvent]) -> bytes:
    """Return the canonical newline-terminated bytes for an event sequence."""

    if not events:
        return b""
    return "".join(f"{encode_event(event)}\n" for event in events).encode("utf-8")


def journal_content_hash(events: Sequence[AnyJournalEvent]) -> str:
    """Return the SHA-256 hash of the canonical bytes of ``events``."""

    return hashlib.sha256(encode_events(events)).hexdigest()


def event_schema() -> dict[str, Any]:
    """Return the generated JSON Schema for the discriminated event union."""

    return EVENT_ADAPTER.json_schema(mode="validation")


def canonical_schema_json() -> str:
    """Return the canonical serialization of the event JSON Schema artifact."""

    return json.dumps(
        event_schema(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def schema_digest() -> str:
    """Return the SHA-256 digest of the canonical event JSON Schema artifact."""

    return hashlib.sha256(canonical_schema_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class JournalReplay:
    """Deterministic reduction of an ordered event sequence into run state."""

    run_id: str | None
    schema_version: int | None
    event_count: int
    last_sequence: int
    content_hash: str
    counts: Mapping[str, int]
    parameters: RunStartedPayload | None
    leads: int
    provider_gaps: int
    invalid_model_responses: int
    retries: int
    blocked_candidates: tuple[str, ...]
    researched: tuple[tuple[str, str], ...]
    last_ledger_revision: int | None
    last_checkpoint: CheckpointPayload | None
    completed: RunCompletedPayload | None

    def as_status(self) -> dict[str, Any]:
        """Return a deterministic JSON-serializable status object."""

        return {
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "event_count": self.event_count,
            "last_sequence": self.last_sequence,
            "content_hash": self.content_hash,
            "counts": dict(self.counts),
            "parameters": (
                self.parameters.model_dump(mode="json")
                if self.parameters is not None
                else None
            ),
            "leads": self.leads,
            "provider_gaps": self.provider_gaps,
            "invalid_model_responses": self.invalid_model_responses,
            "retries": self.retries,
            "blocked_candidates": list(self.blocked_candidates),
            "researched": [
                {"candidate_id": candidate_id, "disposition": disposition}
                for candidate_id, disposition in self.researched
            ],
            "last_ledger_revision": self.last_ledger_revision,
            "last_checkpoint": (
                self.last_checkpoint.model_dump(mode="json")
                if self.last_checkpoint is not None
                else None
            ),
            "completed": (
                self.completed.model_dump(mode="json")
                if self.completed is not None
                else None
            ),
        }


def replay_journal(events: Sequence[AnyJournalEvent]) -> JournalReplay:
    """Deterministically reduce an ordered event sequence into run state.

    Replay is a pure function of the ordered events: identical input always
    yields an identical :class:`JournalReplay`, so a journal read back from disk
    reconstructs exactly the same confirmed run state.
    """

    counts = dict.fromkeys(JOURNAL_EVENT_KINDS, 0)
    parameters: RunStartedPayload | None = None
    leads = 0
    provider_gaps = 0
    invalid_model_responses = 0
    retries = 0
    blocked: dict[str, None] = {}
    researched: dict[str, str] = {}
    last_ledger_revision: int | None = None
    last_checkpoint: CheckpointPayload | None = None
    completed: RunCompletedPayload | None = None
    for event in events:
        counts[event.kind] += 1
        if isinstance(event, RunStartedEvent):
            parameters = event.payload
        elif isinstance(event, LeadFoundEvent):
            leads += 1
        elif isinstance(event, ProviderGapEvent):
            provider_gaps += 1
        elif isinstance(event, ModelResponseInvalidEvent):
            invalid_model_responses += 1
        elif isinstance(event, RetryScheduledEvent):
            retries += 1
        elif isinstance(event, CandidateBlockedEvent):
            blocked[event.payload.candidate_id] = None
        elif isinstance(event, CandidateResearchedEvent):
            researched[event.payload.candidate_id] = event.payload.disposition
        elif isinstance(event, LedgerRevisionObservedEvent):
            last_ledger_revision = event.payload.revision
        elif isinstance(event, CheckpointEvent):
            last_checkpoint = event.payload
        elif isinstance(event, RunCompletedEvent):
            completed = event.payload
    return JournalReplay(
        run_id=events[0].run_id if events else None,
        schema_version=events[0].schema_version if events else None,
        event_count=len(events),
        last_sequence=events[-1].sequence if events else -1,
        content_hash=journal_content_hash(events),
        counts=counts,
        parameters=parameters,
        leads=leads,
        provider_gaps=provider_gaps,
        invalid_model_responses=invalid_model_responses,
        retries=retries,
        blocked_candidates=tuple(blocked),
        researched=tuple(sorted(researched.items())),
        last_ledger_revision=last_ledger_revision,
        last_checkpoint=last_checkpoint,
        completed=completed,
    )


__all__ = [
    "EVENT_ADAPTER",
    "JOURNAL_EVENT_KINDS",
    "JOURNAL_SCHEMA_VERSION",
    "AnyJournalEvent",
    "CandidateBlockedEvent",
    "CandidateBlockedPayload",
    "CandidateResearchedEvent",
    "CandidateResearchedPayload",
    "CheckpointEvent",
    "CheckpointPayload",
    "JournalReplay",
    "LeadFoundEvent",
    "LeadFoundPayload",
    "LedgerMutationProposedEvent",
    "LedgerMutationProposedPayload",
    "LedgerRevisionObservedEvent",
    "LedgerRevisionObservedPayload",
    "ModelResponseInvalidEvent",
    "ModelResponseInvalidPayload",
    "ProviderGapEvent",
    "ProviderGapPayload",
    "QueryExecutedEvent",
    "QueryExecutedPayload",
    "QueryPlannedEvent",
    "QueryPlannedPayload",
    "RetryScheduledEvent",
    "RetryScheduledPayload",
    "RunCompletedEvent",
    "RunCompletedPayload",
    "RunJournalEvent",
    "RunStartedEvent",
    "RunStartedPayload",
    "canonical_schema_json",
    "encode_event",
    "encode_events",
    "event_schema",
    "journal_content_hash",
    "parse_event",
    "replay_journal",
    "schema_digest",
]
