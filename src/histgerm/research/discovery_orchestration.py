"""Executable, dependency-injected focused discovery orchestration."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from histgerm.catalog import Catalog
from histgerm.models import BaseResource, LanguageStage

from .discovery_metrics import DiscoveryCoverage
from .elicitation import (
    ElicitationConfig,
    ElicitationResult,
    ModelCall,
    elicit_candidates,
)
from .fetching import RetrievalFailureStage, RetrievalMode
from .focused_queries import (
    FocusedQuery,
    ResourceCategory,
    apply_exclusion_group,
    bounded_exclusion_groups,
    generate_focused_queries,
)
from .inventory_vocabulary import (
    BoundedClassifier,
    BoundedTransport,
    InventoryVocabulary,
    VocabularyKind,
    VocabularyLimits,
    mine_inventory_vocabulary,
)
from .models import CandidateEntry
from .search_providers import (
    ResponseFormat,
    ResultInspector,
    SearchAssessmentRecord,
    SearchProvider,
    SearchRequest,
    SearchResult,
    assess_search_response,
    build_provider_request,
)


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """One injected provider transport outcome."""

    retrieval_mode: RetrievalMode
    observed_at: datetime
    http_status: int | None
    body: str = ""
    failure_stage: RetrievalFailureStage | None = None


class ProviderFetch(Protocol):
    """Execute one exact provider request without implicit live behavior."""

    def __call__(self, request: SearchRequest, /) -> ProviderResponse: ...


@dataclass(frozen=True, slots=True)
class DiscoveryDependencies:
    """All external and trusted inputs required by one discovery run."""

    catalog: Catalog
    model_call: ModelCall
    vocabulary_transport: BoundedTransport
    provider_fetch: ProviderFetch
    result_inspector: ResultInspector
    ledger_candidates: Sequence[CandidateEntry] = ()
    vocabulary_classifier: BoundedClassifier | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryConfig:
    """Bounds and scope for one run-local discovery execution."""

    category: ResourceCategory
    stage: LanguageStage
    qualifiers: tuple[str, ...] = ()
    max_mined_terms: int = 8
    max_exclusion_groups: int = 2
    exclusion_group_size: int = 5
    exclusion_character_limit: int = 180
    elicitation: ElicitationConfig = ElicitationConfig()
    vocabulary: VocabularyLimits = VocabularyLimits()

    def __post_init__(self) -> None:
        if self.max_mined_terms < 0:
            raise ValueError("max_mined_terms must not be negative")
        if self.max_exclusion_groups < 1:
            raise ValueError("max_exclusion_groups must be positive")
        if self.exclusion_group_size < 1:
            raise ValueError("exclusion_group_size must be positive")


@dataclass(frozen=True, slots=True)
class DiscoveryRunResult:
    """Complete transient result from the enforced discovery sequence."""

    category: ResourceCategory
    stage: LanguageStage
    elicitation: ElicitationResult
    vocabulary: InventoryVocabulary
    queries: tuple[FocusedQuery, ...]
    assessments: tuple[SearchAssessmentRecord, ...]
    leads: tuple[SearchResult, ...]
    metrics: dict[str, object]

    def as_json(self) -> dict[str, object]:
        """Return a deterministic JSON-compatible run result."""

        return {
            "category": self.category,
            "stage": self.stage.value,
            "model_leads": [
                {"name": lead.name, "aliases": list(lead.aliases)}
                for lead in self.elicitation.leads
            ],
            "vocabulary": {
                "terms": len(self.vocabulary.terms),
                "gaps": [
                    {"url": gap.url, "reason": gap.reason}
                    for gap in self.vocabulary.gaps
                ],
                "fetched_pages": self.vocabulary.fetched_pages,
                "fetched_bytes": self.vocabulary.fetched_bytes,
            },
            "queries": [
                {
                    "text": query.text,
                    "language": query.language,
                    "family": query.family,
                    "qualifier": query.qualifier,
                }
                for query in self.queries
            ],
            "assessments": [
                {
                    "provider": record.provider.value,
                    "channel": record.channel,
                    "query": record.query,
                    "retrieval_mode": record.retrieval_mode,
                    "response_format": record.response_format.value,
                    "locale": record.locale,
                    "observed_at": record.observed_at.isoformat(),
                    "http_status": record.http_status,
                    "failure_stage": record.failure_stage,
                    "assessment": record.assessment,
                    "observation": record.observation,
                    "results": [
                        {
                            "position": result.position,
                            "url": result.url,
                            "title": result.title,
                            "snippet": result.snippet,
                            "trusted_evidence": result.trusted_evidence,
                        }
                        for result in record.results
                    ],
                    "inspections": [
                        {
                            "position": inspection.position,
                            "classification": inspection.classification,
                            "reason": inspection.reason,
                        }
                        for inspection in record.inspections
                    ],
                }
                for record in self.assessments
            ],
            "metrics": self.metrics,
        }


@dataclass(frozen=True, slots=True)
class _Channel:
    name: str
    provider: SearchProvider
    response_format: ResponseFormat


_REQUIRED_CHANNELS: tuple[_Channel, ...] = (
    _Channel("general_web_google", SearchProvider.GOOGLE, ResponseFormat.HTML),
    _Channel("general_web_bing", SearchProvider.BING, ResponseFormat.RSS),
    _Channel("general_web_brave", SearchProvider.BRAVE, ResponseFormat.HTML),
    _Channel("clarin", SearchProvider.CLARIN, ResponseFormat.HTML),
    _Channel("olac", SearchProvider.OLAC, ResponseFormat.HTML),
    _Channel("zenodo", SearchProvider.ZENODO, ResponseFormat.HTML),
    _Channel("institutional", SearchProvider.GOOGLE, ResponseFormat.HTML),
    _Channel("github", SearchProvider.GITHUB, ResponseFormat.HTML),
    _Channel("huggingface", SearchProvider.HUGGINGFACE, ResponseFormat.HTML),
)


def run_discovery(
    config: DiscoveryConfig,
    dependencies: DiscoveryDependencies,
) -> DiscoveryRunResult:
    """Run elicitation, mining, focused search, exclusions, and metrics in order."""

    trusted_records = _trusted_records(dependencies.catalog)
    elicitation = elicit_candidates(
        dependencies.model_call,
        category=config.category,
        stage=config.stage,
        trusted_records=trusted_records,
        ledger_candidates=dependencies.ledger_candidates,
        config=config.elicitation,
    )
    vocabulary = mine_inventory_vocabulary(
        dependencies.catalog,
        category=config.category,
        stages=(config.stage,),
        transport=dependencies.vocabulary_transport,
        classifier=dependencies.vocabulary_classifier,
        limits=config.vocabulary,
    )
    queries = _queries(config, vocabulary)
    metrics = DiscoveryCoverage(
        model_leads=len(elicitation.leads),
        inventory_terms=len(vocabulary.terms),
        inventory_leads=sum(
            term.kind
            in {
                VocabularyKind.ALIAS,
                VocabularyKind.PROJECT,
                VocabularyKind.RELATED_NAME,
            }
            for term in vocabulary.terms
        ),
    )
    assessments: list[SearchAssessmentRecord] = []
    leads: dict[str, SearchResult] = {}
    lead_queries: set[str] = set()

    for query in queries:
        for channel in _REQUIRED_CHANNELS:
            record = _execute_query(
                query.text,
                query,
                channel,
                dependencies,
            )
            assessments.append(record)
            retained = _retain_leads(record, leads)
            if retained:
                lead_queries.add(query.text)
            metrics.record_assessment(
                record,
                family=query.family,
                channel=channel.name,
                new_candidates=retained,
            )

    seen_names = [
        *(record.name for record in trusted_records),
        *(lead.name for lead in elicitation.leads),
        *(result.title for result in leads.values()),
    ]
    exclusion_groups = bounded_exclusion_groups(
        seen_names,
        max_names=config.exclusion_group_size,
        max_characters=config.exclusion_character_limit,
    )[: config.max_exclusion_groups]
    weak_queries = [query for query in queries if query.text not in lead_queries]
    for query in weak_queries:
        for group in exclusion_groups:
            excluded_text = apply_exclusion_group(query, group)
            for channel in _REQUIRED_CHANNELS:
                record = _execute_query(
                    excluded_text,
                    query,
                    channel,
                    dependencies,
                )
                assessments.append(record)
                retained = _retain_leads(record, leads)
                metrics.record_assessment(
                    record,
                    family=query.family,
                    channel=channel.name,
                    new_candidates=retained,
                )

    return DiscoveryRunResult(
        category=config.category,
        stage=config.stage,
        elicitation=elicitation,
        vocabulary=vocabulary,
        queries=queries,
        assessments=tuple(assessments),
        leads=tuple(leads.values()),
        metrics=metrics.snapshot(),
    )


def _execute_query(
    text: str,
    query: FocusedQuery,
    channel: _Channel,
    dependencies: DiscoveryDependencies,
) -> SearchAssessmentRecord:
    locale = "de-DE" if query.language == "de" else "en-US"
    request = build_provider_request(
        channel.provider,
        text,
        channel=channel.name,
        locale=locale,
        retrieval_mode="bounded_http",
        response_format=channel.response_format,
    )
    try:
        response = dependencies.provider_fetch(request)
    except Exception as error:
        response = ProviderResponse(
            retrieval_mode="bounded_http",
            observed_at=datetime.now(UTC),
            http_status=None,
            failure_stage="request",
            body=str(error),
        )
    return assess_search_response(
        provider=request.provider,
        channel=request.channel,
        query=request.query,
        retrieval_mode=response.retrieval_mode,
        response_format=request.response_format,
        locale=request.locale,
        observed_at=response.observed_at,
        http_status=response.http_status,
        failure_stage=response.failure_stage,
        body=response.body,
        inspector=dependencies.result_inspector,
    )


def _retain_leads(
    record: SearchAssessmentRecord,
    leads: dict[str, SearchResult],
) -> int:
    retained = 0
    for result, inspection in zip(record.results, record.inspections, strict=True):
        if inspection.classification != "lead":
            continue
        key = result.url.casefold()
        if key not in leads:
            leads[key] = result
            retained += 1
    return retained


def _queries(
    config: DiscoveryConfig,
    vocabulary: InventoryVocabulary,
) -> tuple[FocusedQuery, ...]:
    queries = list(
        generate_focused_queries(
            config.category,
            config.stage,
            qualifiers=config.qualifiers,
        )
    )
    seen = {query.text.casefold() for query in queries}
    for term in vocabulary.terms:
        if config.max_mined_terms == 0:
            break
        for wording in term.wordings[:1]:
            for language in ("de", "en"):
                stage_term = {
                    ("ohg", "de"): "Althochdeutsch",
                    ("ohg", "en"): "Old High German",
                    ("mhg", "de"): "Mittelhochdeutsch",
                    ("mhg", "en"): "Middle High German",
                    ("enhg", "de"): "Frühneuhochdeutsch",
                    ("enhg", "en"): "Early New High German",
                }[(config.stage.value, language)]
                query = FocusedQuery(
                    category=config.category,
                    stage=config.stage,
                    language=language,
                    family=f"mined_{term.kind.value}",
                    stage_term=stage_term,
                    concept=wording,
                )
                if query.text.casefold() not in seen:
                    seen.add(query.text.casefold())
                    queries.append(query)
            if sum(query.family.startswith("mined_") for query in queries) >= (
                config.max_mined_terms * 2
            ):
                return tuple(queries)
    return tuple(queries)


def _trusted_records(catalog: Catalog) -> tuple[BaseResource, ...]:
    return tuple([*catalog.corpora, *catalog.tools, *catalog.dictionaries])


__all__ = [
    "DiscoveryConfig",
    "DiscoveryDependencies",
    "DiscoveryRunResult",
    "ProviderFetch",
    "ProviderResponse",
    "run_discovery",
]
