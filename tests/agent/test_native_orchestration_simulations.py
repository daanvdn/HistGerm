"""Executable native-orchestration simulations for the curator contracts.

``TASK-MIG-009`` replaces the scripted discovery capability exchange with native
Copilot orchestration recorded in a deterministic, append-only run journal.
These simulations exercise the checked-in journal machinery directly (no network
or subprocess) and assert output *shapes* and *command sequencing*, not wording:

* a bilingual, multi-channel discovery run produces a journal whose planned
  queries cover German and English across several channels while preserving the
  structured query ``intent_id``;
* an interrupted append leaves the prior journal valid, resume never repeats a
  confirmed retrieval, and the run continues from the confirmed sequence;
* the publication path derives its run-report facts from the journal replay
  rather than from prose; and
* no curator surface (agent, four skills, or operator guide) still requires an
  old-exchange interface symbol.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from histgerm.research.journal_store import (
    append_event,
    journal_status,
    read_journal,
    validate_journal_path,
)
from histgerm.research.run_journal import (
    AnyJournalEvent,
    CandidateBlockedEvent,
    CandidateBlockedPayload,
    CandidateResearchedEvent,
    CandidateResearchedPayload,
    LeadFoundEvent,
    LeadFoundPayload,
    ProviderGapEvent,
    ProviderGapPayload,
    QueryExecutedEvent,
    QueryExecutedPayload,
    QueryPlannedEvent,
    QueryPlannedPayload,
    RunCompletedEvent,
    RunCompletedPayload,
    RunStartedEvent,
    RunStartedPayload,
    replay_journal,
)

ROOT = Path(__file__).parents[2]
AGENT = ROOT / ".github" / "agents" / "histgerm-inventory-curator.agent.md"
CURATOR_DOC = ROOT / "docs" / "inventory-curator.md"
SKILL_ROOT = ROOT / ".github" / "skills"
SKILLS = (
    "discover-histgerm-resources",
    "curate-histgerm-resource",
    "validate-histgerm-inventory",
    "publish-histgerm-batch",
)

RUN_ID = "run-sim-0001"
AT = "2026-08-16T00:00:00+00:00"
ON = "2026-08-16"
DIGEST = hashlib.sha256(b"native-orchestration-simulation").hexdigest()


def _started() -> RunStartedEvent:
    return RunStartedEvent(
        run_id=RUN_ID,
        sequence=0,
        recorded_at=AT,
        payload=RunStartedPayload(
            category="tool",
            stage="mhg",
            qualifiers=[],
            parameters_digest=DIGEST,
            run_on=ON,
        ),
    )


def _planned(
    seq: int, language: str, channel: str, intent_id: str
) -> QueryPlannedEvent:
    return QueryPlannedEvent(
        run_id=RUN_ID,
        sequence=seq,
        recorded_at=AT,
        payload=QueryPlannedPayload(
            provider="google" if channel == "general_web" else channel,
            channel=channel,
            language=language,  # type: ignore[arg-type]
            query=f"{language}:{channel}:{intent_id}",
            intent_id=intent_id,
        ),
    )


def _executed(seq: int, channel: str, leads: int) -> QueryExecutedEvent:
    return QueryExecutedEvent(
        run_id=RUN_ID,
        sequence=seq,
        recorded_at=AT,
        payload=QueryExecutedPayload(
            provider="google" if channel == "general_web" else channel,
            channel=channel,
            query=f"executed:{channel}",
            result_count=3,
            lead_count=leads,
            http_status=200,
            assessment="inspected every returned item",
        ),
    )


def _bilingual_multichannel_events() -> tuple[AnyJournalEvent, ...]:
    """A confirmed bilingual, multi-channel run ending in completion."""

    return (
        _started(),
        _planned(1, "de", "general_web", "mhg.tool.parser"),
        _executed(2, "general_web", 1),
        _planned(3, "en", "general_web", "mhg.tool.parser"),
        _executed(4, "general_web", 0),
        _planned(5, "de", "github_repository", "mhg.tool.lemmatizer"),
        _executed(6, "github_repository", 1),
        _planned(7, "en", "huggingface_models", "mhg.model.bert"),
        ProviderGapEvent(
            run_id=RUN_ID,
            sequence=8,
            recorded_at=AT,
            payload=ProviderGapPayload(
                provider="huggingface_models",
                channel="huggingface_models",
                reason="HTTP 429 through bounded_http",
                failure_stage="rate_limit",
            ),
        ),
        LeadFoundEvent(
            run_id=RUN_ID,
            sequence=9,
            recorded_at=AT,
            payload=LeadFoundPayload(
                name="Example MHG Parser",
                url="https://example.org/mhg-parser",
                category="tool",
                channel="github_repository",
                position=1,
            ),
        ),
        CandidateResearchedEvent(
            run_id=RUN_ID,
            sequence=10,
            recorded_at=AT,
            payload=CandidateResearchedPayload(
                candidate_id="candidate-0001",
                disposition="added",
                resource_id="tool-example-mhg-parser",
            ),
        ),
        CandidateBlockedEvent(
            run_id=RUN_ID,
            sequence=11,
            recorded_at=AT,
            payload=CandidateBlockedPayload(
                candidate_id="candidate-0002",
                name="Ambiguous Component",
                evidence_gaps=["historical-stage coverage unverified"],
            ),
        ),
        RunCompletedEvent(
            run_id=RUN_ID,
            sequence=12,
            recorded_at=AT,
            payload=RunCompletedPayload(
                status="complete", leads=1, candidates=1, blocked=1
            ),
        ),
    )


def _append_all(path: Path, events: tuple[AnyJournalEvent, ...]) -> None:
    validate_journal_path(path, option="--journal")
    last = -1
    for event in events:
        result = append_event(path, event, expected_last_sequence=last)
        assert not result.idempotent
        last = result.last_sequence


def test_bilingual_multichannel_run_journal_shape(tmp_path: Path) -> None:
    """The journal covers German and English across channels, keeping intents."""

    path = tmp_path / "run.journal.jsonl"
    events = _bilingual_multichannel_events()
    _append_all(path, events)

    parsed = read_journal(path)
    assert parsed.truncated_tail is False
    assert parsed.run_id == RUN_ID
    # Command sequencing: run_started first, run_completed last, gap-free.
    assert parsed.events[0].kind == "run_started"
    assert parsed.events[-1].kind == "run_completed"
    assert [event.sequence for event in parsed.events] == list(range(len(events)))

    planned = [e for e in parsed.events if isinstance(e, QueryPlannedEvent)]
    assert {p.payload.language for p in planned} == {"de", "en"}
    assert len({p.payload.channel for p in planned}) >= 3
    # Structured query intents survive end to end.
    assert all(p.payload.intent_id for p in planned)
    assert "mhg.model.bert" in {p.payload.intent_id for p in planned}


def test_interruption_resume_never_repeats_confirmed_retrieval(
    tmp_path: Path,
) -> None:
    """A torn append is recovered and resume repeats no confirmed retrieval."""

    path = tmp_path / "run.journal.jsonl"
    confirmed = (
        _started(),
        _planned(1, "de", "general_web", "mhg.tool.parser"),
        _executed(2, "general_web", 1),
    )
    _append_all(path, confirmed)

    # Simulate an interrupted append: a torn trailing line without a newline.
    with path.open("ab") as handle:
        handle.write(b'{"schema_version":1,"run_id":"run-sim-0001","sequence":3')

    recovered = read_journal(path)
    assert recovered.truncated_tail is True
    assert len(recovered.events) == len(confirmed)
    assert recovered.last_sequence == 2

    # Re-appending an already-confirmed event is an idempotent no-op.
    replayed = append_event(path, confirmed[2], expected_last_sequence=None)
    assert replayed.idempotent is True
    assert replayed.event_count == len(confirmed)

    # Resume continues from the confirmed sequence without rewriting history.
    lead = LeadFoundEvent(
        run_id=RUN_ID,
        sequence=3,
        recorded_at=AT,
        payload=LeadFoundPayload(
            name="Resumed Lead",
            url="https://example.org/resumed",
            category="tool",
            channel="general_web",
            position=1,
        ),
    )
    result = append_event(path, lead, expected_last_sequence=2)
    assert result.idempotent is False
    assert result.last_sequence == 3

    final = read_journal(path)
    assert final.truncated_tail is False
    assert [event.sequence for event in final.events] == [0, 1, 2, 3]


def test_publication_consumes_journal_derived_results(tmp_path: Path) -> None:
    """Publication-report facts are derived from the deterministic journal replay."""

    path = tmp_path / "run.journal.jsonl"
    events = _bilingual_multichannel_events()
    _append_all(path, events)

    # Replay is deterministic: the on-disk read and the in-memory reduction agree.
    parsed = read_journal(path)
    status = journal_status(path)
    assert status.as_status() == replay_journal(parsed.events).as_status()

    # The report facts come from the journal, not from prose.
    assert status.completed is not None
    assert status.completed.status == "complete"
    assert status.leads == 1
    assert status.provider_gaps == 1
    assert status.blocked_candidates == ("candidate-0002",)
    assert status.researched == (("candidate-0001", "added"),)


def _surface_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").casefold()


@pytest.mark.parametrize(
    "path",
    (
        AGENT,
        CURATOR_DOC,
        *(SKILL_ROOT / name / "SKILL.md" for name in SKILLS),
    ),
)
def test_no_surface_requires_an_old_exchange_symbol(path: Path) -> None:
    """No agent, skill, or doc surface requires an old-exchange interface symbol."""

    text = _surface_text(path)
    forbidden = (
        "--checkpoint",
        "--resume",
        "model_elicitation",
        "result_inspection",
        "needs_input",
        "read_exchange",
        "apply_exchange",
        "discoverycheckpoint",
        "capability exchange",
        "capability-exchange",
        "discover --",
    )
    present = [token for token in forbidden if token in text]
    assert present == [], (path.name, present)


@pytest.mark.parametrize(
    "path",
    (AGENT, CURATOR_DOC, SKILL_ROOT / SKILLS[0] / "SKILL.md"),
)
def test_discovery_surfaces_reference_the_run_journal(path: Path) -> None:
    """Discovery-bearing surfaces route external results into the run journal."""

    text = _surface_text(path)
    missing = [
        token
        for token in ("journal-append", "journal-status", "run_completed")
        if token not in text
    ]
    assert missing == [], (path.name, missing)
