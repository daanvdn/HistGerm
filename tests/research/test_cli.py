from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from conftest import candidate_data, pass_data, write_json

from histgerm.catalog import load_catalog
from histgerm.models import LanguageStage
from histgerm.research import __main__ as research_main
from histgerm.research.__main__ import main
from histgerm.research.discovery_orchestration import (
    DiscoveryDependencies,
    ProviderResponse,
)
from histgerm.research.discovery_protocol import (
    CHECKPOINT_SCHEMA_VERSION,
    DiscoveryExchange,
    ModelElicitationRequest,
    ModelElicitationResponse,
    write_checkpoint,
)
from histgerm.research.discovery_session import NeedsInput, new_checkpoint
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


def test_resume_failure_preserves_checkpoint_for_one_correction(
    tmp_path: Path, monkeypatch: Any
) -> None:
    checkpoint_path = tmp_path / "checkpoint.json"
    response_path = tmp_path / "response.json"
    checkpoint = new_checkpoint(
        category="corpus",
        stage=LanguageStage.OHG,
        max_mined_terms=0,
        max_exclusion_groups=1,
    )
    request = ModelElicitationRequest(
        request_id=f"{checkpoint.run_id}:elicitation:one",
        iteration=1,
        prompt_kind="broad",
        prompt="prompt",
        max_output_chars=100,
        max_candidates=5,
    )
    checkpoint = checkpoint.model_copy(update={"revision": 1, "pending": [request]})
    write_checkpoint(checkpoint_path, checkpoint)
    original_checkpoint = checkpoint_path.read_bytes()

    def write_response(output: str) -> None:
        response_path.write_text(
            DiscoveryExchange(
                schema_version=CHECKPOINT_SCHEMA_VERSION,
                run_id=checkpoint.run_id,
                checkpoint_revision=1,
                responses=[
                    ModelElicitationResponse(
                        kind="model_elicitation",
                        request_id=request.request_id,
                        output=output,
                    )
                ],
            ).model_dump_json(),
            encoding="utf-8",
        )

    write_response('{"candidates":[]}')
    monkeypatch.setattr(
        research_main,
        "advance",
        lambda checkpoint, capabilities: (_ for _ in ()).throw(
            ValueError("correctable response failure")
        ),
    )
    code, payload = run_cli(
        "discover",
        "--resume",
        str(checkpoint_path),
        "--input",
        str(response_path),
    )
    assert code == 2
    assert payload["errors"][0]["message"] == "correctable response failure"
    assert checkpoint_path.read_bytes() == original_checkpoint
    assert not response_path.exists()

    next_request = request.model_copy(
        update={
            "request_id": f"{checkpoint.run_id}:elicitation:two",
            "iteration": 2,
            "prompt_kind": "follow_up",
            "prompt": "prompt two",
        }
    )

    def pause_after_correction(applied: Any, capabilities: Any) -> NeedsInput:
        assert applied.pending == []
        assert applied.consumed_request_ids == [request.request_id]
        updated = applied.model_copy(update={"revision": 2, "pending": [next_request]})
        return NeedsInput(updated, (next_request,))

    write_response('{"candidates":[{"name":"Corrected","aliases":[]}]}')
    monkeypatch.setattr(research_main, "advance", pause_after_correction)
    code, payload = run_cli(
        "discover",
        "--resume",
        str(checkpoint_path),
        "--input",
        str(response_path),
    )
    assert code == 0
    assert payload["checkpoint_revision"] == 2
    assert not response_path.exists()

    write_response('{"candidates":[]}')
    code, payload = run_cli(
        "discover",
        "--resume",
        str(checkpoint_path),
        "--input",
        str(response_path),
    )
    assert code == 2
    assert payload["errors"][0]["code"] == "invalid_exchange"
    assert "revision" in payload["errors"][0]["message"]
    assert not response_path.exists()


def test_terminal_initial_discovery_failure_removes_operational_files(
    tmp_path: Path, monkeypatch: Any
) -> None:
    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint_path.write_text("stale", encoding="utf-8")
    checkpoint = new_checkpoint(category="corpus", stage=LanguageStage.OHG)
    monkeypatch.setattr(
        research_main,
        "advance",
        lambda checkpoint, capabilities: (_ for _ in ()).throw(
            RuntimeError("terminal runtime failure")
        ),
    )

    try:
        research_main._advance_discovery(checkpoint, checkpoint_path, None)
    except RuntimeError as error:
        assert str(error) == "terminal runtime failure"
    else:
        raise AssertionError("terminal runtime failure was not raised")

    assert not checkpoint_path.exists()


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
