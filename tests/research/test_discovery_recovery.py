"""Synthetic failure and restart coverage for resumable protocol recovery.

These tests exercise every ``GATE-RECOVERY`` requirement end to end through the
resumable capability-exchange state machine: evidence-grounded dispositions
(``TASK-MIG-002``), canonical structured query intents (``TASK-MIG-003``),
candidate-local model-output recovery and its single retry (``TASK-MIG-005``),
and the resumable protocol failures introduced by ``TASK-MIG-006`` -- missing
inspection positions, smaller-batch retries, provider transport gaps, stale or
future revisions, and resume without repeating confirmed retrieval.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError
from synthetic_transport import (
    LEAD_TITLE,
    LEAD_URL,
    PROVIDER_HOSTS,
    inspect_result,
    model_answer,
    synthetic_fetch,
)

from histgerm.models import LanguageStage
from histgerm.research import discovery_session, query_intents
from histgerm.research.discovery_protocol import (
    CHECKPOINT_SCHEMA_VERSION,
    DiscoveryCheckpoint,
    DiscoveryExchange,
    DiscoveryProtocolError,
    ModelElicitationRequest,
    ResultInspectionRequest,
    StaleCheckpointError,
)
from histgerm.research.discovery_runtime import (
    MetadataFetch,
    RuntimeCapabilities,
    load_runtime_capabilities,
)
from histgerm.research.discovery_session import (
    Completed,
    NeedsInput,
    advance,
    apply_exchange,
    new_checkpoint,
)
from histgerm.research.fetching import FetchedMetadata, MetadataFetchError
from histgerm.research.models import CandidateResearchResult
from histgerm.research.search_providers import SearchResult

RUN_ON = date(2026, 8, 12)
_MODEL_LEADS = [{"name": "MhgBERT", "aliases": ["MHG BERT"]}]

Respond = Callable[[NeedsInput], list[dict[str, Any]]]


def _capabilities(fetch: MetadataFetch) -> RuntimeCapabilities:
    return load_runtime_capabilities(
        fetch=fetch, clock=lambda: datetime(2026, 8, 12, tzinfo=UTC)
    )


def _start() -> DiscoveryCheckpoint:
    return new_checkpoint(
        category="tool",
        stage=LanguageStage.MHG,
        max_mined_terms=0,
        max_exclusion_groups=1,
        run_on=RUN_ON,
    )


def _multi_link_fetch(
    extra_links: int, calls: list[str] | None = None
) -> MetadataFetch:
    """Return one offline transport whose provider pages hold multiple items."""

    def fetch(url: str, /, *, max_bytes: int) -> FetchedMetadata:
        if calls is not None:
            calls.append(url)
        if urlsplit(url).netloc in PROVIDER_HOSTS and "format=rss" not in url:
            anchors = [f'<a href="{LEAD_URL}">{LEAD_TITLE}</a>']
            anchors.extend(
                f'<a href="https://example.org/unrelated-{index}">Unrelated {index}</a>'
                for index in range(extra_links)
            )
            return FetchedMetadata(url, "text/html", "".join(anchors).encode("utf-8"))
        return synthetic_fetch(url, max_bytes=max_bytes)

    return fetch


def _elicitation_response(
    request: ModelElicitationRequest, model: Callable[[str], str]
) -> dict[str, Any]:
    return {
        "kind": "model_elicitation",
        "request_id": request.request_id,
        "output": model(request.prompt),
    }


def _inspection_response(
    request: ResultInspectionRequest, held: frozenset[int] = frozenset()
) -> dict[str, Any]:
    verdicts: list[dict[str, Any]] = []
    for item in request.items:
        if item.position in held:
            continue
        classification, reason = inspect_result(
            SearchResult(item.position, item.url, item.title, item.snippet)
        )
        verdicts.append(
            {
                "position": item.position,
                "classification": classification,
                "reason": reason,
            }
        )
    return {
        "kind": "result_inspection",
        "request_id": request.request_id,
        "verdicts": verdicts,
    }


def _answer_all(step: NeedsInput) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []
    for request in step.requests:
        if isinstance(request, ModelElicitationRequest):
            responses.append(_elicitation_response(request, model_answer))
        else:
            responses.append(_inspection_response(request))
    return responses


def _exchange(step: NeedsInput, responses: list[dict[str, Any]]) -> DiscoveryExchange:
    return DiscoveryExchange.model_validate(
        {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "run_id": step.checkpoint.run_id,
            "checkpoint_revision": step.checkpoint.revision,
            "responses": responses,
        }
    )


def _run_to_completion(
    runtime: RuntimeCapabilities, respond: Respond
) -> tuple[dict[str, object], int]:
    checkpoint = _start()
    rounds = 0
    while True:
        step = advance(checkpoint, runtime)
        if isinstance(step, Completed):
            return step.result.as_json(), rounds
        rounds += 1
        assert step.checkpoint.revision == rounds
        checkpoint = apply_exchange(step.checkpoint, _exchange(step, respond(step)))


class _HoldOnePosition:
    """Answer inspection requests fully except the first large batch, once."""

    def __init__(self) -> None:
        self.original_items: int | None = None
        self.retry_items: int | None = None
        self._held: tuple[str, str, str | None] | None = None

    def __call__(self, step: NeedsInput) -> list[dict[str, Any]]:
        responses: list[dict[str, Any]] = []
        for request in step.requests:
            if isinstance(request, ModelElicitationRequest):
                responses.append(_elicitation_response(request, model_answer))
                continue
            if self.original_items is None and len(request.items) >= 2:
                held = request.items[-1]
                self.original_items = len(request.items)
                self._held = (held.url, held.title, held.snippet)
                responses.append(
                    _inspection_response(request, held=frozenset({held.position}))
                )
                continue
            if (
                self.retry_items is None
                and self._held is not None
                and any(
                    (item.url, item.title, item.snippet) == self._held
                    for item in request.items
                )
            ):
                self.retry_items = len(request.items)
            responses.append(_inspection_response(request))
        return responses


def test_evidence_grounded_dispositions_stay_strict() -> None:
    """TASK-MIG-002 grounding: silence, out-of-scope, and identity ambiguity."""

    with pytest.raises(ValidationError, match="out_of_scope requires direct evidence"):
        CandidateResearchResult(
            candidate_id="candidate-example",
            category="corpus",
            disposition="out_of_scope",
            verified_stages=[],
            evidence=[],
            evidence_gaps=[],
            risk_flags=[],
            summary="No canonical stage evidence was found.",
        )
    blocked = CandidateResearchResult(
        candidate_id="candidate-example",
        category="corpus",
        disposition="blocked",
        verified_stages=[],
        evidence=[],
        evidence_gaps=["No canonical stage evidence was found."],
        risk_flags=[],
        summary="Blocked pending canonical stage evidence.",
    )
    assert blocked.disposition == "blocked"
    with pytest.raises(ValidationError, match="identity_conflict risk flag"):
        CandidateResearchResult(
            candidate_id="candidate-example",
            category="corpus",
            disposition="blocked",
            verified_stages=[],
            evidence=[],
            evidence_gaps=["A competing identity match was found."],
            matched_resource_id="corpus-reference",
            risk_flags=[],
            summary="Ambiguous identity pending resolution.",
        )
    duplicate = CandidateResearchResult(
        candidate_id="candidate-example",
        category="corpus",
        disposition="duplicate",
        verified_stages=[],
        evidence=[],
        evidence_gaps=[],
        matched_resource_id="corpus-reference",
        risk_flags=[],
        summary="Duplicate of an existing corpus.",
    )
    assert duplicate.matched_resource_id == "corpus-reference"


def test_structured_query_intent_registry_stays_canonical() -> None:
    """TASK-MIG-003 grounding: the canonical intent registry stays intact."""

    snapshot = query_intents.registry_snapshot()
    matrix = query_intents.coverage_matrix()
    assert snapshot["intents"] and matrix
    required = query_intents.required_intent_ids("tool", "mhg")
    assert required
    assert all(query_intents.is_registered_intent(intent) for intent in required)
    assert matrix["tool-mhg"] == sorted(required)


def test_malformed_sibling_is_quarantined_without_discarding_valid_lead() -> None:
    """TASK-MIG-005 retention through the resumable protocol."""

    def sibling_model(prompt: str) -> str:
        if "additional plausible" in prompt:
            return json.dumps({"candidates": []})
        return json.dumps(
            {
                "candidates": [
                    {"name": "MhgBERT", "aliases": ["MHG BERT"]},
                    {"name": "BrokenSibling", "note": "extra field is not allowed"},
                ]
            }
        )

    def respond(step: NeedsInput) -> list[dict[str, Any]]:
        return [
            _elicitation_response(request, sibling_model)
            if isinstance(request, ModelElicitationRequest)
            else _inspection_response(request)
            for request in step.requests
        ]

    result, _ = _run_to_completion(_capabilities(synthetic_fetch), respond)
    metrics = result["metrics"]
    assert result["model_leads"] == _MODEL_LEADS
    assert isinstance(metrics, dict)
    assert metrics["elicitation_quarantined_candidates"] >= 1


def test_malformed_response_formatting_is_recovered_by_one_retry() -> None:
    """TASK-MIG-005 retry: invalid formatting recovers through the protocol."""

    def retry_model(prompt: str) -> str:
        if "must be corrected" in prompt:
            return json.dumps(
                {"candidates": [{"name": "MhgBERT", "aliases": ["MHG BERT"]}]}
            )
        if "additional plausible" in prompt:
            return json.dumps({"candidates": []})
        return "this text is not JSON at all"

    retry_prompts: list[str] = []

    def respond(step: NeedsInput) -> list[dict[str, Any]]:
        responses: list[dict[str, Any]] = []
        for request in step.requests:
            if isinstance(request, ModelElicitationRequest):
                if "must be corrected" in request.prompt:
                    retry_prompts.append(request.request_id)
                responses.append(_elicitation_response(request, retry_model))
            else:
                responses.append(_inspection_response(request))
        return responses

    result, _ = _run_to_completion(_capabilities(synthetic_fetch), respond)
    metrics = result["metrics"]
    assert result["model_leads"] == _MODEL_LEADS
    assert retry_prompts, "the invalid broad response must surface a retry request"
    assert isinstance(metrics, dict)
    assert metrics["elicitation_retries"] >= 1
    assert metrics["elicitation_recovered_retries"] >= 1


def test_large_inspection_page_is_split_into_bounded_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TASK-MIG-006: oversized inspection pages split into bounded batches."""

    monkeypatch.setattr(discovery_session, "INSPECTION_BATCH_LIMIT", 3)
    runtime = _capabilities(_multi_link_fetch(4))
    checkpoint = _start()
    request_ids: list[str] = []
    max_batch = 0
    saw_split = False
    result: dict[str, object] | None = None
    while True:
        step = advance(checkpoint, runtime)
        if isinstance(step, Completed):
            result = step.result.as_json()
            break
        inspections = [
            request
            for request in step.requests
            if isinstance(request, ResultInspectionRequest)
        ]
        for request in inspections:
            assert len(request.items) <= 3
            max_batch = max(max_batch, len(request.items))
            request_ids.append(request.request_id)
        if len(inspections) >= 2:
            saw_split = True
            assert sum(len(request.items) for request in inspections) == 5
        checkpoint = apply_exchange(step.checkpoint, _exchange(step, _answer_all(step)))
    assert saw_split
    assert max_batch == 3
    assert len(request_ids) == len(set(request_ids))
    assert result is not None
    assert result["model_leads"] == _MODEL_LEADS


def test_missing_inspection_positions_recover_via_smaller_batch_retry() -> None:
    """TASK-MIG-006: a partial inspection answer resumes without restarting."""

    baseline_calls: list[str] = []
    baseline, _ = _run_to_completion(
        _capabilities(_multi_link_fetch(2, baseline_calls)), _answer_all
    )

    partial_calls: list[str] = []
    hold = _HoldOnePosition()
    partial, _ = _run_to_completion(
        _capabilities(_multi_link_fetch(2, partial_calls)), hold
    )

    assert hold.original_items == 3
    assert hold.retry_items is not None
    assert hold.retry_items < hold.original_items
    assert partial == baseline
    assert partial["model_leads"] == _MODEL_LEADS
    assert Counter(partial_calls) == Counter(baseline_calls)
    catalog_urls = [url for url in partial_calls if "?" not in url]
    assert catalog_urls
    assert all(Counter(partial_calls)[url] == 1 for url in catalog_urls)


def test_provider_transport_gap_does_not_abort_other_channels() -> None:
    """TASK-MIG-006: one provider gap is structured; other channels proceed."""

    def fetch(url: str, /, *, max_bytes: int) -> FetchedMetadata:
        if urlsplit(url).netloc == "search.brave.com":
            raise MetadataFetchError(
                "metadata request returned HTTP 429",
                stage="rate_limit",
                status=429,
            )
        return synthetic_fetch(url, max_bytes=max_bytes)

    result, _ = _run_to_completion(_capabilities(fetch), _answer_all)
    assessments = result["assessments"]
    assert isinstance(assessments, list)
    assert result["model_leads"] == _MODEL_LEADS
    gaps = [
        record
        for record in assessments
        if record["assessment"] in {"access_gap", "transport_error"}
    ]
    assert any(record["channel"] == "general_web_brave" for record in gaps)
    assert any(
        inspection["classification"] == "lead"
        for record in assessments
        for inspection in record["inspections"]
    )


def _pending_checkpoint() -> DiscoveryCheckpoint:
    value = _start()
    return value.model_copy(
        update={
            "revision": 2,
            "pending": [
                ModelElicitationRequest(
                    request_id=f"{value.run_id}:elicitation:one",
                    iteration=1,
                    prompt_kind="broad",
                    prompt="prompt",
                    max_output_chars=100,
                    max_candidates=5,
                )
            ],
        }
    )


def _elicitation_exchange(
    value: DiscoveryCheckpoint, revision: int
) -> DiscoveryExchange:
    return DiscoveryExchange.model_validate(
        {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "run_id": value.run_id,
            "checkpoint_revision": revision,
            "responses": [
                {
                    "kind": "model_elicitation",
                    "request_id": value.pending[0].request_id,
                    "output": '{"candidates":[]}',
                }
            ],
        }
    )


def test_stale_or_future_revision_returns_actionable_state() -> None:
    """TASK-MIG-006: revision mismatch yields recoverable state, not discard."""

    value = _pending_checkpoint()
    for revision in (1, 3):
        with pytest.raises(StaleCheckpointError) as caught:
            apply_exchange(value, _elicitation_exchange(value, revision))
        error = caught.value
        assert isinstance(error, DiscoveryProtocolError)
        assert "revision" in str(error)
        assert error.run_id == value.run_id
        assert error.expected_revision == value.revision == 2
        assert error.requests == tuple(value.pending)
    assert value.pending
    assert value.consumed_request_ids == []


def test_wrong_run_identity_remains_fatal_and_is_not_stale() -> None:
    """TASK-MIG-006: a wrong run id is fatal before any revision recovery."""

    value = _pending_checkpoint()
    exchange = _elicitation_exchange(value, value.revision).model_copy(
        update={"run_id": "0" * 16}
    )
    with pytest.raises(DiscoveryProtocolError, match="run identifier") as caught:
        apply_exchange(value, exchange)
    assert not isinstance(caught.value, StaleCheckpointError)
