from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]
from conftest import candidate_data, pass_data, write_json

from histgerm.models import LanguageStage
from histgerm.research import (
    CandidateEntry,
    LedgerPolicyError,
    LedgerRevisionError,
    LedgerWriteError,
    SearchPass,
    load_ledger,
    record_search_pass,
    select_next_sweep,
    upsert_candidate,
)


def _raise_os_error(*_: object) -> None:
    raise OSError("synthetic")


def test_mutations_increment_once_and_reject_stale_revision(
    ledger_path: Path,
) -> None:
    first = upsert_candidate(
        ledger_path,
        CandidateEntry.model_validate(candidate_data()),
        expected_revision=0,
    )
    assert first.revision == 1
    with pytest.raises(LedgerRevisionError, match="expected revision 0"):
        upsert_candidate(
            ledger_path,
            CandidateEntry.model_validate(candidate_data(id="candidate-other")),
            expected_revision=0,
        )
    assert load_ledger(ledger_path).revision == 1
    assert not ledger_path.with_name(f".{ledger_path.name}.lock").exists()
    assert not list(ledger_path.parent.glob(f".{ledger_path.name}.*.tmp"))


def test_cross_process_mutations_serialize_without_lost_updates(
    ledger_path: Path, tmp_path: Path
) -> None:
    inputs = []
    processes = []
    for name in ("alpha", "beta"):
        payload = tmp_path / f"{name}.json"
        write_json(payload, candidate_data(id=f"candidate-{name}", name=name))
        inputs.append(payload)
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "histgerm.research",
                    "upsert-candidate",
                    "--ledger",
                    str(ledger_path),
                    "--expected-revision",
                    "0",
                    "--input",
                    str(payload),
                ],
                stdout=subprocess.PIPE,
                text=True,
            )
        )
    outputs = [process.communicate(timeout=20)[0] for process in processes]
    assert sorted(process.returncode for process in processes) == [0, 3]
    assert sum('"ok":true' in output for output in outputs) == 1
    ledger = load_ledger(ledger_path)
    assert ledger.revision == 1
    assert (
        len(
            {item.id for item in ledger.candidates}
            & {"candidate-alpha", "candidate-beta"}
        )
        == 1
    )
    assert not ledger_path.with_name(f".{ledger_path.name}.lock").exists()


def test_external_lock_is_deterministic_distinct_and_never_evicted(
    ledger_path: Path,
    tmp_path: Path,
) -> None:
    from histgerm.research.ledger import _lock_path_for_ledger

    lock = _lock_path_for_ledger(ledger_path)
    other_lock = _lock_path_for_ledger(tmp_path / "other-ledger.yaml")
    assert lock == _lock_path_for_ledger(ledger_path)
    assert lock != other_lock
    assert lock.parent != ledger_path.parent
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    old_inode = lock.stat().st_ino
    updated = upsert_candidate(
        ledger_path,
        CandidateEntry.model_validate(candidate_data()),
        expected_revision=0,
    )
    assert updated.revision == 1
    assert lock.stat().st_ino == old_inode


def test_interprocess_lock_timeout_releases_cleanly(
    ledger_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import histgerm.research.ledger as module

    script = (
        "import sys,time;"
        "from pathlib import Path;"
        "from histgerm.research.ledger import _ledger_file_lock,_lock_path_for_ledger;"
        "lock=_lock_path_for_ledger(Path(sys.argv[1]));"
        "ctx=_ledger_file_lock(lock);ctx.__enter__();"
        "print('ready',flush=True);time.sleep(2);ctx.__exit__(None,None,None)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(ledger_path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"
    monkeypatch.setattr(module, "_LOCK_TIMEOUT_SECONDS", 0.1)
    started = time.monotonic()
    with pytest.raises(module.LedgerWriteError, match="timed out"):
        upsert_candidate(
            ledger_path,
            CandidateEntry.model_validate(candidate_data()),
            expected_revision=0,
        )
    assert time.monotonic() - started < 1
    process.wait(timeout=5)
    updated = upsert_candidate(
        ledger_path,
        CandidateEntry.model_validate(candidate_data()),
        expected_revision=0,
    )
    assert updated.revision == 1


def test_two_empty_complete_passes_finish_a_sweep(ledger_path: Path) -> None:
    first = record_search_pass(
        ledger_path,
        SearchPass.model_validate(pass_data(suffix="one")),
        expected_revision=0,
    )
    second = record_search_pass(
        ledger_path,
        SearchPass.model_validate(pass_data(suffix="two") | {"run_on": "2026-08-13"}),
        expected_revision=first.revision,
    )
    sweep = next(item for item in second.sweeps if item.id == "corpus-mhg")
    assert (sweep.state, sweep.consecutive_empty_passes) == ("complete", 2)
    assert select_next_sweep(second).id == "corpus-ohg"
    assert select_next_sweep(second, stage=LanguageStage.ENHG).id == "corpus-enhg"


def test_complete_pass_rejects_pending_candidate(ledger_path: Path) -> None:
    pending = CandidateEntry.model_validate(
        candidate_data(disposition="pending", evidence_gaps=None)
    )
    upsert_candidate(ledger_path, pending, expected_revision=0)
    with pytest.raises(LedgerPolicyError, match="pending candidates"):
        record_search_pass(
            ledger_path,
            SearchPass.model_validate(
                pass_data(
                    candidate_ids=[pending.id],
                    new_candidate_ids=[pending.id],
                )
            ),
            expected_revision=1,
        )


def test_atomic_replace_failure_leaves_prior_ledger_intact(
    ledger_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = ledger_path.read_bytes()
    monkeypatch.setattr(os, "replace", _raise_os_error)
    with pytest.raises(LedgerWriteError, match="atomically replace ledger"):
        upsert_candidate(
            ledger_path,
            CandidateEntry.model_validate(candidate_data()),
            expected_revision=0,
        )
    assert ledger_path.read_bytes() == before
    assert load_ledger(ledger_path).revision == 0
    assert not list(ledger_path.parent.glob(f".{ledger_path.name}.*.tmp"))


def test_committed_ledger_is_durable_lf_canonical_yaml(ledger_path: Path) -> None:
    updated = upsert_candidate(
        ledger_path,
        CandidateEntry.model_validate(candidate_data()),
        expected_revision=0,
    )
    raw = ledger_path.read_bytes()
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")
    expected = yaml.safe_dump(
        updated.model_dump(mode="json", exclude_none=True),
        sort_keys=False,
        allow_unicode=True,
    ).encode("utf-8")
    assert raw == expected
