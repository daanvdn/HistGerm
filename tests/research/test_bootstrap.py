from datetime import date
from pathlib import Path

import pytest

from histgerm.catalog import load_catalog
from histgerm.research import initialize_ledger, load_ledger, validate_ledger


def test_bootstrap_creates_exact_matrix_and_trusted_candidates(tmp_path: Path) -> None:
    path = tmp_path / "ledger.yaml"
    ledger = initialize_ledger(path, on=date(2026, 8, 12))
    assert len(ledger.sweeps) == 9
    assert {(item.category, item.stage) for item in ledger.sweeps} == {
        (category, stage)
        for category in ("corpus", "tool", "dictionary")
        for stage in ("ohg", "mhg", "enhg")
    }
    catalog = load_catalog()
    inventory = (
        ("corpus", catalog.corpora, "covered_stages"),
        ("tool", catalog.tools, "supported_stages"),
        ("dictionary", catalog.dictionaries, "covered_stages"),
    )
    expected = {
        record.id: (
            category,
            [stage.value for stage in (getattr(record, stage_field) or [])] or None,
            record.description,
        )
        for category, records, stage_field in inventory
        for record in records
    }
    assert {
        item.resource_id: (
            item.category,
            item.discovery_stage_claims,
            item.disposition_reason,
        )
        for item in ledger.candidates
    } == expected
    assert validate_ledger(path) == load_ledger(path)
    with pytest.raises(FileExistsError):
        initialize_ledger(path, on=date(2026, 8, 12))
