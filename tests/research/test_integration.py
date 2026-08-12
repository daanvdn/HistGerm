from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from conftest import candidate_data, pass_data

from histgerm.research import CandidateEntry, SearchPass, SweepEntry, load_ledger
from histgerm.research.ledger import select_next_sweep


def test_checked_in_ledger_validates() -> None:
    ledger = load_ledger(Path("research") / "discovery-ledger.yaml")
    assert ledger.schema_version == 1 and len(ledger.sweeps) == 9


def test_default_selector_returns_oldest_six_month_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import histgerm.research.ledger as module

    class FixedDate(date):
        @classmethod
        def today(cls) -> FixedDate:
            return cls(2026, 8, 31)

    monkeypatch.setattr(module, "date", FixedDate)
    seeded = load_ledger(Path("research") / "discovery-ledger.yaml")
    sweeps = []
    for sweep in seeded.sweeps:
        passes = [
            SearchPass.model_validate(
                pass_data(
                    category=sweep.category,
                    stage=sweep.stage.value,
                    suffix=suffix,
                )
                | {"run_on": run_on}
            )
            for suffix, run_on in (("one", "2026-08-11"), ("two", "2026-08-12"))
        ]
        sweeps.append(
            SweepEntry(
                id=sweep.id,
                category=sweep.category,
                stage=sweep.stage,
                state="complete",
                pass_count=2,
                consecutive_empty_passes=2,
                last_run_on=date(2026, 8, 12),
                passes=passes,
            )
        )
    old = CandidateEntry.model_validate(
        candidate_data(
            id="candidate-old",
            disposition="duplicate",
            resource_id="corpus-old",
            evidence_gaps=None,
            last_checked_on="2026-02-28",
        )
    )
    recent = CandidateEntry.model_validate(
        candidate_data(
            id="candidate-recent",
            disposition="duplicate",
            resource_id="corpus-recent",
            evidence_gaps=None,
            last_checked_on="2026-03-01",
        )
    )
    ledger = seeded.model_copy(update={"sweeps": sweeps, "candidates": [old, recent]})
    assert select_next_sweep(ledger).id == "candidate-old"
