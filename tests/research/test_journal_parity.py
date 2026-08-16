"""TASK-MIG-008/010: journal projection and parity.

These tests prove the journal event stream captures the authoritative discovery
run result exactly, now that ``TASK-MIG-010`` has retired the old capability
exchange and native orchestration owns execution:

* the run-result projection and the journal-replayed projection are semantically
  identical, and journal replay is deterministic and order-sensitive,
* the projection survives a durable round trip through the ``TASK-MIG-007``
  store, including a compacted journal,
* an interrupted append leaves the prior journal valid and the append resumes
  idempotently, and
* the append path inherits optimistic-concurrency and conflicting-duplicate
  handling from the store.

Every mismatch is asserted as a hard failure; there is no runtime fallback.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from histgerm.catalog import load_catalog
from histgerm.models import LanguageStage
from histgerm.research import _persistence
from histgerm.research import journal_adapters as ja
from histgerm.research.discovery_orchestration import (
    DiscoveryConfig,
    DiscoveryDependencies,
    DiscoveryRunResult,
    ProviderResponse,
    run_discovery,
)
from histgerm.research.inventory_vocabulary import FetchedDocument, VocabularyLimits
from histgerm.research.journal_store import (
    JournalConflictError,
    JournalWriteError,
    append_event,
    read_journal,
)
from histgerm.research.run_journal import (
    LeadFoundEvent,
    LeadFoundPayload,
    encode_events,
    replay_journal,
)

RUN_ON = date(2026, 8, 12)
RUN_ID = "run-parity-0001"


# --------------------------------------------------------------------------- #
# Synthetic run builders                                                       #
# --------------------------------------------------------------------------- #
def _config() -> DiscoveryConfig:
    return DiscoveryConfig(
        category="tool",
        stage=LanguageStage.MHG,
        max_mined_terms=0,
        max_exclusion_groups=1,
        run_on=RUN_ON,
        vocabulary=VocabularyLimits(max_pages=1),
    )


def _lead_run() -> tuple[DiscoveryConfig, DiscoveryRunResult]:
    config = _config()

    def provider(request: Any) -> ProviderResponse:
        return ProviderResponse(
            retrieval_mode="bounded_http",
            observed_at=datetime(2026, 8, 12, tzinfo=UTC),
            http_status=200,
            body='<a href="https://example.org/found">Found Tool</a>',
        )

    result = run_discovery(
        config,
        DiscoveryDependencies(
            catalog=load_catalog(),
            model_call=lambda prompt: '{"candidates":[]}',
            vocabulary_transport=lambda url, *, max_bytes: FetchedDocument(
                url, "text/plain", b""
            ),
            provider_fetch=provider,
            result_inspector=lambda result: ("lead", "untrusted lead"),
        ),
    )
    return config, result


def _gap_run() -> tuple[DiscoveryConfig, DiscoveryRunResult]:
    config = DiscoveryConfig(
        category="dictionary",
        stage=LanguageStage.OHG,
        max_mined_terms=0,
        max_exclusion_groups=1,
        run_on=RUN_ON,
        vocabulary=VocabularyLimits(max_pages=1),
    )

    def fail(request: Any) -> ProviderResponse:
        raise OSError("synthetic network gap")

    result = run_discovery(
        config,
        DiscoveryDependencies(
            catalog=load_catalog(),
            model_call=lambda prompt: '{"candidates":[]}',
            vocabulary_transport=lambda url, *, max_bytes: FetchedDocument(
                url, "text/plain", b"Old High German dictionary"
            ),
            provider_fetch=fail,
            result_inspector=lambda result: ("unrelated", "unused"),
        ),
    )
    return config, result


class _MalformedSiblingModel:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, prompt: str, /) -> str:
        self.calls += 1
        if self.calls == 1:
            return (
                '{"candidates":[{"name":"Kept Lead","aliases":["KL"]},'
                '{"name":"Bad Sibling","aliases":[],"rationale":"nope"}]}'
            )
        return '{"candidates":[]}'


def _quarantine_run() -> tuple[DiscoveryConfig, DiscoveryRunResult]:
    config = _config()
    result = run_discovery(
        config,
        DiscoveryDependencies(
            catalog=load_catalog(),
            model_call=_MalformedSiblingModel(),
            vocabulary_transport=lambda url, *, max_bytes: FetchedDocument(
                url, "text/plain", b""
            ),
            provider_fetch=lambda request: ProviderResponse(
                retrieval_mode="bounded_http",
                observed_at=datetime(2026, 8, 12, tzinfo=UTC),
                http_status=200,
                body="<main>No results</main>",
            ),
            result_inspector=lambda result: ("unrelated", "no matching resource"),
        ),
    )
    return config, result


def _small(result: DiscoveryRunResult, count: int = 5) -> DiscoveryRunResult:
    """Truncate a real run to a few assessments for fast durable I/O tests."""

    return replace(result, assessments=result.assessments[:count])


def _journal_path(tmp_path: Path) -> Path:
    return tmp_path / "run.journal.jsonl"


# --------------------------------------------------------------------------- #
# In-memory parity and determinism                                            #
# --------------------------------------------------------------------------- #
def test_old_path_and_journal_replay_projections_are_identical() -> None:
    config, result = _lead_run()
    events = ja.discovery_run_events(config, result, run_id=RUN_ID)

    from_result = ja.synthetic_from_result(config, result, run_id=RUN_ID)
    from_events = ja.synthetic_from_events(events)

    assert from_result == from_events
    assert from_result.as_json() == from_events.as_json()
    assert from_result.leads and from_result.executed_queries
    assert from_result.status == ("complete" if result.complete else "aborted")


def test_journal_replay_is_deterministic_and_order_sensitive() -> None:
    config, result = _lead_run()

    first = ja.discovery_run_events(config, result, run_id=RUN_ID)
    second = ja.discovery_run_events(config, result, run_id=RUN_ID)

    assert encode_events(first) == encode_events(second)
    assert replay_journal(first).content_hash == replay_journal(second).content_hash
    assert ja.synthetic_from_events(first) == ja.synthetic_from_events(second)

    reordered = (first[0], *first[2:], first[1]) if len(first) > 3 else first
    assert replay_journal(reordered).content_hash != replay_journal(first).content_hash


def test_replay_reconstructs_lead_and_query_counts() -> None:
    config, result = _lead_run()
    events = ja.discovery_run_events(config, result, run_id=RUN_ID)
    replay = replay_journal(events)

    projection = ja.synthetic_from_events(events)
    assert replay.leads == len(projection.leads)
    assert replay.leads == len(result.leads_with_context())
    assert replay.completed is not None
    assert replay.completed.status == projection.status
    assert replay.run_id == RUN_ID


# --------------------------------------------------------------------------- #
# Durable round trip through the store                                        #
# --------------------------------------------------------------------------- #
def test_durable_round_trip_matches_old_path(tmp_path: Path) -> None:
    config, full = _lead_run()
    result = _small(full)
    path = _journal_path(tmp_path)
    events = ja.discovery_run_events(config, result, run_id=RUN_ID)

    ja.append_discovery_journal(path, events)
    parsed = read_journal(path)

    assert not parsed.truncated_tail
    assert parsed.events == events
    assert ja.synthetic_from_events(parsed.events) == ja.synthetic_from_result(
        config, result, run_id=RUN_ID
    )


def test_compacted_journal_reduces_to_the_same_projection(tmp_path: Path) -> None:
    config, full = _lead_run()
    result = _small(full)
    path = _journal_path(tmp_path)
    events = ja.discovery_run_events(config, result, run_id=RUN_ID)

    ja.append_discovery_journal(path, events, checkpoint=True)
    parsed = read_journal(path)

    assert parsed.events[-1].kind == "checkpoint"
    assert ja.synthetic_from_events(parsed.events) == ja.synthetic_from_result(
        config, result, run_id=RUN_ID
    )


# --------------------------------------------------------------------------- #
# Interrupted append and idempotent resume                                    #
# --------------------------------------------------------------------------- #
def test_interrupted_append_leaves_prior_journal_valid_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, full = _lead_run()
    result = _small(full, count=6)
    path = _journal_path(tmp_path)
    events = ja.discovery_run_events(config, result, run_id=RUN_ID)
    assert len(events) > 4

    real_replace = _persistence.replace_atomically
    calls = {"n": 0}

    def flaky(source: Path, target: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 4:
            raise OSError("synthetic interrupted rename")
        real_replace(source, target)

    monkeypatch.setattr("histgerm.research.journal_store.replace_atomically", flaky)
    with pytest.raises(JournalWriteError):
        ja.append_discovery_journal(path, events)

    monkeypatch.setattr(
        "histgerm.research.journal_store.replace_atomically", real_replace
    )
    prior = read_journal(path)
    assert not prior.truncated_tail
    assert prior.events == events[:3]

    results = ja.append_discovery_journal(path, events)
    assert [r.idempotent for r in results[:3]] == [True, True, True]
    parsed = read_journal(path)
    assert parsed.events == events
    assert ja.synthetic_from_events(parsed.events) == ja.synthetic_from_result(
        config, result, run_id=RUN_ID
    )


# --------------------------------------------------------------------------- #
# Optimistic concurrency and idempotency of the dual-write                    #
# --------------------------------------------------------------------------- #
def test_dual_write_optimistic_concurrency_and_conflicts(tmp_path: Path) -> None:
    config, full = _lead_run()
    result = _small(full)
    path = _journal_path(tmp_path)
    events = ja.discovery_run_events(config, result, run_id=RUN_ID)

    append_event(path, events[0])

    winner = LeadFoundEvent(
        run_id=RUN_ID,
        sequence=1,
        recorded_at="2026-08-12T00:00:00+00:00",
        payload=LeadFoundPayload(
            name="Winner",
            url="https://example.org/winner",
            category="tool",
            channel="general_web_google",
            position=1,
        ),
    )
    loser = LeadFoundEvent(
        run_id=RUN_ID,
        sequence=1,
        recorded_at="2026-08-12T00:00:00+00:00",
        payload=LeadFoundPayload(
            name="Loser",
            url="https://example.org/loser",
            category="tool",
            channel="general_web_google",
            position=1,
        ),
    )

    append_event(path, winner, expected_last_sequence=0)

    # A stale optimistic writer that still expects sequence 0 loses.
    with pytest.raises(JournalConflictError):
        append_event(path, loser, expected_last_sequence=0)
    # A conflicting duplicate at an already-committed sequence is rejected.
    with pytest.raises(JournalConflictError):
        append_event(path, loser)
    # The identical winning event re-appends as an idempotent no-op.
    assert append_event(path, winner).idempotent

    parsed = read_journal(path)
    assert [event.sequence for event in parsed.events] == [0, 1]
    assert isinstance(parsed.events[1], LeadFoundEvent)
    assert parsed.events[1].payload.name == "Winner"


# --------------------------------------------------------------------------- #
# Provider-gap and quarantine parity                                          #
# --------------------------------------------------------------------------- #
def test_provider_gap_run_parity_records_gaps_not_executions() -> None:
    config, result = _gap_run()
    events = ja.discovery_run_events(config, result, run_id=RUN_ID)

    from_result = ja.synthetic_from_result(config, result, run_id=RUN_ID)
    from_events = ja.synthetic_from_events(events)

    assert from_result == from_events
    assert from_result.provider_gaps
    assert not from_result.executed_queries
    assert not from_result.leads
    assert {gap.reason for gap in from_result.provider_gaps} == {"transport_error"}
    assert all(gap.failure_stage == "request" for gap in from_result.provider_gaps)


def test_model_quarantine_events_are_recorded_and_parity_holds() -> None:
    config, result = _quarantine_run()
    events = ja.discovery_run_events(config, result, run_id=RUN_ID)

    from_result = ja.synthetic_from_result(config, result, run_id=RUN_ID)
    from_events = ja.synthetic_from_events(events)

    assert from_result == from_events
    assert len(from_result.invalid_model_responses) == 1
    assert from_result.invalid_model_responses[0].scope == "candidate"
    assert any(event.kind == "model_response_invalid" for event in events)
