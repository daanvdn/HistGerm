"""TASK-MIG-007: typed append-only run journal, store, replay, and CLI.

These tests exercise the journal model/store contract end to end:

* every event kind and the fixed envelope validate, and schema/version/typing
  violations are rejected,
* replay is a deterministic, order-sensitive reduction,
* durable atomic append leaves the prior journal valid under interruption, and
  reads recover a torn trailing line while rejecting mid-file corruption,
* append is idempotent on ``(run_id, sequence)`` and rejects conflicting
  duplicates, wrong run identifiers, and sequence gaps (optimistic concurrency),
* compaction records a verifiable content hash and last sequence, and a tampered
  checkpoint is fatal,
* concurrent writers never lose an update, and
* each CLI subcommand emits exactly one JSON object with the right exit code.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from histgerm.research import journal_store as store
from histgerm.research import run_journal as journal
from histgerm.research.journal_store import (
    AppendResult,
    JournalConflictError,
    JournalCorruptionError,
    JournalPathError,
    JournalWriteError,
    append_event,
    compact_journal,
    read_journal,
    validate_journal_path,
)
from histgerm.research.run_journal import (
    AnyJournalEvent,
    canonical_schema_json,
    encode_event,
    encode_events,
    event_schema,
    journal_content_hash,
    parse_event,
    replay_journal,
    schema_digest,
)

ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = ROOT / "migration-state.json"
SCHEMA_ARTIFACT = (
    Path(__file__).resolve().parent / "fixtures" / "journal_event_schema.txt"
)

_WORKER_SCRIPT = (
    "import sys, time, random;"
    "from pathlib import Path;"
    "from histgerm.research.journal_store import read_journal, append_event, "
    "JournalConflictError;"
    "from histgerm.research.run_journal import parse_event;"
    "path = Path(sys.argv[1]); worker = sys.argv[2];"
    "\nwhile True:\n"
    "    parsed = read_journal(path)\n"
    "    seq = parsed.last_sequence + 1\n"
    "    event = parse_event({'run_id': 'r1', 'sequence': seq,"
    " 'recorded_at': '2026-08-16T00:00:00+00:00', 'kind': 'lead_found',"
    " 'payload': {'name': worker, 'url': 'https://example.org/' + worker,"
    " 'category': 'tool', 'channel': 'web_en', 'position': 1}})\n"
    "    try:\n"
    "        append_event(path, event, expected_last_sequence=parsed.last_sequence)\n"
    "        break\n"
    "    except JournalConflictError:\n"
    "        time.sleep(random.uniform(0, 0.02))\n"
)


# --------------------------------------------------------------------------- #
# Event builders                                                              #
# --------------------------------------------------------------------------- #
def _event(
    sequence: int,
    kind: str,
    payload: dict[str, Any],
    *,
    run_id: str = "r1",
    schema_version: int = journal.JOURNAL_SCHEMA_VERSION,
) -> AnyJournalEvent:
    return parse_event(
        {
            "schema_version": schema_version,
            "run_id": run_id,
            "sequence": sequence,
            "recorded_at": f"2026-08-16T00:00:{sequence % 60:02d}+00:00",
            "kind": kind,
            "payload": payload,
        }
    )


def _run_started(
    sequence: int = 0,
    *,
    run_id: str = "r1",
    schema_version: int = journal.JOURNAL_SCHEMA_VERSION,
) -> AnyJournalEvent:
    return _event(
        sequence,
        "run_started",
        {
            "category": "tool",
            "stage": "mhg",
            "qualifiers": [],
            "parameters_digest": "0" * 64,
            "run_on": "2026-08-16",
        },
        run_id=run_id,
        schema_version=schema_version,
    )


def _lead(sequence: int, name: str = "MhgBERT") -> AnyJournalEvent:
    return _event(
        sequence,
        "lead_found",
        {
            "name": name,
            "url": f"https://example.org/{name}",
            "category": "tool",
            "channel": "web_en",
            "position": 1,
        },
    )


_ALL_KIND_PAYLOADS: dict[str, dict[str, Any]] = {
    "run_started": {
        "category": "tool",
        "stage": "mhg",
        "qualifiers": ["diachronic"],
        "parameters_digest": "a" * 64,
        "run_on": "2026-08-16",
    },
    "query_planned": {
        "provider": "clarin",
        "channel": "clarin",
        "language": "de",
        "query": "Mittelhochdeutsch Tagger",
        "intent_id": "intent-tool-mhg-tagging-de",
    },
    "query_executed": {
        "provider": "clarin",
        "channel": "clarin",
        "query": "Mittelhochdeutsch Tagger",
        "result_count": 5,
        "lead_count": 2,
        "http_status": 200,
        "assessment": "usable",
    },
    "provider_gap": {
        "provider": "olac",
        "channel": "olac",
        "reason": "transport gap",
        "failure_stage": "connect",
    },
    "lead_found": {
        "name": "MhgBERT",
        "url": "https://example.org/mhgbert",
        "category": "tool",
        "channel": "web_en",
        "position": 3,
    },
    "model_response_invalid": {
        "iteration": 1,
        "scope": "candidate",
        "reason": "extra field",
        "position": 2,
    },
    "retry_scheduled": {"iteration": 2, "reason": "schema feedback"},
    "candidate_blocked": {
        "candidate_id": "candidate-mhgbert",
        "name": "MhgBERT",
        "evidence_gaps": ["Canonical stage evidence unavailable."],
    },
    "candidate_researched": {
        "candidate_id": "candidate-mhgbert",
        "disposition": "added",
        "resource_id": "tool-mhgbert",
    },
    "ledger_revision_observed": {"revision": 95},
    "ledger_mutation_proposed": {
        "operation": "apply-result",
        "target_id": "candidate-mhgbert",
        "expected_revision": 95,
    },
    "checkpoint": {"content_hash": "b" * 64, "last_sequence": 4},
    "run_completed": {
        "status": "complete",
        "leads": 3,
        "candidates": 2,
        "blocked": 1,
    },
}


def _seed(path: Path, *events: AnyJournalEvent) -> None:
    for event in events:
        append_event(path, event)


@pytest.fixture
def journal_path(tmp_path: Path) -> Path:
    return tmp_path / "run.journal.jsonl"


# --------------------------------------------------------------------------- #
# Schema / version / event typing                                             #
# --------------------------------------------------------------------------- #
def test_every_event_kind_round_trips() -> None:
    assert set(_ALL_KIND_PAYLOADS) == set(journal.JOURNAL_EVENT_KINDS)
    for sequence, kind in enumerate(journal.JOURNAL_EVENT_KINDS):
        event = _event(sequence, kind, _ALL_KIND_PAYLOADS[kind])
        assert event.kind == kind
        assert event.schema_version == journal.JOURNAL_SCHEMA_VERSION
        reparsed = parse_event(json.loads(encode_event(event)))
        assert reparsed == event


def test_payload_must_match_kind_and_reject_extras() -> None:
    with pytest.raises(ValidationError):
        _event(0, "run_started", _ALL_KIND_PAYLOADS["lead_found"])
    with pytest.raises(ValidationError):
        _event(0, "lead_found", {**_ALL_KIND_PAYLOADS["lead_found"], "extra": 1})
    with pytest.raises(ValidationError):
        parse_event(
            {
                "run_id": "r1",
                "sequence": 0,
                "recorded_at": "2026-08-16T00:00:00+00:00",
                "kind": "does_not_exist",
                "payload": {},
            }
        )


def test_envelope_requires_valid_fields() -> None:
    with pytest.raises(ValidationError):
        _event(-1, "run_started", _ALL_KIND_PAYLOADS["run_started"])
    with pytest.raises(ValidationError):
        _event(
            0,
            "run_started",
            _ALL_KIND_PAYLOADS["run_started"],
            run_id="",
        )
    with pytest.raises(ValidationError):
        parse_event(
            {
                "run_id": "r1",
                "sequence": 0,
                "recorded_at": "not-a-timestamp",
                "kind": "ledger_revision_observed",
                "payload": {"revision": 1},
            }
        )


def test_append_rejects_unsupported_schema_version(journal_path: Path) -> None:
    event = _run_started(schema_version=2)
    with pytest.raises(JournalConflictError, match="schema version"):
        append_event(journal_path, event)
    assert not journal_path.exists()


# --------------------------------------------------------------------------- #
# Deterministic replay                                                        #
# --------------------------------------------------------------------------- #
def test_replay_is_deterministic_and_order_sensitive() -> None:
    events = [
        _run_started(0),
        _event(1, "ledger_revision_observed", {"revision": 95}),
        _lead(2, "Alpha"),
        _event(
            3,
            "candidate_researched",
            {"candidate_id": "candidate-alpha", "disposition": "added"},
        ),
        _event(4, "ledger_revision_observed", {"revision": 96}),
        _event(
            5,
            "run_completed",
            {"status": "complete", "leads": 1, "candidates": 1, "blocked": 0},
        ),
    ]
    first = replay_journal(events)
    second = replay_journal(list(events))
    assert first == second
    assert first.as_status() == second.as_status()
    assert first.last_ledger_revision == 96
    assert first.leads == 1
    assert first.counts["ledger_revision_observed"] == 2

    reordered = [events[0], events[4], events[1], *events[2:4], events[5]]
    assert replay_journal(reordered).last_ledger_revision == 95


def test_replay_status_is_json_serializable() -> None:
    events = [_run_started(0), _lead(1), _lead(2, "Beta")]
    status = replay_journal(events).as_status()
    assert json.loads(json.dumps(status))["leads"] == 2


# --------------------------------------------------------------------------- #
# Idempotency and optimistic-concurrency conflict handling                    #
# --------------------------------------------------------------------------- #
def test_identical_duplicate_append_is_idempotent(journal_path: Path) -> None:
    event = _run_started(0)
    first = append_event(journal_path, event)
    assert first.idempotent is False
    before = journal_path.read_bytes()
    second = append_event(journal_path, event)
    assert second.idempotent is True
    assert second.content_hash == first.content_hash
    assert journal_path.read_bytes() == before


def test_conflicting_duplicate_append_fails(journal_path: Path) -> None:
    _seed(journal_path, _run_started(0))
    conflicting = _event(
        0,
        "run_started",
        {**_ALL_KIND_PAYLOADS["run_started"], "category": "corpus"},
    )
    with pytest.raises(JournalConflictError, match="conflicts"):
        append_event(journal_path, conflicting)


def test_wrong_run_id_append_fails(journal_path: Path) -> None:
    _seed(journal_path, _run_started(0))
    with pytest.raises(JournalConflictError, match="run id"):
        append_event(
            journal_path,
            _event(1, "lead_found", _ALL_KIND_PAYLOADS["lead_found"], run_id="other"),
        )


def test_sequence_gap_append_fails(journal_path: Path) -> None:
    _seed(journal_path, _run_started(0))
    with pytest.raises(JournalConflictError, match="gap"):
        append_event(journal_path, _lead(2))


def test_optimistic_concurrency_expected_last_sequence(journal_path: Path) -> None:
    _seed(journal_path, _run_started(0))
    with pytest.raises(JournalConflictError, match="expected last sequence"):
        append_event(journal_path, _lead(1), expected_last_sequence=5)
    ok = append_event(journal_path, _lead(1), expected_last_sequence=0)
    assert ok.sequence == 1


def test_first_event_must_be_run_started(journal_path: Path) -> None:
    with pytest.raises(JournalConflictError, match="run_started"):
        append_event(journal_path, _lead(0))
    assert not journal_path.exists()


# --------------------------------------------------------------------------- #
# Durable, interruption-safe append                                           #
# --------------------------------------------------------------------------- #
def test_interrupted_append_leaves_prior_journal_valid(
    journal_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(journal_path, _run_started(0), _lead(1))
    prior = read_journal(journal_path)
    prior_bytes = journal_path.read_bytes()

    def boom(*_: object) -> None:
        raise OSError("synthetic interruption")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(JournalWriteError):
        append_event(journal_path, _lead(2, "Gamma"))
    monkeypatch.undo()

    assert journal_path.read_bytes() == prior_bytes
    recovered = read_journal(journal_path)
    assert recovered.events == prior.events
    assert recovered.content_hash == prior.content_hash
    assert not list(journal_path.parent.glob(".*.tmp"))


def test_torn_trailing_line_is_recovered(journal_path: Path) -> None:
    _seed(journal_path, _run_started(0), _lead(1))
    valid = read_journal(journal_path)
    journal_path.write_bytes(journal_path.read_bytes() + b'{"run_id": "r1"')
    recovered = read_journal(journal_path)
    assert recovered.truncated_tail is True
    assert recovered.events == valid.events
    assert recovered.last_sequence == 1
    # A subsequent append rewrites a clean journal, dropping the torn tail.
    append_event(journal_path, _lead(2, "Gamma"))
    clean = read_journal(journal_path)
    assert clean.truncated_tail is False
    assert clean.last_sequence == 2


def test_mid_file_corruption_is_fatal(journal_path: Path) -> None:
    _seed(journal_path, _run_started(0), _lead(1), _lead(2, "Beta"))
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    lines[1] = "{ this is not json }"
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(JournalCorruptionError):
        read_journal(journal_path)


def test_non_utf8_journal_is_fatal(journal_path: Path) -> None:
    journal_path.write_bytes(b"\xff\xfe not utf8\n")
    with pytest.raises(JournalCorruptionError, match="UTF-8"):
        read_journal(journal_path)


def test_sequence_gap_on_disk_is_fatal(journal_path: Path) -> None:
    events = (_run_started(0), _lead(2))
    journal_path.write_bytes(encode_events(events))
    with pytest.raises(JournalCorruptionError, match="sequence"):
        read_journal(journal_path)


def test_wrong_run_id_on_disk_is_fatal(journal_path: Path) -> None:
    events = (
        _run_started(0),
        _event(1, "lead_found", _ALL_KIND_PAYLOADS["lead_found"], run_id="other"),
    )
    journal_path.write_bytes(encode_events(events))
    with pytest.raises(JournalCorruptionError, match="run id"):
        read_journal(journal_path)


def test_first_event_on_disk_must_be_run_started(journal_path: Path) -> None:
    journal_path.write_bytes(encode_events((_lead(0),)))
    with pytest.raises(JournalCorruptionError, match="run_started"):
        read_journal(journal_path)


# --------------------------------------------------------------------------- #
# Compaction and checkpoint / recovery semantics                             #
# --------------------------------------------------------------------------- #
def test_compaction_records_verifiable_checkpoint(journal_path: Path) -> None:
    _seed(journal_path, _run_started(0), _lead(1))
    before = read_journal(journal_path)
    result = compact_journal(journal_path)
    assert result.kind == "checkpoint"
    assert result.idempotent is False
    parsed = read_journal(journal_path)
    checkpoint = parsed.events[-1]
    assert checkpoint.kind == "checkpoint"
    assert checkpoint.payload.content_hash == before.content_hash
    assert checkpoint.payload.last_sequence == before.last_sequence


def test_compaction_is_idempotent_on_a_checkpointed_tail(journal_path: Path) -> None:
    _seed(journal_path, _run_started(0), _lead(1))
    compact_journal(journal_path)
    again = compact_journal(journal_path)
    assert again.idempotent is True
    assert read_journal(journal_path).events[-1].kind == "checkpoint"


def test_compaction_rejects_empty_journal(journal_path: Path) -> None:
    with pytest.raises(JournalConflictError, match="empty"):
        compact_journal(journal_path)


def test_tampered_checkpoint_hash_is_fatal(journal_path: Path) -> None:
    _seed(journal_path, _run_started(0), _lead(1))
    compact_journal(journal_path)
    lines = journal_path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[-1])
    tampered["payload"]["content_hash"] = "c" * 64
    lines[-1] = json.dumps(
        tampered, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(JournalCorruptionError, match="content hash"):
        read_journal(journal_path)


# --------------------------------------------------------------------------- #
# Concurrency                                                                 #
# --------------------------------------------------------------------------- #
def test_concurrent_writers_never_lose_an_update(journal_path: Path) -> None:
    _seed(journal_path, _run_started(0))
    workers = ["alpha", "beta", "gamma", "delta"]
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", _WORKER_SCRIPT, str(journal_path), worker]
        )
        for worker in workers
    ]
    for process in processes:
        assert process.wait(timeout=60) == 0
    parsed = read_journal(journal_path)
    assert parsed.last_sequence == len(workers)
    assert [event.sequence for event in parsed.events] == list(range(len(workers) + 1))
    recorded = {
        event.payload.name for event in parsed.events if event.kind == "lead_found"
    }
    assert recorded == set(workers)


# --------------------------------------------------------------------------- #
# Path safety                                                                 #
# --------------------------------------------------------------------------- #
def test_journal_path_requires_operational_suffix(tmp_path: Path) -> None:
    with pytest.raises(JournalPathError, match="journal.jsonl"):
        validate_journal_path(tmp_path / "run.txt", option="--journal")


def test_journal_path_rejects_package_data(tmp_path: Path) -> None:
    inside = store._PACKAGE_DIR / "data" / "leak.journal.jsonl"
    with pytest.raises(JournalPathError, match="outside the histgerm package"):
        validate_journal_path(inside, option="--journal")


def test_journal_path_accepts_operational_file(tmp_path: Path) -> None:
    path = tmp_path / "run.journal.jsonl"
    assert validate_journal_path(path, option="--journal") == path


# --------------------------------------------------------------------------- #
# CLI: exactly one JSON object and correct exit codes                         #
# --------------------------------------------------------------------------- #
def _cli(*args: str) -> tuple[int, dict[str, Any]]:
    completed = subprocess.run(
        [sys.executable, "-m", "histgerm.research", *args],
        capture_output=True,
        text=True,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, (completed.stdout, completed.stderr)
    return completed.returncode, json.loads(lines[0])


def _write_event_file(path: Path, event: AnyJournalEvent) -> Path:
    path.write_text(encode_event(event), encoding="utf-8")
    return path


def test_cli_append_validate_status_compact(tmp_path: Path) -> None:
    journal_path = tmp_path / "run.journal.jsonl"
    started = _write_event_file(tmp_path / "e0.json", _run_started(0))
    lead = _write_event_file(tmp_path / "e1.json", _lead(1))

    code, appended = _cli(
        "journal-append", "--journal", str(journal_path), "--input", str(started)
    )
    assert code == 0 and appended["ok"] is True
    assert appended["sequence"] == 0 and appended["idempotent"] is False

    code, idempotent = _cli(
        "journal-append", "--journal", str(journal_path), "--input", str(started)
    )
    assert code == 0 and idempotent["idempotent"] is True

    code, _ = _cli(
        "journal-append",
        "--journal",
        str(journal_path),
        "--input",
        str(lead),
        "--expected-last-sequence",
        "0",
    )
    assert code == 0

    code, validated = _cli("journal-validate", "--journal", str(journal_path))
    assert code == 0 and validated["events"] == 2 and validated["last_sequence"] == 1
    assert validated["truncated_tail"] is False

    code, status = _cli("journal-status", "--journal", str(journal_path))
    assert code == 0 and status["status"]["leads"] == 1

    code, compacted = _cli("journal-compact", "--journal", str(journal_path))
    assert code == 0 and compacted["kind"] == "checkpoint"


def test_cli_conflict_exit_code(tmp_path: Path) -> None:
    journal_path = tmp_path / "run.journal.jsonl"
    started = _write_event_file(tmp_path / "e0.json", _run_started(0))
    _cli("journal-append", "--journal", str(journal_path), "--input", str(started))
    lead = _write_event_file(tmp_path / "e1.json", _lead(1))
    code, error = _cli(
        "journal-append",
        "--journal",
        str(journal_path),
        "--input",
        str(lead),
        "--expected-last-sequence",
        "5",
    )
    assert code == 3
    assert error["ok"] is False
    assert error["errors"][0]["code"] == "journal_conflict"


def test_cli_invalid_path_exit_code(tmp_path: Path) -> None:
    code, error = _cli("journal-validate", "--journal", str(tmp_path / "run.txt"))
    assert code == 2
    assert error["errors"][0]["code"] == "invalid_journal_path"


def test_cli_missing_journal_exit_code(tmp_path: Path) -> None:
    code, error = _cli(
        "journal-validate", "--journal", str(tmp_path / "absent.journal.jsonl")
    )
    assert code == 4
    assert error["errors"][0]["code"] == "filesystem_error"


def test_cli_corrupt_journal_exit_code(tmp_path: Path) -> None:
    journal_path = tmp_path / "run.journal.jsonl"
    journal_path.write_bytes(encode_events((_run_started(0), _lead(2))))
    code, error = _cli("journal-validate", "--journal", str(journal_path))
    assert code == 2
    assert error["errors"][0]["code"] == "invalid_journal"


# --------------------------------------------------------------------------- #
# Generated JSON Schema artifact                                              #
# --------------------------------------------------------------------------- #
def test_event_schema_is_a_discriminated_union() -> None:
    schema = event_schema()
    assert "oneOf" in schema or "$ref" in schema
    text = json.dumps(schema)
    for kind in journal.JOURNAL_EVENT_KINDS:
        assert kind in text


def test_schema_artifact_file_matches_generated_schema() -> None:
    stored = SCHEMA_ARTIFACT.read_text(encoding="utf-8")
    assert stored == canonical_schema_json()
    assert hashlib.sha256(stored.encode("utf-8")).hexdigest() == schema_digest()


@pytest.fixture(scope="module")
def state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        pytest.skip("migration-state.json is absent on a fresh checkout")
    data: dict[str, Any] = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return data


def test_recorded_schema_hash_matches(state: dict[str, Any]) -> None:
    recorded = state["artifacts"]["journal"]
    assert recorded["schema_sha256"] == schema_digest()
    assert (
        recorded["schema_artifact"]
        == "tests/research/fixtures/journal_event_schema.txt"
    )


# --------------------------------------------------------------------------- #
# Encoding invariants                                                         #
# --------------------------------------------------------------------------- #
def test_encode_is_canonical_and_hash_matches_bytes() -> None:
    events = [_run_started(0), _lead(1)]
    payload = encode_events(events)
    assert payload.endswith(b"\n")
    assert journal_content_hash(events) == hashlib.sha256(payload).hexdigest()
    for event in events:
        assert "\n" not in encode_event(event)


def test_append_result_shape(journal_path: Path) -> None:
    result = append_event(journal_path, _run_started(0))
    assert isinstance(result, AppendResult)
    assert result.run_id == "r1"
    assert result.event_count == 1
