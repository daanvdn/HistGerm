from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from conftest import candidate_data, pass_data, write_json

from histgerm.research.__main__ import main


def run_cli(*arguments: str) -> tuple[int, dict[str, Any]]:
    import contextlib
    import io

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = main(list(arguments))
    return code, json.loads(output.getvalue())


def test_bootstrap_validate_status_next_and_mutations(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger.yaml"
    code, response = run_cli("bootstrap", "--ledger", str(ledger))
    assert code == 0 and response["revision"] == 0
    for command in ("validate", "status", "next"):
        code, response = run_cli(command, "--ledger", str(ledger))
        assert code == 0 and response["command"] == command
    candidate = tmp_path / "candidate.json"
    write_json(candidate, candidate_data())
    code, response = run_cli(
        "upsert-candidate",
        "--ledger",
        str(ledger),
        "--expected-revision",
        "0",
        "--input",
        str(candidate),
    )
    assert code == 0 and response["revision"] == 1
    search = tmp_path / "pass.json"
    write_json(
        search,
        pass_data(
            candidate_ids=["candidate-example"],
            new_candidate_ids=["candidate-example"],
        ),
    )
    code, response = run_cli(
        "record-search",
        "--ledger",
        str(ledger),
        "--expected-revision",
        "1",
        "--input",
        str(search),
    )
    assert code == 0 and response["revision"] == 2


def test_cli_failure_codes_are_json(ledger_path: Path, tmp_path: Path) -> None:
    payload = tmp_path / "candidate.json"
    write_json(payload, candidate_data())
    code, response = run_cli(
        "upsert-candidate",
        "--ledger",
        str(ledger_path),
        "--expected-revision",
        "9",
        "--input",
        str(payload),
    )
    assert code == 3
    assert response["errors"][0]["code"] == "stale_revision"
    code, response = run_cli("unknown")
    assert code == 2 and response["errors"][0]["code"] == "invalid_arguments"


def test_cli_stdout_is_utf8() -> None:
    command = ["uv", "run", "python", "-m", "histgerm.research", "status"]
    output = subprocess.check_output(
        [*command, "--ledger", "research/discovery-ledger.yaml"]
    )
    assert "T\u00fcbingen" in output.decode("utf-8")
