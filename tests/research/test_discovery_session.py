from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from synthetic_transport import LEAD_URL, inspect_result, model_answer, synthetic_fetch

from histgerm.catalog import load_catalog
from histgerm.models import LanguageStage
from histgerm.research.discovery_orchestration import DiscoveryDependencies
from histgerm.research.discovery_protocol import (
    CHECKPOINT_SCHEMA_VERSION,
    DiscoveryCheckpoint,
    DiscoveryExchange,
    ModelElicitationRequest,
    ResultInspectionRequest,
)
from histgerm.research.discovery_runtime import RuntimeCapabilities
from histgerm.research.discovery_runtime import (
    load_runtime_capabilities as load_capabilities,
)
from histgerm.research.discovery_session import (
    Completed,
    NeedsInput,
    advance,
    apply_exchange,
    checkpoint_config,
    new_checkpoint,
)
from histgerm.research.search_providers import SearchResult

RUN_ON = date(2026, 8, 12)


def capabilities(calls: list[str] | None = None) -> RuntimeCapabilities:
    def fetch(url: str, /, *, max_bytes: int) -> Any:
        if calls is not None:
            calls.append(url)
        return synthetic_fetch(url, max_bytes=max_bytes)

    return load_capabilities(
        fetch=fetch, clock=lambda: datetime(2026, 8, 12, tzinfo=UTC)
    )


def start() -> DiscoveryCheckpoint:
    return new_checkpoint(
        category="tool",
        stage=LanguageStage.MHG,
        max_mined_terms=0,
        max_exclusion_groups=1,
        run_on=RUN_ON,
    )


def answer(step: NeedsInput) -> DiscoveryExchange:
    responses: list[dict[str, Any]] = []
    for request in step.requests:
        if isinstance(request, ModelElicitationRequest):
            responses.append(
                {
                    "kind": "model_elicitation",
                    "request_id": request.request_id,
                    "output": model_answer(request.prompt),
                }
            )
            continue
        assert isinstance(request, ResultInspectionRequest)
        responses.append(
            {
                "kind": "result_inspection",
                "request_id": request.request_id,
                "verdicts": [
                    {
                        "position": item.position,
                        "classification": inspect_result(
                            SearchResult(
                                item.position, item.url, item.title, item.snippet
                            )
                        )[0],
                        "reason": inspect_result(
                            SearchResult(
                                item.position, item.url, item.title, item.snippet
                            )
                        )[1],
                    }
                    for item in request.items
                ],
            }
        )
    return DiscoveryExchange.model_validate(
        {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "run_id": step.checkpoint.run_id,
            "checkpoint_revision": step.checkpoint.revision,
            "responses": responses,
        }
    )


def drive(
    runtime: RuntimeCapabilities,
) -> tuple[dict[str, object], int, DiscoveryCheckpoint]:
    checkpoint = start()
    rounds = 0
    last = checkpoint
    while True:
        step = advance(checkpoint, runtime)
        if isinstance(step, Completed):
            return step.result.as_json(), rounds, last
        rounds += 1
        assert step.checkpoint.revision == rounds
        last = step.checkpoint
        checkpoint = apply_exchange(step.checkpoint, answer(step))


def test_resumable_loop_matches_the_injected_in_process_run() -> None:
    runtime = capabilities()
    resumed, rounds, _ = drive(runtime)
    injected = advance(
        start(),
        dependencies=DiscoveryDependencies(
            catalog=load_catalog(),
            model_call=model_answer,
            provider_fetch=runtime.provider_fetch,
            result_inspector=inspect_result,
            vocabulary_transport=runtime.vocabulary_transport,
        ),
    )
    assert isinstance(injected, Completed)
    assert rounds >= 3
    assert json.dumps(resumed, sort_keys=True) == json.dumps(
        injected.result.as_json(), sort_keys=True
    )
    assert {lead["name"] for lead in resumed["model_leads"]} == {"MhgBERT"}
    assert any(
        inspection["classification"] == "lead"
        for assessment in resumed["assessments"]
        for inspection in assessment["inspections"]
    )


def test_resume_never_repeats_confirmed_retrieval() -> None:
    resumed_calls: list[str] = []
    drive(capabilities(resumed_calls))
    injected_calls: list[str] = []
    runtime = capabilities(injected_calls)
    advance(
        start(),
        dependencies=DiscoveryDependencies(
            catalog=load_catalog(),
            model_call=model_answer,
            provider_fetch=runtime.provider_fetch,
            result_inspector=inspect_result,
            vocabulary_transport=runtime.vocabulary_transport,
        ),
    )
    resumed = Counter(resumed_calls)
    injected = Counter(injected_calls)
    catalog_urls = [url for url in resumed if "?" not in url]
    assert catalog_urls and all(resumed[url] == 1 for url in catalog_urls)
    assert all(resumed[url] <= injected[url] + 1 for url in resumed)
    assert sum(resumed.values()) - sum(injected.values()) <= 5


def test_checkpoint_only_retains_normalized_state() -> None:
    _, rounds, last = drive(capabilities())
    payload = json.dumps(last.model_dump(mode="json"))
    assert rounds >= 4
    assert last.vocabulary is not None and last.executions
    assert "<a href" not in payload and "<rss" not in payload
    assert "Authorization" not in payload and "Cookie" not in payload
    assert LEAD_URL in payload


def test_subprocess_cli_completes_discovery_without_injected_callbacks(
    tmp_path: Path,
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(
        "import histgerm.research.discovery_runtime as runtime\n"
        "from synthetic_transport import synthetic_fetch\n"
        "runtime.fetch_public_metadata = synthetic_fetch\n",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join((str(site), str(Path(__file__).parent)))
    checkpoint = tmp_path / "discover.json"
    response = tmp_path / "response.json"

    def run(*arguments: str) -> dict[str, Any]:
        completed = subprocess.run(
            [sys.executable, "-m", "histgerm.research", *arguments],
            capture_output=True,
            text=True,
            env=environment,
            cwd=Path(__file__).parents[2],
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        return json.loads(completed.stdout)

    payload = run(
        "discover",
        "--category",
        "tool",
        "--stage",
        "mhg",
        "--max-mined-terms",
        "0",
        "--max-exclusion-groups",
        "1",
        "--checkpoint",
        str(checkpoint),
    )
    rounds = 0
    while payload["state"] == "needs_input":
        rounds += 1
        assert payload["checkpoint_revision"] == rounds
        assert checkpoint.exists()
        response.write_text(
            json.dumps(
                {
                    "schema_version": CHECKPOINT_SCHEMA_VERSION,
                    "run_id": payload["run_id"],
                    "checkpoint_revision": payload["checkpoint_revision"],
                    "responses": [
                        _response(request) for request in payload["requests"]
                    ],
                }
            ),
            encoding="utf-8",
        )
        payload = run("discover", "--resume", str(checkpoint), "--input", str(response))
        assert not response.exists()
    assert payload["state"] == "complete"
    assert not checkpoint.exists()
    assert payload["result"]["model_leads"] == [
        {"name": "MhgBERT", "aliases": ["MHG BERT"]}
    ]
    assert rounds >= 3


def _response(request: dict[str, Any]) -> dict[str, Any]:
    if request["kind"] == "model_elicitation":
        return {
            "kind": "model_elicitation",
            "request_id": request["request_id"],
            "output": model_answer(request["prompt"]),
        }
    return {
        "kind": "result_inspection",
        "request_id": request["request_id"],
        "verdicts": [
            {
                "position": item["position"],
                "classification": inspect_result(
                    SearchResult(
                        item["position"], item["url"], item["title"], item["snippet"]
                    )
                )[0],
                "reason": inspect_result(
                    SearchResult(
                        item["position"], item["url"], item["title"], item["snippet"]
                    )
                )[1],
            }
            for item in request["items"]
        ],
    }


def test_stale_checkpoint_configuration_is_preserved(tmp_path: Path) -> None:
    checkpoint = start()
    assert checkpoint_config(checkpoint).max_mined_terms == 0
    assert checkpoint_config(checkpoint).run_on == RUN_ON
