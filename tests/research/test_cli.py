from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from conftest import candidate_data, pass_data, write_json

from histgerm.catalog import load_catalog
from histgerm.research.__main__ import main
from histgerm.research.discovery_orchestration import (
    DiscoveryDependencies,
    ProviderResponse,
)
from histgerm.research.inventory_vocabulary import FetchedDocument
from histgerm.research.search_providers import ResponseFormat, SearchRequest
from histgerm.research.vocabulary_store import (
    DiscoveryVocabulary,
    serialize_vocabulary,
)


def run_cli(*arguments: str) -> tuple[int, dict[str, Any]]:
    import contextlib
    import io

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = main(list(arguments))
    return code, json.loads(output.getvalue())


def run_discovery_cli(
    dependencies: DiscoveryDependencies, *arguments: str
) -> tuple[int, dict[str, Any]]:
    import contextlib
    import io

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = main(list(arguments), discovery_dependencies=dependencies)
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


def test_discover_cli_uses_injected_orchestration_without_ledger_mutation(
    tmp_path: Path,
) -> None:
    calls = 0

    def provider(request: SearchRequest) -> ProviderResponse:
        nonlocal calls
        calls += 1
        body = (
            "<rss><channel/></rss>"
            if request.response_format is ResponseFormat.RSS
            else "<main>No results</main>"
        )
        return ProviderResponse(
            retrieval_mode="bounded_http",
            observed_at=datetime(2026, 8, 12, tzinfo=UTC),
            http_status=200,
            body=body,
        )

    responses = iter(['{"candidates":[]}', '{"candidates":[]}'])
    dependencies = DiscoveryDependencies(
        catalog=load_catalog(),
        model_call=lambda prompt: next(responses),
        vocabulary_transport=lambda url, *, max_bytes: FetchedDocument(
            url, "text/plain", b"Middle High German corpus"
        ),
        provider_fetch=provider,
        result_inspector=lambda result: ("unrelated", "offline fixture"),
    )
    ledger = tmp_path / "must-not-exist.yaml"
    code, response = run_discovery_cli(
        dependencies,
        "discover",
        "--category",
        "corpus",
        "--stage",
        "mhg",
        "--max-mined-terms",
        "0",
        "--max-exclusion-groups",
        "1",
    )
    assert code == 0
    assert response["command"] == "discover"
    assert response["result"]["metrics"]["model_leads"] == 0
    assert calls > 0
    assert not ledger.exists()

    code, response = run_cli("discover", "--category", "corpus", "--stage", "mhg")
    assert code == 6
    assert response["errors"][0]["code"] == "capability_unavailable"


def test_vocabulary_validate_status_and_yaml_apply(tmp_path: Path) -> None:
    vocabulary = tmp_path / "vocabulary.yaml"
    initial = DiscoveryVocabulary(
        schema_version=1,
        revision=0,
        updated_on="2026-08-12",
        sources=[],
        terms=[],
    )
    vocabulary.write_bytes(serialize_vocabulary(initial))

    for command in ("vocabulary-validate", "vocabulary-status"):
        code, response = run_cli(command, "--vocabulary", str(vocabulary))
        assert code == 0
        assert response["command"] == command
        assert response["revision"] == 0

    payload = tmp_path / "vocabulary-update.yaml"
    payload.write_bytes(serialize_vocabulary(initial))
    code, response = run_cli(
        "vocabulary-apply",
        "--vocabulary",
        str(vocabulary),
        "--expected-revision",
        "0",
        "--input",
        str(payload),
    )
    assert code == 0
    assert response["revision"] == 1
    assert response["result"]["terms"] == 0


def test_vocabulary_cli_reports_stale_and_invalid_files(tmp_path: Path) -> None:
    vocabulary = tmp_path / "vocabulary.yaml"
    initial = DiscoveryVocabulary(
        schema_version=1,
        revision=0,
        updated_on="2026-08-12",
        sources=[],
        terms=[],
    )
    vocabulary.write_bytes(serialize_vocabulary(initial))
    payload = tmp_path / "vocabulary.json"
    payload.write_text(
        json.dumps(initial.model_dump(mode="json")),
        encoding="utf-8",
    )

    code, response = run_cli(
        "vocabulary-apply",
        "--vocabulary",
        str(vocabulary),
        "--expected-revision",
        "1",
        "--input",
        str(payload),
    )
    assert code == 3
    assert response["errors"][0]["code"] == "stale_revision"

    vocabulary.write_text(
        "schema_version: 1\nrevision: 0\nrevision: 1\n"
        "updated_on: 2026-08-12\nsources: []\nterms: []\n",
        encoding="utf-8",
    )
    code, response = run_cli("vocabulary-validate", "--vocabulary", str(vocabulary))
    assert code == 2
    assert response["errors"][0]["code"] == "invalid_vocabulary"
