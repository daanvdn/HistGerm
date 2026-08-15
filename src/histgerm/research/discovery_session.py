"""Resumable capability-exchange state machine for command-line discovery.

The deterministic phases stay inside the CLI. When a run reaches a model
elicitation or item inspection boundary it serializes one bounded request,
persists confirmed phase results, and pauses. A later invocation validates and
consumes the coordinator response exactly once and continues from the next
phase without repeating confirmed retrieval.
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from histgerm.models import LanguageStage

from .discovery_orchestration import (
    DiscoveryConfig,
    DiscoveryDependencies,
    DiscoveryRunResult,
    IncrementalVocabulary,
    run_discovery,
)
from .discovery_protocol import (
    CHECKPOINT_SCHEMA_VERSION,
    CapabilityRequest,
    DiscoveryCheckpoint,
    DiscoveryExchange,
    DiscoveryProtocolError,
    ElicitationRecord,
    ExecutionRecord,
    InspectionItem,
    InspectionRecord,
    ModelElicitationRequest,
    ModelElicitationResponse,
    ResultInspectionRequest,
    ResultInspectionResponse,
    RunParameters,
    decode_records,
    decode_vocabulary,
    encode_records,
    encode_vocabulary,
    item_digest,
    prompt_digest,
    request_id,
)
from .discovery_runtime import RuntimeCapabilities
from .models import CandidateEntry, ResourceCategory
from .search_providers import (
    ResultClassification,
    SearchAssessmentRecord,
    SearchResult,
)

_DEFERRED_REASON = "inspection pending"


class _Pause(BaseException):
    """Interrupt one run at a capability boundary without being swallowed."""

    def __init__(self, requests: tuple[CapabilityRequest, ...]) -> None:
        self.requests = requests
        super().__init__("discovery requires a bounded capability response")


@dataclass(frozen=True, slots=True)
class NeedsInput:
    """One paused run awaiting bounded coordinator judgments."""

    checkpoint: DiscoveryCheckpoint
    requests: tuple[CapabilityRequest, ...]


@dataclass(frozen=True, slots=True)
class Completed:
    """One finished run and its final transient result."""

    result: DiscoveryRunResult


type SessionStep = NeedsInput | Completed


def new_checkpoint(
    *,
    category: ResourceCategory,
    stage: LanguageStage,
    qualifiers: Sequence[str] = (),
    max_mined_terms: int = 8,
    max_exclusion_groups: int = 2,
    run_on: date | None = None,
) -> DiscoveryCheckpoint:
    """Create one empty, unwritten checkpoint for a new discovery run."""

    parameters = RunParameters(
        category=category,
        stage=stage.value,
        qualifiers=list(qualifiers),
        max_mined_terms=max_mined_terms,
        max_exclusion_groups=max_exclusion_groups,
        run_on=(run_on or date.today()).isoformat(),
    )
    return DiscoveryCheckpoint(
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        run_id=secrets.token_hex(8),
        revision=0,
        parameters_digest=parameters.digest(),
        parameters=parameters,
    )


def checkpoint_config(checkpoint: DiscoveryCheckpoint) -> DiscoveryConfig:
    """Rebuild the immutable run configuration recorded in a checkpoint."""

    if checkpoint.parameters.digest() != checkpoint.parameters_digest:
        raise DiscoveryProtocolError(
            "checkpoint run parameters do not match their recorded digest"
        )
    parameters = checkpoint.parameters
    return DiscoveryConfig(
        category=parameters.category,
        stage=LanguageStage(parameters.stage),
        qualifiers=tuple(parameters.qualifiers),
        max_mined_terms=parameters.max_mined_terms,
        max_exclusion_groups=parameters.max_exclusion_groups,
        run_on=date.fromisoformat(parameters.run_on),
    )


def advance(
    checkpoint: DiscoveryCheckpoint,
    capabilities: RuntimeCapabilities | None = None,
    *,
    ledger_candidates: Sequence[CandidateEntry] = (),
    dependencies: DiscoveryDependencies | None = None,
) -> SessionStep:
    """Replay confirmed phases and continue until completion or the next pause.

    Supply ``capabilities`` for the resumable protocol or ``dependencies`` for
    one fully injected in-process run; both routes share this entry point.
    """

    config = checkpoint_config(checkpoint)
    memo: _Memo | None = None
    if dependencies is None:
        if capabilities is None:
            raise DiscoveryProtocolError(
                "discovery requires runtime capabilities or injected dependencies"
            )
        memo = _Memo(checkpoint, config)
        dependencies = DiscoveryDependencies(
            catalog=capabilities.catalog,
            model_call=memo.model_call,
            provider_fetch=capabilities.provider_fetch,
            result_inspector=memo.inspect,
            vocabulary_transport=capabilities.vocabulary_transport,
            ledger_candidates=ledger_candidates,
            memo=memo,
        )
    try:
        result = run_discovery(config, dependencies)
    except _Pause as pause:
        if memo is None:
            raise
        return NeedsInput(memo.paused(pause.requests), pause.requests)
    return Completed(result)


def apply_exchange(
    checkpoint: DiscoveryCheckpoint,
    exchange: DiscoveryExchange,
) -> DiscoveryCheckpoint:
    """Validate and consume every pending response exactly once."""

    if exchange.run_id != checkpoint.run_id:
        raise DiscoveryProtocolError("response run identifier does not match")
    if exchange.checkpoint_revision != checkpoint.revision:
        raise DiscoveryProtocolError(
            f"response targets checkpoint revision {exchange.checkpoint_revision} "
            f"but the checkpoint is at revision {checkpoint.revision}"
        )
    if not checkpoint.pending:
        raise DiscoveryProtocolError("checkpoint has no pending capability request")
    pending = {request.request_id: request for request in checkpoint.pending}
    consumed = set(checkpoint.consumed_request_ids)
    answered: set[str] = set()
    elicitations = list(checkpoint.elicitations)
    inspections = list(checkpoint.inspections)
    for response in exchange.responses:
        identity = response.request_id
        if identity in consumed:
            raise DiscoveryProtocolError(
                f"request {identity} was already answered and consumed"
            )
        if identity in answered:
            raise DiscoveryProtocolError(f"request {identity} was answered twice")
        request = pending.get(identity)
        if request is None:
            raise DiscoveryProtocolError(f"request {identity} is not pending")
        answered.add(identity)
        if isinstance(response, ModelElicitationResponse):
            if not isinstance(request, ModelElicitationRequest):
                raise DiscoveryProtocolError(
                    f"request {identity} is not an elicitation"
                )
            elicitations.append(
                ElicitationRecord(
                    prompt_digest=prompt_digest(request.prompt),
                    output=response.output,
                )
            )
            continue
        if not isinstance(request, ResultInspectionRequest):
            raise DiscoveryProtocolError(f"request {identity} is not an inspection")
        inspections.extend(_inspection_records(request, response))
    missing = set(pending) - answered
    if missing:
        raise DiscoveryProtocolError(
            "responses are missing for " + ", ".join(sorted(missing))
        )
    return checkpoint.model_copy(
        update={
            "elicitations": elicitations,
            "inspections": inspections,
            "pending": [],
            "consumed_request_ids": [
                *checkpoint.consumed_request_ids,
                *sorted(pending),
            ],
        }
    )


def _inspection_records(
    request: ResultInspectionRequest,
    response: ResultInspectionResponse,
) -> list[InspectionRecord]:
    items = {item.position: item for item in request.items}
    positions = [verdict.position for verdict in response.verdicts]
    if len(set(positions)) != len(positions):
        raise DiscoveryProtocolError("inspection positions must be unique")
    if set(positions) != set(items):
        raise DiscoveryProtocolError(
            "inspection response must classify every requested position exactly once"
        )
    return [
        InspectionRecord(
            item_digest=item_digest(
                items[verdict.position].url,
                items[verdict.position].title,
                items[verdict.position].snippet,
            ),
            classification=verdict.classification,
            reason=verdict.reason,
        )
        for verdict in response.verdicts
    ]


class _Memo:
    """Replay confirmed judgments and phase results for one resumed run."""

    def __init__(
        self, checkpoint: DiscoveryCheckpoint, config: DiscoveryConfig
    ) -> None:
        self._checkpoint = checkpoint
        self._config = config
        self._elicitations = {
            record.prompt_digest: record.output for record in checkpoint.elicitations
        }
        self._inspections = {
            record.item_digest: (record.classification, record.reason)
            for record in checkpoint.inspections
        }
        self._executions = {
            record.key: decode_records(record.records)
            for record in checkpoint.executions
        }
        self._order = [record.key for record in checkpoint.executions]
        self._vocabulary: IncrementalVocabulary | None = None
        self._iteration = 0
        self._deferred: list[SearchResult] = []

    def model_call(self, prompt: str, /) -> str:
        self._iteration += 1
        digest = prompt_digest(prompt)
        output = self._elicitations.get(digest)
        if output is not None:
            return output
        raise _Pause(
            (
                ModelElicitationRequest(
                    request_id=request_id(
                        self._checkpoint.run_id, "elicitation", digest
                    ),
                    iteration=self._iteration,
                    prompt_kind="broad" if self._iteration == 1 else "follow_up",
                    prompt=prompt,
                    max_output_chars=self._config.elicitation.max_output_chars,
                    max_candidates=self._config.elicitation.max_candidates_per_response,
                ),
            )
        )

    def inspect(self, result: SearchResult) -> tuple[ResultClassification, str]:
        verdict = self._inspections.get(
            item_digest(result.url, result.title, result.snippet)
        )
        if verdict is not None:
            return verdict
        if all(
            item_digest(item.url, item.title, item.snippet)
            != item_digest(result.url, result.title, result.snippet)
            for item in self._deferred
        ):
            self._deferred.append(result)
        return "unrelated", _DEFERRED_REASON

    def cached_vocabulary(self) -> IncrementalVocabulary | None:
        state = self._checkpoint.vocabulary
        if state is None:
            return None
        self._vocabulary = IncrementalVocabulary(
            vocabulary=decode_vocabulary(state),
            revision=state.revision,
            refreshed_sources=state.refreshed_sources,
            reused_sources=state.reused_sources,
            new_terms=state.new_terms,
            reused_decisions=state.reused_decisions,
            inactive_associations=state.inactive_associations,
            access_gaps=state.access_gaps,
        )
        return self._vocabulary

    def store_vocabulary(self, value: IncrementalVocabulary, /) -> None:
        self._vocabulary = value

    def cached_execution(
        self, key: str, /
    ) -> tuple[SearchAssessmentRecord, ...] | None:
        self._deferred.clear()
        return self._executions.get(key)

    def store_execution(
        self, key: str, records: tuple[SearchAssessmentRecord, ...], /
    ) -> None:
        if self._deferred:
            raise _Pause((self._inspection_request(key, records),))
        self._executions[key] = records
        self._order.append(key)

    def _inspection_request(
        self,
        key: str,
        records: tuple[SearchAssessmentRecord, ...],
    ) -> ResultInspectionRequest:
        first = records[0]
        page_key = "\x1f".join(
            (
                key,
                *(
                    item_digest(result.url, result.title, result.snippet)
                    for result in self._deferred
                ),
            )
        )
        return ResultInspectionRequest(
            request_id=request_id(self._checkpoint.run_id, "inspection", page_key),
            category=self._config.category,
            stage=self._config.stage.value,
            query=first.query,
            provider=first.provider.value,
            channel=first.channel,
            locale=first.locale,
            items=[
                InspectionItem(
                    position=position,
                    url=result.url,
                    title=result.title,
                    snippet=result.snippet,
                )
                for position, result in enumerate(self._deferred, start=1)
            ],
        )

    def paused(self, requests: tuple[CapabilityRequest, ...]) -> DiscoveryCheckpoint:
        """Return the next checkpoint revision holding confirmed phase state."""

        vocabulary = None
        if self._vocabulary is not None:
            state = encode_vocabulary(self._vocabulary.vocabulary)
            vocabulary = state.model_copy(
                update={
                    "revision": self._vocabulary.revision,
                    "refreshed_sources": self._vocabulary.refreshed_sources,
                    "reused_sources": self._vocabulary.reused_sources,
                    "new_terms": self._vocabulary.new_terms,
                    "reused_decisions": self._vocabulary.reused_decisions,
                    "inactive_associations": self._vocabulary.inactive_associations,
                    "access_gaps": self._vocabulary.access_gaps,
                }
            )
        return self._checkpoint.model_copy(
            update={
                "revision": self._checkpoint.revision + 1,
                "vocabulary": vocabulary,
                "executions": [
                    ExecutionRecord(
                        key=key, records=encode_records(self._executions[key])
                    )
                    for key in dict.fromkeys(self._order)
                ],
                "pending": list(requests),
            }
        )


__all__ = [
    "Completed",
    "NeedsInput",
    "SessionStep",
    "advance",
    "apply_exchange",
    "checkpoint_config",
    "new_checkpoint",
]
