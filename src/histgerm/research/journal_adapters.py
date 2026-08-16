"""Deterministic journal adapters: dual-write projection and parity replay.

``TASK-MIG-008`` introduces a journal-derived execution path that runs beside the
authoritative old exchange without changing any live result. This module owns the
two deterministic adapters that make parity provable:

* :func:`discovery_run_events` maps one authoritative :class:`DiscoveryRunResult`
  to an ordered, append-only run-journal event stream (the dual-write side). The
  mapping is a pure function of the confirmed result, so it never repeats a
  retrieval and never mutates live state.
* :func:`synthetic_from_result` and :func:`synthetic_from_events` reduce the
  run result and the journal event stream, respectively, into the same
  :class:`SyntheticDiscoveryResult` projection.

Because both reductions are symmetric, ``synthetic_from_result(...)`` and
``synthetic_from_events(discovery_run_events(...))`` are equal by construction. A
parity test that finds them unequal is a hard failure, never a runtime fallback:
the journal must capture the run's semantic content exactly, or the test fails.

The durable helper :func:`append_discovery_journal` reuses the ``TASK-MIG-007``
store so the dual-write path inherits its atomic, interruption-safe append,
optimistic-concurrency sequencing, and idempotent restart-resume.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from .discovery_orchestration import DiscoveryConfig, DiscoveryRunResult
from .discovery_protocol import RunParameters
from .journal_store import AppendResult, append_event, compact_journal
from .run_journal import (
    AnyJournalEvent,
    JournalReplay,
    LeadFoundEvent,
    LeadFoundPayload,
    ModelResponseInvalidEvent,
    ModelResponseInvalidPayload,
    ProviderGapEvent,
    ProviderGapPayload,
    QueryExecutedEvent,
    QueryExecutedPayload,
    QueryPlannedEvent,
    QueryPlannedPayload,
    RetryScheduledEvent,
    RetryScheduledPayload,
    RunCompletedEvent,
    RunCompletedPayload,
    RunStartedEvent,
    RunStartedPayload,
    replay_journal,
)

_GAP_ASSESSMENTS = frozenset({"access_gap", "transport_error"})
_RETRY_REASON = "model response formatting retry"


@dataclass(frozen=True, slots=True)
class SyntheticPlannedQuery:
    """One planned provider query recovered from either the run or the journal."""

    provider: str
    channel: str
    query: str
    language: str


@dataclass(frozen=True, slots=True)
class SyntheticExecutedQuery:
    """The bounded outcome of one executed, non-gap provider query."""

    provider: str
    channel: str
    query: str
    result_count: int
    lead_count: int
    http_status: int | None
    assessment: str | None


@dataclass(frozen=True, slots=True)
class SyntheticProviderGap:
    """One structured, body-less provider transport or access gap."""

    provider: str
    channel: str
    reason: str
    failure_stage: str | None


@dataclass(frozen=True, slots=True)
class SyntheticLead:
    """One untrusted candidate lead retained by the run, with channel context."""

    name: str
    url: str
    category: str
    channel: str
    position: int


@dataclass(frozen=True, slots=True)
class SyntheticModelResponseInvalid:
    """One quarantined or malformed model elicitation response."""

    iteration: int
    scope: str
    reason: str
    position: int | None


@dataclass(frozen=True, slots=True)
class SyntheticDiscoveryResult:
    """The semantic projection shared by the old path and the journal replay.

    Two projections compare equal iff the journal captured the run's confirmed
    scope, planned and executed queries, provider gaps, retained leads, invalid
    model responses, retry count, and terminal status. Fields the journal does
    not carry (vocabulary internals, pagination, completion-gap prose) are
    intentionally excluded so parity never depends on unrecorded information.
    """

    run_id: str
    category: str
    stage: str
    run_on: str
    parameters_digest: str
    planned_queries: tuple[SyntheticPlannedQuery, ...]
    executed_queries: tuple[SyntheticExecutedQuery, ...]
    provider_gaps: tuple[SyntheticProviderGap, ...]
    leads: tuple[SyntheticLead, ...]
    invalid_model_responses: tuple[SyntheticModelResponseInvalid, ...]
    retries: int
    status: str

    def as_json(self) -> dict[str, object]:
        """Return a deterministic JSON-compatible projection."""

        return {
            "run_id": self.run_id,
            "category": self.category,
            "stage": self.stage,
            "run_on": self.run_on,
            "parameters_digest": self.parameters_digest,
            "planned_queries": [
                {
                    "provider": query.provider,
                    "channel": query.channel,
                    "query": query.query,
                    "language": query.language,
                }
                for query in self.planned_queries
            ],
            "executed_queries": [
                {
                    "provider": query.provider,
                    "channel": query.channel,
                    "query": query.query,
                    "result_count": query.result_count,
                    "lead_count": query.lead_count,
                    "http_status": query.http_status,
                    "assessment": query.assessment,
                }
                for query in self.executed_queries
            ],
            "provider_gaps": [
                {
                    "provider": gap.provider,
                    "channel": gap.channel,
                    "reason": gap.reason,
                    "failure_stage": gap.failure_stage,
                }
                for gap in self.provider_gaps
            ],
            "leads": [
                {
                    "name": lead.name,
                    "url": lead.url,
                    "category": lead.category,
                    "channel": lead.channel,
                    "position": lead.position,
                }
                for lead in self.leads
            ],
            "invalid_model_responses": [
                {
                    "iteration": invalid.iteration,
                    "scope": invalid.scope,
                    "reason": invalid.reason,
                    "position": invalid.position,
                }
                for invalid in self.invalid_model_responses
            ],
            "retries": self.retries,
            "status": self.status,
        }


def _run_on(config: DiscoveryConfig) -> str:
    return (config.run_on or date.today()).isoformat()


def _parameters_digest(config: DiscoveryConfig, run_on: str) -> str:
    return RunParameters(
        category=config.category,
        stage=config.stage.value,
        qualifiers=list(config.qualifiers),
        max_mined_terms=config.max_mined_terms,
        max_exclusion_groups=config.max_exclusion_groups,
        run_on=run_on,
    ).digest()


def _language(locale: str) -> Literal["de", "en"]:
    return "de" if locale.startswith("de") else "en"


def _clamped_status(http_status: int | None) -> int | None:
    if http_status is None or not 100 <= http_status <= 599:
        return None
    return http_status


def discovery_run_events(
    config: DiscoveryConfig,
    result: DiscoveryRunResult,
    *,
    run_id: str,
    recorded_at: str | None = None,
) -> tuple[AnyJournalEvent, ...]:
    """Project one authoritative run result into an ordered journal stream.

    The stream is deterministic and append-only: ``run_started`` at sequence 0,
    then the elicitation retries and quarantines, one ``query_planned`` plus a
    ``query_executed`` or ``provider_gap`` per assessment, one ``lead_found`` per
    retained lead, and a terminal ``run_completed``. Sequence numbers are
    gap-free, so the stream validates against the ``TASK-MIG-007`` store.
    """

    run_on = _run_on(config)
    run_at = recorded_at if recorded_at is not None else f"{run_on}T00:00:00+00:00"
    events: list[AnyJournalEvent] = [
        RunStartedEvent(
            run_id=run_id,
            sequence=0,
            recorded_at=run_at,
            payload=RunStartedPayload(
                category=config.category,
                stage=config.stage.value,
                qualifiers=list(config.qualifiers),
                parameters_digest=_parameters_digest(config, run_on),
                run_on=run_on,
            ),
        )
    ]
    for index in range(result.elicitation.metrics.retries_attempted):
        events.append(
            RetryScheduledEvent(
                run_id=run_id,
                sequence=len(events),
                recorded_at=run_at,
                payload=RetryScheduledPayload(
                    iteration=index + 1, reason=_RETRY_REASON
                ),
            )
        )
    for quarantine in result.elicitation.quarantines:
        events.append(
            ModelResponseInvalidEvent(
                run_id=run_id,
                sequence=len(events),
                recorded_at=run_at,
                payload=ModelResponseInvalidPayload(
                    iteration=quarantine.iteration,
                    scope=quarantine.scope,
                    reason=quarantine.reason,
                    position=quarantine.position,
                ),
            )
        )
    for record in result.assessments:
        at = record.observed_at.isoformat()
        events.append(
            QueryPlannedEvent(
                run_id=run_id,
                sequence=len(events),
                recorded_at=at,
                payload=QueryPlannedPayload(
                    provider=record.provider.value,
                    channel=record.channel,
                    language=_language(record.locale),
                    query=record.query,
                    intent_id=None,
                ),
            )
        )
        if record.assessment in _GAP_ASSESSMENTS:
            events.append(
                ProviderGapEvent(
                    run_id=run_id,
                    sequence=len(events),
                    recorded_at=at,
                    payload=ProviderGapPayload(
                        provider=record.provider.value,
                        channel=record.channel,
                        reason=record.assessment,
                        failure_stage=(
                            str(record.failure_stage)
                            if record.failure_stage is not None
                            else None
                        ),
                    ),
                )
            )
        else:
            lead_count = sum(
                1
                for inspection in record.inspections
                if inspection.classification == "lead"
            )
            events.append(
                QueryExecutedEvent(
                    run_id=run_id,
                    sequence=len(events),
                    recorded_at=at,
                    payload=QueryExecutedPayload(
                        provider=record.provider.value,
                        channel=record.channel,
                        query=record.query,
                        result_count=len(record.results),
                        lead_count=lead_count,
                        http_status=_clamped_status(record.http_status),
                        assessment=record.assessment,
                    ),
                )
            )
    leads = result.leads_with_context()
    for name, url, channel, position in leads:
        events.append(
            LeadFoundEvent(
                run_id=run_id,
                sequence=len(events),
                recorded_at=run_at,
                payload=LeadFoundPayload(
                    name=name,
                    url=url,
                    category=result.category,
                    channel=channel,
                    position=position,
                ),
            )
        )
    events.append(
        RunCompletedEvent(
            run_id=run_id,
            sequence=len(events),
            recorded_at=run_at,
            payload=RunCompletedPayload(
                status="complete" if result.complete else "aborted",
                leads=len(leads),
                candidates=0,
                blocked=0,
            ),
        )
    )
    return tuple(events)


def synthetic_from_result(
    config: DiscoveryConfig,
    result: DiscoveryRunResult,
    *,
    run_id: str,
) -> SyntheticDiscoveryResult:
    """Reduce an authoritative run result into the parity projection."""

    run_on = _run_on(config)
    planned: list[SyntheticPlannedQuery] = []
    executed: list[SyntheticExecutedQuery] = []
    gaps: list[SyntheticProviderGap] = []
    for record in result.assessments:
        planned.append(
            SyntheticPlannedQuery(
                provider=record.provider.value,
                channel=record.channel,
                query=record.query,
                language=_language(record.locale),
            )
        )
        if record.assessment in _GAP_ASSESSMENTS:
            gaps.append(
                SyntheticProviderGap(
                    provider=record.provider.value,
                    channel=record.channel,
                    reason=record.assessment,
                    failure_stage=(
                        str(record.failure_stage)
                        if record.failure_stage is not None
                        else None
                    ),
                )
            )
            continue
        lead_count = sum(
            1
            for inspection in record.inspections
            if inspection.classification == "lead"
        )
        executed.append(
            SyntheticExecutedQuery(
                provider=record.provider.value,
                channel=record.channel,
                query=record.query,
                result_count=len(record.results),
                lead_count=lead_count,
                http_status=_clamped_status(record.http_status),
                assessment=record.assessment,
            )
        )
    leads = tuple(
        SyntheticLead(
            name=name,
            url=url,
            category=result.category,
            channel=channel,
            position=position,
        )
        for name, url, channel, position in result.leads_with_context()
    )
    invalid = tuple(
        SyntheticModelResponseInvalid(
            iteration=quarantine.iteration,
            scope=quarantine.scope,
            reason=quarantine.reason,
            position=quarantine.position,
        )
        for quarantine in result.elicitation.quarantines
    )
    return SyntheticDiscoveryResult(
        run_id=run_id,
        category=config.category,
        stage=config.stage.value,
        run_on=run_on,
        parameters_digest=_parameters_digest(config, run_on),
        planned_queries=tuple(planned),
        executed_queries=tuple(executed),
        provider_gaps=tuple(gaps),
        leads=leads,
        invalid_model_responses=invalid,
        retries=result.elicitation.metrics.retries_attempted,
        status="complete" if result.complete else "aborted",
    )


def synthetic_from_events(
    events: Sequence[AnyJournalEvent],
) -> SyntheticDiscoveryResult:
    """Reduce a journal event stream into the parity projection.

    This is the journal-derived execution path: it reconstructs the proposed
    run projection purely from recorded events, performing no retrieval and
    reading no live state. Integrity events (``checkpoint``) and downstream
    coordinator events are ignored so a compacted journal still reduces to the
    same projection.
    """

    run_id: str | None = None
    category: str | None = None
    stage: str | None = None
    run_on: str | None = None
    parameters_digest: str | None = None
    planned: list[SyntheticPlannedQuery] = []
    executed: list[SyntheticExecutedQuery] = []
    gaps: list[SyntheticProviderGap] = []
    leads: list[SyntheticLead] = []
    invalid: list[SyntheticModelResponseInvalid] = []
    retries = 0
    status: str | None = None
    for event in events:
        if isinstance(event, RunStartedEvent):
            run_id = event.run_id
            category = event.payload.category
            stage = event.payload.stage
            run_on = event.payload.run_on
            parameters_digest = event.payload.parameters_digest
        elif isinstance(event, QueryPlannedEvent):
            planned.append(
                SyntheticPlannedQuery(
                    provider=event.payload.provider,
                    channel=event.payload.channel,
                    query=event.payload.query,
                    language=event.payload.language,
                )
            )
        elif isinstance(event, QueryExecutedEvent):
            executed.append(
                SyntheticExecutedQuery(
                    provider=event.payload.provider,
                    channel=event.payload.channel,
                    query=event.payload.query,
                    result_count=event.payload.result_count,
                    lead_count=event.payload.lead_count,
                    http_status=event.payload.http_status,
                    assessment=event.payload.assessment,
                )
            )
        elif isinstance(event, ProviderGapEvent):
            gaps.append(
                SyntheticProviderGap(
                    provider=event.payload.provider,
                    channel=event.payload.channel,
                    reason=event.payload.reason,
                    failure_stage=event.payload.failure_stage,
                )
            )
        elif isinstance(event, LeadFoundEvent):
            leads.append(
                SyntheticLead(
                    name=event.payload.name,
                    url=event.payload.url,
                    category=event.payload.category,
                    channel=event.payload.channel,
                    position=event.payload.position,
                )
            )
        elif isinstance(event, ModelResponseInvalidEvent):
            invalid.append(
                SyntheticModelResponseInvalid(
                    iteration=event.payload.iteration,
                    scope=event.payload.scope,
                    reason=event.payload.reason,
                    position=event.payload.position,
                )
            )
        elif isinstance(event, RetryScheduledEvent):
            retries += 1
        elif isinstance(event, RunCompletedEvent):
            status = event.payload.status
    if (
        run_id is None
        or category is None
        or stage is None
        or run_on is None
        or parameters_digest is None
        or status is None
    ):
        raise ValueError("journal is missing a run_started or run_completed event")
    return SyntheticDiscoveryResult(
        run_id=run_id,
        category=category,
        stage=stage,
        run_on=run_on,
        parameters_digest=parameters_digest,
        planned_queries=tuple(planned),
        executed_queries=tuple(executed),
        provider_gaps=tuple(gaps),
        leads=tuple(leads),
        invalid_model_responses=tuple(invalid),
        retries=retries,
        status=status,
    )


def replay_run(events: Sequence[AnyJournalEvent]) -> JournalReplay:
    """Return the deterministic journal replay for an event stream."""

    return replay_journal(events)


def append_discovery_journal(
    path: Path,
    events: Sequence[AnyJournalEvent],
    *,
    checkpoint: bool = False,
) -> tuple[AppendResult, ...]:
    """Durably append a run stream, idempotent and safe to resume after a fault.

    Each event is appended through the ``TASK-MIG-007`` store, which makes every
    write atomic and interruption-safe and enforces sequence ordering. Because
    append is idempotent on an identical ``(run_id, sequence)``, re-running this
    helper after an interrupted write replays the already-committed prefix as
    no-ops and continues, never repeating a retrieval. When ``checkpoint`` is
    set, a verifiable compaction checkpoint is appended after the run stream.
    """

    results: list[AppendResult] = []
    for event in events:
        results.append(append_event(path, event))
    if checkpoint:
        results.append(compact_journal(path))
    return tuple(results)


__all__ = [
    "SyntheticDiscoveryResult",
    "SyntheticExecutedQuery",
    "SyntheticLead",
    "SyntheticModelResponseInvalid",
    "SyntheticPlannedQuery",
    "SyntheticProviderGap",
    "append_discovery_journal",
    "discovery_run_events",
    "replay_run",
    "synthetic_from_events",
    "synthetic_from_result",
]
