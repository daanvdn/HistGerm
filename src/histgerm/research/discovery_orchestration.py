"""Executable, dependency-injected focused discovery orchestration."""

from __future__ import annotations

import asyncio
import ipaddress
import re
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, TypedDict, cast
from urllib.parse import SplitResult, unquote, urlsplit

from histgerm.catalog import Catalog
from histgerm.models import BaseResource, LanguageStage

from .crawl4ai_adapter import Crawl4AIAdapterError, Crawl4AIResult
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
    QueryFormulation,
    ResourceCategory,
    apply_exclusion_group,
    bounded_exclusion_groups,
    controlled_recall_formulations,
    generate_focused_queries,
    normalize_metadata_lead_terms,
    render_query,
)
from .inventory_vocabulary import (
    BoundedClassifier,
    BoundedTransport,
    CandidateDecision,
    ClassifierGap,
    CleanedSourceDocument,
    IncrementalClassifier,
    InventoryURL,
    InventoryVocabulary,
    MiningGap,
    SourceAssociation,
    SourceReconciliation,
    VocabularyKind,
    VocabularyLimits,
    enumerate_inventory_urls,
    mine_inventory_vocabulary,
    reconcile_cleaned_source,
)
from .inventory_vocabulary import VocabularyTerm as MinedVocabularyTerm
from .models import CandidateEntry
from .search_providers import (
    ResponseFormat,
    ResultInspector,
    SearchAssessmentRecord,
    SearchPageResponse,
    SearchProvider,
    SearchRequest,
    SearchResult,
    assess_paginated_search,
    assess_search_response,
    build_provider_request,
    supports_pagination,
)
from .vocabulary_store import (
    DEFAULT_REFRESH_DAYS,
    DiscoveryVocabulary,
    VocabularyContext,
    VocabularyDecision,
    VocabularyGap,
    VocabularySource,
    VocabularyTerm,
    VocabularyWording,
    apply_vocabulary,
    load_vocabulary,
    mark_source_access_gap,
    reconcile_inventory_sources,
    select_sources_for_refresh,
)


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """One injected provider transport outcome."""

    retrieval_mode: RetrievalMode
    observed_at: datetime
    http_status: int | None
    body: str = ""
    failure_stage: RetrievalFailureStage | None = None
    next_cursor: str | None = None
    exhausted: bool = False


class ProviderFetch(Protocol):
    """Execute one exact provider request without implicit live behavior."""

    def __call__(self, request: SearchRequest, /) -> ProviderResponse: ...


class SingleURLCrawler(Protocol):
    """Render exactly one selected canonical inventory URL."""

    def render(
        self, url: str, *, refresh: bool = False
    ) -> Coroutine[Any, Any, Crawl4AIResult]: ...


class DiscoveryMemo(Protocol):
    """Reuse confirmed deterministic phase outcomes across resumed runs.

    ``store_vocabulary`` and ``store_execution`` may raise to pause a run at a
    capability boundary, so callers must not swallow their exceptions.
    """

    def cached_vocabulary(self) -> IncrementalVocabulary | None: ...

    def store_vocabulary(self, value: IncrementalVocabulary, /) -> None: ...

    def cached_execution(
        self, key: str, /
    ) -> tuple[SearchAssessmentRecord, ...] | None: ...

    def store_execution(
        self, key: str, records: tuple[SearchAssessmentRecord, ...], /
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class DiscoveryDependencies:
    """All external and trusted inputs required by one discovery run."""

    catalog: Catalog
    model_call: ModelCall
    provider_fetch: ProviderFetch
    result_inspector: ResultInspector
    vocabulary_path: Path | None = None
    vocabulary_crawler: SingleURLCrawler | None = None
    vocabulary_transport: BoundedTransport | None = None
    ledger_candidates: Sequence[CandidateEntry] = ()
    vocabulary_classifier: BoundedClassifier | IncrementalClassifier | None = None
    memo: DiscoveryMemo | None = None


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
    run_on: date | None = None
    explicit_refresh_urls: tuple[str, ...] = ()
    vocabulary_refresh_days: int = DEFAULT_REFRESH_DAYS
    vocabulary_retry_days: int = 7

    def __post_init__(self) -> None:
        if self.max_mined_terms < 0:
            raise ValueError("max_mined_terms must not be negative")
        if self.max_exclusion_groups < 1:
            raise ValueError("max_exclusion_groups must be positive")
        if self.exclusion_group_size < 1:
            raise ValueError("exclusion_group_size must be positive")
        if not 1 <= self.vocabulary_refresh_days <= 365:
            raise ValueError("vocabulary_refresh_days must be between 1 and 365")
        if not 1 <= self.vocabulary_retry_days <= 30:
            raise ValueError("vocabulary_retry_days must be between 1 and 30")


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
    complete: bool
    completion_gaps: tuple[str, ...]
    vocabulary_revision: int | None = None

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
                    "intent_id": query.intent_id,
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
                    "page_number": record.page_number,
                    "pagination_state": record.pagination_state,
                    "pagination_stop_reason": record.pagination_stop_reason,
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
            "complete": self.complete,
            "completion_gaps": list(self.completion_gaps),
            "vocabulary_revision": self.vocabulary_revision,
        }


@dataclass(frozen=True, slots=True)
class _Channel:
    name: str
    provider: SearchProvider
    response_format: ResponseFormat


@dataclass(frozen=True, slots=True)
class _FollowUpPlan:
    queries: tuple[tuple[FocusedQuery, str], ...]
    truncated: bool = False


_REQUIRED_CHANNELS: tuple[_Channel, ...] = (
    _Channel("general_web_google", SearchProvider.GOOGLE, ResponseFormat.HTML),
    _Channel("general_web_bing", SearchProvider.BING, ResponseFormat.RSS),
    _Channel("general_web_brave", SearchProvider.BRAVE, ResponseFormat.HTML),
    _Channel("clarin", SearchProvider.CLARIN, ResponseFormat.HTML),
    _Channel("olac", SearchProvider.OLAC, ResponseFormat.HTML),
    _Channel("zenodo", SearchProvider.ZENODO, ResponseFormat.HTML),
    _Channel("institutional", SearchProvider.GOOGLE, ResponseFormat.HTML),
    _Channel("github", SearchProvider.GITHUB, ResponseFormat.HTML),
    _Channel("gitlab", SearchProvider.GITLAB, ResponseFormat.HTML),
    _Channel("huggingface", SearchProvider.HUGGINGFACE, ResponseFormat.HTML),
)
_GENERAL_WEB_CHANNEL_ORDER = (
    "general_web_google",
    "general_web_bing",
    "general_web_brave",
)
_GENERAL_WEB_CHANNELS = frozenset(_GENERAL_WEB_CHANNEL_ORDER)
_EXACT_STAGE_CHANNELS = _GENERAL_WEB_CHANNELS | {"institutional"}
_CROSS_CHANNEL_HOSTS = {
    "github.com": "github",
    "www.github.com": "github",
    "gitlab.com": "gitlab",
    "www.gitlab.com": "gitlab",
    "huggingface.co": "huggingface",
    "www.huggingface.co": "huggingface",
    "zenodo.org": "zenodo",
    "www.zenodo.org": "zenodo",
    "doi.org": "general_web_google",
    "dx.doi.org": "general_web_google",
    "figshare.com": "general_web_google",
    "www.figshare.com": "general_web_google",
    "osf.io": "general_web_google",
}
_REGISTRY_CHANNELS = frozenset({"clarin", "olac"})
_REGISTRY_HOSTS = frozenset(
    {
        "vlo.clarin.eu",
        "www.language-archives.org",
        "language-archives.org",
    }
)
_GITLAB_RESERVED_ROOTS = frozenset(
    {"dashboard", "explore", "groups", "help", "search", "users"}
)
_METADATA_LABEL = re.compile(
    r"(?i)\b(?:alias(?:es)?|architecture(?:s)?|model[_ -]?type|"
    r"project|author|person|owner|maintainer|institution|"
    r"organi[sz]ation)\s*[:=]\s*([@\w.-]+)"
)
_BERT_NAME = re.compile(r"(?i)\b[\w.-]*bert[\w.-]*\b")
_HANDLE = re.compile(r"(?<![\w.])@[\w.-]{2,40}\b")
_URL = re.compile(r"https?://[^\s<>()\"']+")
_TECHNICAL_LEAD = re.compile(
    r"(?i)\b(?:bert(?: architecture| family| model)?|tokeni[sz](?:er|ation)|"
    r"word embeddings?|worteinbettungen?|wort-embeddings?)\b"
)
_MAX_FOLLOW_UP_QUERIES = 16
_MAX_METADATA_LEADS = 32
_MAX_METADATA_URLS_PER_RESULT = 8
_MAX_NEGATIVE_GAP_TERMS = 4
_MAX_CONTROLLED_RECALL_QUERIES = 24
_NEGATIVE_CLAIM = re.compile(
    r"(?is)\b(?:no|none|not|kein(?:e[rsn]?)?|nicht)\b.{0,80}"
    r"\b(?:available|exists?|found|verfügbar|existiert|vorhanden)\b"
)
_EMBEDDED_IPV4 = re.compile(r"(?<!\d)(?:\d{1,3}[.-]){3}\d{1,3}(?!\d)")


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
    incremental = _memoized_vocabulary(config, dependencies)
    vocabulary = incremental.vocabulary
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
        vocabulary_revision=incremental.revision,
        vocabulary_sources_refreshed=incremental.refreshed_sources,
        vocabulary_sources_reused=incremental.reused_sources,
        vocabulary_new_terms=incremental.new_terms,
        vocabulary_reused_decisions=incremental.reused_decisions,
        vocabulary_inactive_associations=incremental.inactive_associations,
        vocabulary_access_gaps=incremental.access_gaps,
        elicitation_retries=elicitation.metrics.retries_attempted,
        elicitation_recovered_retries=elicitation.metrics.retries_recovered,
        elicitation_blocked_responses=elicitation.metrics.responses_blocked,
        elicitation_quarantined_candidates=elicitation.metrics.candidates_quarantined,
    )
    assessments: list[SearchAssessmentRecord] = []
    leads: dict[str, SearchResult] = {}
    productive_requests: set[tuple[FocusedQuery, str]] = set()
    executed_requests: set[tuple[str, str]] = set()

    for query in queries:
        for channel in _REQUIRED_CHANNELS:
            formulation = _first_round_formulation(channel)
            text = render_query(query, formulation)
            request_key = (channel.name, text.casefold())
            if request_key in executed_requests:
                continue
            executed_requests.add(request_key)
            records = _execute_query(
                text,
                query,
                channel,
                dependencies,
            )
            assessments.extend(records)
            retained = _retain_execution_leads(records, leads)
            if any(_has_lead(record) for record in records):
                productive_requests.add((query, channel.name))
            _record_execution_metrics(
                metrics,
                records,
                family=query.family,
                channel=channel.name,
                new_candidates=retained,
            )

    initial_follow_ups = _untrusted_follow_up_queries(config, assessments)
    follow_up_queries = list(initial_follow_ups.queries)
    included_follow_ups: list[FocusedQuery] = []
    follow_up_limit_reached = initial_follow_ups.truncated
    while follow_up_queries:
        query, channel_name = follow_up_queries.pop(0)
        channel = next(item for item in _REQUIRED_CHANNELS if item.name == channel_name)
        text = render_query(query, _first_round_formulation(channel))
        request_key = (channel.name, text.casefold())
        if request_key in executed_requests:
            continue
        if len(included_follow_ups) >= _MAX_FOLLOW_UP_QUERIES:
            follow_up_limit_reached = True
            break
        executed_requests.add(request_key)
        included_follow_ups.append(query)
        records = _execute_query(
            text,
            query,
            channel,
            dependencies,
        )
        assessments.extend(records)
        retained = _retain_execution_leads(records, leads)
        _record_execution_metrics(
            metrics,
            records,
            family=query.family,
            channel=channel.name,
            new_candidates=retained,
        )
        new_follow_ups = _untrusted_follow_up_queries(config, records)
        follow_up_queries.extend(new_follow_ups.queries)
        follow_up_limit_reached = follow_up_limit_reached or new_follow_ups.truncated
    all_queries = tuple(dict.fromkeys([*queries, *included_follow_ups]))

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
    for query in queries:
        for channel in _REQUIRED_CHANNELS:
            if (query, channel.name) in productive_requests:
                continue
            formulations = _weak_coverage_formulations(query, channel)
            rendered_variants = _rendered_exclusion_variants(
                query,
                formulations,
                exclusion_groups,
            )
            for excluded_text in rendered_variants:
                request_key = (channel.name, excluded_text.casefold())
                if request_key in executed_requests:
                    continue
                executed_requests.add(request_key)
                records = _execute_query(
                    excluded_text,
                    query,
                    channel,
                    dependencies,
                )
                assessments.extend(records)
                retained = _retain_execution_leads(records, leads)
                _record_execution_metrics(
                    metrics,
                    records,
                    family=query.family,
                    channel=channel.name,
                    new_candidates=retained,
                )

    for query in _controlled_recall_queries(queries):
        for channel in _REQUIRED_CHANNELS:
            if channel.name not in _GENERAL_WEB_CHANNELS:
                continue
            text = render_query(query, "stage_iso_639_3")
            request_key = (channel.name, text.casefold())
            if request_key in executed_requests:
                continue
            executed_requests.add(request_key)
            records = _execute_query(text, query, channel, dependencies)
            assessments.extend(records)
            retained = _retain_execution_leads(records, leads)
            _record_execution_metrics(
                metrics,
                records,
                family=query.family,
                channel=channel.name,
                new_candidates=retained,
            )

    completion_gaps = _completion_gaps(
        config,
        all_queries,
        assessments,
        follow_up_limit_reached=follow_up_limit_reached,
    )
    return DiscoveryRunResult(
        category=config.category,
        stage=config.stage,
        elicitation=elicitation,
        vocabulary=vocabulary,
        queries=all_queries,
        assessments=tuple(assessments),
        leads=tuple(leads.values()),
        metrics=metrics.snapshot(),
        complete=not completion_gaps,
        completion_gaps=completion_gaps,
        vocabulary_revision=incremental.revision,
    )


@dataclass(frozen=True, slots=True)
class IncrementalVocabulary:
    """One completed vocabulary phase and its incremental counters."""

    vocabulary: InventoryVocabulary
    revision: int | None = None
    refreshed_sources: int = 0
    reused_sources: int = 0
    new_terms: int = 0
    reused_decisions: int = 0
    inactive_associations: int = 0
    access_gaps: int = 0


def _memoized_vocabulary(
    config: DiscoveryConfig,
    dependencies: DiscoveryDependencies,
) -> IncrementalVocabulary:
    memo = dependencies.memo
    if memo is not None:
        cached = memo.cached_vocabulary()
        if cached is not None:
            return cached
    incremental = _incremental_vocabulary(config, dependencies)
    if memo is not None:
        memo.store_vocabulary(incremental)
    return incremental


def _incremental_vocabulary(
    config: DiscoveryConfig,
    dependencies: DiscoveryDependencies,
) -> IncrementalVocabulary:
    if dependencies.vocabulary_path is None:
        if dependencies.vocabulary_transport is None:
            raise ValueError(
                "vocabulary_path and vocabulary_crawler are required for "
                "incremental discovery"
            )
        legacy = mine_inventory_vocabulary(
            dependencies.catalog,
            category=config.category,
            stages=(config.stage,),
            transport=dependencies.vocabulary_transport,
            classifier=cast(
                BoundedClassifier | None, dependencies.vocabulary_classifier
            ),
            limits=config.vocabulary,
        )
        return IncrementalVocabulary(vocabulary=legacy)
    if dependencies.vocabulary_crawler is None:
        raise ValueError("vocabulary_crawler is required with vocabulary_path")

    on = config.run_on or date.today()
    path = dependencies.vocabulary_path
    original = load_vocabulary(path)
    inventory_urls = enumerate_inventory_urls(dependencies.catalog)
    proposed = reconcile_inventory_sources(
        original,
        inventory_urls,
        on=on,
        refresh_days=config.vocabulary_refresh_days,
    )
    proposed = _reconcile_provenance_contexts(
        proposed,
        inventory_urls,
        dependencies.catalog,
    )
    selected = select_sources_for_refresh(
        proposed,
        on=on,
        explicit_urls=config.explicit_refresh_urls,
        max_sources=config.vocabulary.max_pages,
    )
    inventory_by_url = {item.url: item for item in inventory_urls}
    refreshed = reused_decisions = gaps = 0
    prior_keys = {(term.normalized, term.kind) for term in original.terms}

    for stored_source in selected:
        source = inventory_by_url[stored_source.url]
        prior_decisions = _prior_decisions(stored_source)
        contexts = _source_contexts(source, dependencies.catalog)
        prior_associations = _prior_associations(proposed, stored_source.url)
        try:
            rendered = _render_one(
                dependencies.vocabulary_crawler,
                stored_source.url,
                refresh=stored_source.last_attempted_on is not None,
            )
        except Exception as error:
            reason = (
                f"{error.stage}: {error}"
                if isinstance(error, Crawl4AIAdapterError)
                else str(error) or type(error).__name__
            )
            proposed = mark_source_access_gap(
                proposed,
                source_url=stored_source.url,
                attempted_on=on,
                reason=reason,
                retry_days=config.vocabulary_retry_days,
            )
            gaps += 1
            continue
        refreshed += 1
        if (
            stored_source.cleaned_content_sha256 is not None
            and rendered.cleaned_content_sha256 == stored_source.cleaned_content_sha256
        ):
            proposed = _update_source_success(
                proposed,
                stored_source.url,
                rendered,
                on=on,
                refresh_days=config.vocabulary_refresh_days,
            )
            continue
        if not contexts:
            proposed = _update_source_success(
                proposed,
                stored_source.url,
                rendered,
                on=on,
                refresh_days=config.vocabulary_refresh_days,
            )
            continue
        reconciliation = _reconcile_source_contexts(
            source,
            CleanedSourceDocument(
                source_url=rendered.source_url,
                cleaned_markdown=rendered.cleaned_markdown,
                structured_metadata=rendered.structured_metadata,
            ),
            contexts=contexts,
            prior_decisions=prior_decisions,
            prior_associations=prior_associations,
            classifier=cast(
                IncrementalClassifier | None, dependencies.vocabulary_classifier
            ),
            limits=config.vocabulary,
        )
        reused_decisions += reconciliation.reused_decisions
        proposed = _merge_reconciliation(
            proposed,
            stored_source,
            reconciliation,
            rendered,
            on=on,
            refresh_days=config.vocabulary_refresh_days,
        )

    proposed = DiscoveryVocabulary.model_validate(proposed)
    if _substantive_dump(proposed) == _substantive_dump(original):
        confirmed = original
    else:
        confirmed = apply_vocabulary(
            path, proposed, expected_revision=original.revision
        )
    active = _active_inventory_vocabulary(
        confirmed,
        dependencies.catalog,
        config.category,
        config.stage,
    )
    current_keys = {(term.normalized, term.kind) for term in confirmed.terms}
    return IncrementalVocabulary(
        vocabulary=active,
        revision=confirmed.revision,
        refreshed_sources=refreshed,
        reused_sources=sum(source.status == "active" for source in confirmed.sources)
        - len(selected),
        new_terms=len(current_keys - prior_keys),
        reused_decisions=reused_decisions,
        inactive_associations=max(
            0,
            _inactive_association_count(confirmed)
            - _inactive_association_count(original),
        ),
        access_gaps=gaps,
    )


def _render_one(
    crawler: SingleURLCrawler, url: str, *, refresh: bool
) -> Crawl4AIResult:
    return asyncio.run(crawler.render(url, refresh=refresh))


def _prior_decisions(source: VocabularySource) -> tuple[CandidateDecision, ...]:
    return tuple(
        CandidateDecision(item.normalized, item.suggested_kind, item.accepted)
        for item in source.decisions
    )


def _prior_associations(
    vocabulary: DiscoveryVocabulary,
    source_url: str,
) -> tuple[SourceAssociation, ...]:
    associations: list[SourceAssociation] = []
    source = next(item for item in vocabulary.sources if item.url == source_url)
    for term in vocabulary.terms:
        for wording in term.wordings:
            if (
                source_url not in wording.source_urls
                and source_url not in wording.inactive_source_urls
            ):
                continue
            active = source_url in wording.source_urls
            for context in term.contexts:
                associations.append(
                    SourceAssociation(
                        normalized=term.normalized,
                        kind=term.kind,
                        wording=wording.value,
                        source_url=source_url,
                        resource_ids=tuple(source.resource_ids),
                        source_fields=tuple(source.source_fields),
                        category=context.category,
                        stages=(context.stage,),
                        active=active,
                    )
                )
    return tuple(associations)


def _source_contexts(
    source: InventoryURL,
    catalog: Catalog,
) -> tuple[tuple[ResourceCategory, LanguageStage], ...]:
    records = {
        record.id: (category, record)
        for category, items in (
            ("corpus", catalog.corpora),
            ("tool", catalog.tools),
            ("dictionary", catalog.dictionaries),
        )
        for record in items
    }
    contexts: set[tuple[ResourceCategory, LanguageStage]] = set()
    for resource_id in source.resource_ids:
        matched = records.get(resource_id)
        if matched is None:
            continue
        category, record = matched
        for attribute in ("covered_stages", "supported_stages"):
            contexts.update(
                (cast(ResourceCategory, category), stage)
                for stage in (getattr(record, attribute, None) or ())
            )
    return tuple(sorted(contexts, key=lambda item: (item[0], item[1].value)))


def _reconcile_provenance_contexts(
    vocabulary: DiscoveryVocabulary,
    inventory_urls: tuple[InventoryURL, ...],
    catalog: Catalog,
) -> DiscoveryVocabulary:
    contexts_by_url = {
        source.url: _source_contexts(source, catalog) for source in inventory_urls
    }
    terms: list[VocabularyTerm] = []
    for term in vocabulary.terms:
        contexts = {(context.category, context.stage) for context in term.contexts}
        active_source_urls = {
            source_url
            for wording in term.wordings
            for source_url in wording.source_urls
        }
        for source_url in active_source_urls:
            contexts.update(contexts_by_url.get(source_url, ()))
        terms.append(
            VocabularyTerm.model_validate(
                term.model_copy(
                    update={
                        "contexts": [
                            VocabularyContext(category=category, stage=stage)
                            for category, stage in sorted(
                                contexts,
                                key=lambda item: (item[0], item[1].value),
                            )
                        ]
                    }
                )
            )
        )
    return DiscoveryVocabulary.model_validate(
        vocabulary.model_copy(update={"terms": terms})
    )


def _reconcile_source_contexts(
    source: InventoryURL,
    document: CleanedSourceDocument,
    *,
    contexts: tuple[tuple[ResourceCategory, LanguageStage], ...],
    prior_decisions: tuple[CandidateDecision, ...],
    prior_associations: tuple[SourceAssociation, ...],
    classifier: IncrementalClassifier | None,
    limits: VocabularyLimits,
) -> SourceReconciliation:
    decisions = prior_decisions
    reconciliations: list[SourceReconciliation] = []
    for category, stage in contexts:
        reconciliation = reconcile_cleaned_source(
            source,
            document,
            category=category,
            stages=(stage,),
            prior_decisions=decisions,
            prior_associations=tuple(
                association
                for association in prior_associations
                if association.category == category and association.stages == (stage,)
            ),
            classifier=classifier,
            limits=limits,
        )
        decisions = reconciliation.decisions
        reconciliations.append(reconciliation)

    candidate_keys = tuple(
        sorted(
            {
                key
                for reconciliation in reconciliations
                for key in reconciliation.candidate_keys
            }
        )
    )
    prior_keys = {decision.key for decision in prior_decisions}
    associations = [
        association
        for reconciliation in reconciliations
        for association in reconciliation.associations
    ]
    represented_wordings = {
        (association.normalized, association.kind, association.wording)
        for association in associations
    }
    supported_contexts = set(contexts)
    for association in prior_associations:
        wording_key = (
            association.normalized,
            association.kind,
            association.wording,
        )
        if (
            association.category,
            association.stages[0],
        ) in supported_contexts or wording_key in represented_wordings:
            continue
        associations.append(
            SourceAssociation(
                normalized=association.normalized,
                kind=association.kind,
                wording=association.wording,
                source_url=association.source_url,
                resource_ids=source.resource_ids,
                source_fields=source.source_fields,
                category=association.category,
                stages=association.stages,
                active=False,
            )
        )
        represented_wordings.add(wording_key)
    classifier_gap: ClassifierGap | None = next(
        (
            reconciliation.classifier_gap
            for reconciliation in reconciliations
            if reconciliation.classifier_gap is not None
        ),
        None,
    )
    return SourceReconciliation(
        source=source,
        associations=tuple(associations),
        decisions=decisions,
        candidate_keys=candidate_keys,
        reused_decisions=len(prior_keys.intersection(candidate_keys)),
        new_decisions=len(decisions) - len(prior_decisions),
        inactive_associations=sum(
            reconciliation.inactive_associations for reconciliation in reconciliations
        ),
        classifier_gap=classifier_gap,
    )


def _update_source_success(
    vocabulary: DiscoveryVocabulary,
    url: str,
    result: Crawl4AIResult,
    *,
    on: date,
    refresh_days: int,
) -> DiscoveryVocabulary:
    headers = result.response_metadata.get("headers")
    response_headers = headers if isinstance(headers, dict) else {}
    etag = response_headers.get("etag")
    last_modified = response_headers.get("last-modified")
    sources = [
        source
        if source.url != url
        else VocabularySource.model_validate(
            source.model_copy(
                update={
                    "last_attempted_on": on,
                    "last_successful_on": on,
                    "refresh_after": on + timedelta(days=refresh_days),
                    "etag": etag if isinstance(etag, str) else None,
                    "last_modified": (
                        last_modified if isinstance(last_modified, str) else None
                    ),
                    "crawl_cache_key": result.crawl_cache_key,
                    "raw_content_sha256": result.raw_content_sha256,
                    "cleaned_content_sha256": result.cleaned_content_sha256,
                    "crawler_version": result.crawler_version,
                    "extractor_version": result.extractor_version,
                    "gap": None,
                }
            )
        )
        for source in vocabulary.sources
    ]
    return DiscoveryVocabulary.model_validate(
        vocabulary.model_copy(
            update={"updated_on": max(vocabulary.updated_on, on), "sources": sources}
        )
    )


def _merge_reconciliation(
    vocabulary: DiscoveryVocabulary,
    stored_source: VocabularySource,
    reconciliation: SourceReconciliation,
    result: Crawl4AIResult,
    *,
    on: date,
    refresh_days: int,
) -> DiscoveryVocabulary:
    updated = _update_source_success(
        vocabulary,
        stored_source.url,
        result,
        on=on,
        refresh_days=refresh_days,
    )
    source = next(item for item in updated.sources if item.url == stored_source.url)
    old_decisions = {
        (item.normalized, item.suggested_kind): item for item in stored_source.decisions
    }
    seen = set(reconciliation.candidate_keys)
    decisions = []
    for item in reconciliation.decisions:
        old = old_decisions.get((item.normalized, item.suggested_kind))
        decisions.append(
            VocabularyDecision(
                normalized=item.normalized,
                suggested_kind=item.suggested_kind,
                accepted=item.accepted,
                active=item.key in seen,
                first_seen_on=old.first_seen_on if old else on,
                last_seen_on=(
                    on if item.key in seen else (old.last_seen_on if old else on)
                ),
            )
        )
    sources = [
        item
        if item.url != source.url
        else VocabularySource.model_validate(
            item.model_copy(
                update={
                    "decisions": decisions,
                    "gap": (
                        VocabularyGap(
                            attempted_on=on,
                            kind="classifier",
                            reason=reconciliation.classifier_gap.reason,
                        )
                        if reconciliation.classifier_gap is not None
                        else None
                    ),
                }
            )
        )
        for item in updated.sources
    ]
    terms = _merge_terms(updated, reconciliation, on=on)
    return DiscoveryVocabulary.model_validate(
        updated.model_copy(update={"sources": sources, "terms": terms})
    )


class _WordingState(TypedDict):
    active: set[str]
    inactive: set[str]
    first: date
    last: date


class _TermState(TypedDict):
    contexts: set[tuple[ResourceCategory, LanguageStage]]
    wordings: dict[str, _WordingState]


def _merge_terms(
    vocabulary: DiscoveryVocabulary,
    reconciliation: SourceReconciliation,
    *,
    on: date,
) -> list[VocabularyTerm]:
    url = reconciliation.source.url
    entries: dict[tuple[str, VocabularyKind], _TermState] = {}
    for term in vocabulary.terms:
        term_state = entries.setdefault(
            (term.normalized, term.kind),
            {"contexts": set(), "wordings": {}},
        )
        term_state["contexts"].update(
            (context.category, context.stage) for context in term.contexts
        )
        for stored_wording in term.wordings:
            term_state["wordings"][stored_wording.value] = {
                "active": set(stored_wording.source_urls) - {url},
                "inactive": set(stored_wording.inactive_source_urls) - {url},
                "first": stored_wording.first_seen_on,
                "last": stored_wording.last_seen_on,
            }
    for association in reconciliation.associations:
        term_state = entries.setdefault(
            (association.normalized, association.kind),
            {"contexts": set(), "wordings": {}},
        )
        term_state["contexts"].update(
            (association.category, stage) for stage in association.stages
        )
        wording_state = term_state["wordings"].setdefault(
            association.wording,
            {"active": set(), "inactive": set(), "first": on, "last": on},
        )
        if association.active:
            wording_state["active"].add(url)
            wording_state["inactive"].discard(url)
            wording_state["last"] = on
        else:
            wording_state["inactive"].add(url)
            wording_state["active"].discard(url)
    result: list[VocabularyTerm] = []
    for (normalized, kind), term_state in entries.items():
        merged_wordings: list[VocabularyWording] = []
        for value, wording_state in term_state["wordings"].items():
            active_urls = sorted(wording_state["active"])
            inactive_urls = sorted(wording_state["inactive"])
            if active_urls or inactive_urls:
                merged_wordings.append(
                    VocabularyWording(
                        value=value,
                        source_urls=active_urls,
                        inactive_source_urls=inactive_urls,
                        first_seen_on=wording_state["first"],
                        last_seen_on=wording_state["last"],
                    )
                )
        if not merged_wordings:
            continue
        merged_contexts = [
            VocabularyContext(category=category, stage=stage)
            for category, stage in sorted(
                term_state["contexts"],
                key=lambda value: (value[0], value[1].value),
            )
        ]
        result.append(
            VocabularyTerm(
                normalized=normalized,
                kind=kind,
                active=any(wording.source_urls for wording in merged_wordings),
                contexts=merged_contexts,
                wordings=merged_wordings,
            )
        )
    return result


def _active_inventory_vocabulary(
    vocabulary: DiscoveryVocabulary,
    catalog: Catalog,
    category: ResourceCategory,
    stage: LanguageStage,
) -> InventoryVocabulary:
    matching_source_urls = _matching_active_source_urls(
        vocabulary, catalog, category, stage
    )
    terms = []
    for term in vocabulary.terms:
        if not term.active or not any(
            context.category == category and context.stage == stage
            for context in term.contexts
        ):
            continue
        active_wordings = [
            wording
            for wording in term.wordings
            if matching_source_urls.intersection(wording.source_urls)
        ]
        if not active_wordings:
            continue
        source_urls = sorted(
            {
                url
                for wording in active_wordings
                for url in wording.source_urls
                if url in matching_source_urls
            }
        )
        source_fields = sorted(
            {
                field
                for source in vocabulary.sources
                if source.url in source_urls
                for field in source.source_fields
            }
        )
        terms.append(
            MinedVocabularyTerm(
                normalized=term.normalized,
                kind=term.kind,
                wordings=tuple(wording.value for wording in active_wordings),
                source_urls=tuple(source_urls),
                source_fields=tuple(source_fields),
            )
        )
    gaps = tuple(
        MiningGap(source.url, source.gap.reason)
        for source in vocabulary.sources
        if source.gap is not None
    )
    return InventoryVocabulary(
        terms=tuple(terms),
        urls=(),
        gaps=gaps,
        fetched_pages=0,
        fetched_bytes=0,
    )


def _matching_active_source_urls(
    vocabulary: DiscoveryVocabulary,
    catalog: Catalog,
    category: ResourceCategory,
    stage: LanguageStage,
) -> set[str]:
    records: dict[str, tuple[ResourceCategory, set[LanguageStage]]] = {}
    for record_category, items in (
        ("corpus", catalog.corpora),
        ("tool", catalog.tools),
        ("dictionary", catalog.dictionaries),
    ):
        for record in items:
            stages: set[LanguageStage] = set()
            for attribute in ("covered_stages", "supported_stages"):
                stages.update(getattr(record, attribute, None) or ())
            records[record.id] = (cast(ResourceCategory, record_category), stages)

    return {
        source.url
        for source in vocabulary.sources
        if source.status == "active"
        and any(
            resource_id in records
            and records[resource_id][0] == category
            and stage in records[resource_id][1]
            for resource_id in source.resource_ids
        )
    }


def _substantive_dump(vocabulary: DiscoveryVocabulary) -> dict[str, object]:
    result = vocabulary.model_dump(mode="json")
    result.pop("revision")
    result.pop("updated_on")
    return result


def _inactive_association_count(vocabulary: DiscoveryVocabulary) -> int:
    return sum(
        len(wording.inactive_source_urls)
        for term in vocabulary.terms
        for wording in term.wordings
    )


def _execute_query(
    text: str,
    query: FocusedQuery,
    channel: _Channel,
    dependencies: DiscoveryDependencies,
) -> tuple[SearchAssessmentRecord, ...]:
    memo = dependencies.memo
    key = f"{channel.name}\x1f{text}"
    if memo is not None:
        cached = memo.cached_execution(key)
        if cached is not None:
            return cached
    records = _fetch_and_assess(text, query, channel, dependencies)
    if memo is not None:
        memo.store_execution(key, records)
    return records


def _fetch_and_assess(
    text: str,
    query: FocusedQuery,
    channel: _Channel,
    dependencies: DiscoveryDependencies,
) -> tuple[SearchAssessmentRecord, ...]:
    locale = "de-DE" if query.language == "de" else "en-US"
    request = build_provider_request(
        channel.provider,
        text,
        channel=channel.name,
        locale=locale,
        retrieval_mode="bounded_http",
        response_format=channel.response_format,
    )
    if supports_pagination(request.provider):
        paginated = assess_paginated_search(
            request,
            fetch_page=lambda page_request: _fetch_page(page_request, dependencies),
            inspector=dependencies.result_inspector,
        )
        return paginated.attempts
    response = _fetch_provider_response(request, dependencies)
    record = assess_search_response(
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
    return (
        replace(
            record,
            pagination_state="access_gap",
            pagination_stop_reason="unsupported_pagination",
            observation=(
                f"{record.observation}; {request.provider.value} pagination is "
                "unsupported, so provider exhaustion was not established"
            ),
        ),
    )


def _fetch_page(
    request: SearchRequest,
    dependencies: DiscoveryDependencies,
) -> SearchPageResponse:
    response = _fetch_provider_response(request, dependencies)
    return SearchPageResponse(
        retrieval_mode=response.retrieval_mode,
        observed_at=response.observed_at,
        http_status=response.http_status,
        failure_stage=response.failure_stage,
        body=response.body,
        next_cursor=response.next_cursor,
        exhausted=response.exhausted,
    )


def _fetch_provider_response(
    request: SearchRequest,
    dependencies: DiscoveryDependencies,
) -> ProviderResponse:
    try:
        return dependencies.provider_fetch(request)
    except Exception as error:
        return ProviderResponse(
            retrieval_mode="bounded_http",
            observed_at=datetime.now(UTC),
            http_status=None,
            failure_stage="request",
            body=str(error),
        )


def _first_round_formulation(channel: _Channel) -> QueryFormulation:
    return "exact_stage" if channel.name in _EXACT_STAGE_CHANNELS else "plain"


def _weak_coverage_formulations(
    query: FocusedQuery,
    channel: _Channel,
) -> tuple[QueryFormulation, ...]:
    base = _first_round_formulation(channel)
    if channel.name not in _GENERAL_WEB_CHANNELS:
        return (base,)
    return tuple(
        formulation
        for formulation in controlled_recall_formulations(query)
        if formulation != "stage_iso_639_3"
    )


def _rendered_exclusion_variants(
    query: FocusedQuery,
    formulations: Sequence[QueryFormulation],
    exclusion_groups: Sequence[Sequence[str]],
) -> tuple[str, ...]:
    texts: list[str] = []
    seen: set[str] = set()
    groups: Sequence[Sequence[str] | None] = (
        exclusion_groups if exclusion_groups else (None,)
    )
    for formulation in formulations:
        formulation_groups = (
            (None, *groups) if formulation == "stage_abbreviation" else groups
        )
        for group in formulation_groups:
            text = (
                render_query(query, formulation)
                if group is None
                else apply_exclusion_group(
                    query,
                    group,
                    formulation=formulation,
                )
            )
            key = text.casefold()
            if key not in seen:
                seen.add(key)
                texts.append(text)
    return tuple(texts)


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


def _retain_execution_leads(
    records: Sequence[SearchAssessmentRecord],
    leads: dict[str, SearchResult],
) -> int:
    return sum(_retain_leads(record, leads) for record in records)


def _record_execution_metrics(
    metrics: DiscoveryCoverage,
    records: Sequence[SearchAssessmentRecord],
    *,
    family: str,
    channel: str,
    new_candidates: int,
) -> None:
    for index, record in enumerate(records):
        metrics.record_assessment(
            record,
            family=family,
            channel=channel,
            new_candidates=new_candidates if index == 0 else 0,
        )


def _has_lead(record: SearchAssessmentRecord) -> bool:
    return any(inspection.classification == "lead" for inspection in record.inspections)


def _untrusted_follow_up_queries(
    config: DiscoveryConfig,
    assessments: Sequence[SearchAssessmentRecord],
) -> _FollowUpPlan:
    raw_leads: list[tuple[str, tuple[str, ...]]] = []
    truncated = False
    for record in assessments:
        for result, inspection in zip(record.results, record.inspections, strict=True):
            if inspection.classification != "lead":
                continue
            metadata = " ".join(
                value for value in (result.title, result.snippet) if value
            )
            for match in _METADATA_LABEL.finditer(metadata):
                label = match.group(0).split(":", 1)[0].split("=", 1)[0].casefold()
                label_targets = (
                    ("institutional", "general_web_google")
                    if any(
                        name in label
                        for name in (
                            "author",
                            "person",
                            "institution",
                            "organisation",
                            "organization",
                        )
                    )
                    else ("github", "huggingface")
                )
                raw_leads.append((match.group(1), label_targets))
            raw_leads.extend(
                (match.group(0), ("github", "huggingface"))
                for match in _BERT_NAME.finditer(metadata)
            )
            raw_leads.extend(
                (match.group(0), ("github", "huggingface"))
                for match in _HANDLE.finditer(metadata)
            )
            raw_leads.extend(
                (match.group(0), ("github", "huggingface"))
                for match in _TECHNICAL_LEAD.finditer(metadata)
            )
            raw_leads.extend(
                (term, _GENERAL_WEB_CHANNEL_ORDER)
                for term in _negative_gap_terms(config, metadata)
            )
            urls = (result.url, *(_URL.findall(metadata)))[
                :_MAX_METADATA_URLS_PER_RESULT
            ]
            for url in urls:
                pivot = _cross_channel_pivot(url, record.channel)
                if pivot is not None:
                    raw_leads.append((pivot[0], (pivot[1],)))
            if len(raw_leads) >= _MAX_METADATA_LEADS:
                truncated = True
                raw_leads = raw_leads[:_MAX_METADATA_LEADS]
                break
        if len(raw_leads) >= _MAX_METADATA_LEADS:
            break

    normalized = normalize_metadata_lead_terms(
        (term for term, _ in raw_leads),
        max_terms=8,
    )
    targets_by_term: dict[str, tuple[str, ...]] = {}
    for term, lead_targets in raw_leads:
        safe = normalize_metadata_lead_terms((term,), max_terms=1)
        if safe:
            term_key = safe[0].casefold()
            targets_by_term.setdefault(term_key, ())
            targets_by_term[term_key] = tuple(
                dict.fromkeys((*targets_by_term[term_key], *lead_targets))
            )

    queries: list[tuple[FocusedQuery, str]] = []
    seen: set[tuple[str, str]] = set()
    for term in normalized:
        for target in targets_by_term[term.casefold()]:
            query = FocusedQuery(
                category=config.category,
                stage=config.stage,
                language="en",
                family="untrusted_metadata_lead",
                stage_term={
                    LanguageStage.OHG: "Old High German",
                    LanguageStage.MHG: "Middle High German",
                    LanguageStage.ENHG: "Early New High German",
                }[config.stage],
                concept=term,
            )
            query_key = (query.text.casefold(), target)
            if query_key not in seen:
                seen.add(query_key)
                queries.append((query, target))
    return _FollowUpPlan(tuple(queries), truncated)


def _completion_gaps(
    config: DiscoveryConfig,
    queries: Sequence[FocusedQuery],
    assessments: Sequence[SearchAssessmentRecord],
    *,
    follow_up_limit_reached: bool,
) -> tuple[str, ...]:
    gaps: list[str] = []
    incomplete = [record for record in assessments if not record.completed]
    if incomplete:
        reasons = sorted(
            {
                record.pagination_stop_reason or record.assessment
                for record in incomplete
            }
        )
        gaps.append(
            "provider coverage incomplete: "
            + ", ".join(str(reason) for reason in reasons)
        )
    if follow_up_limit_reached:
        gaps.append(
            f"untrusted metadata follow-up limit {_MAX_FOLLOW_UP_QUERIES} reached"
        )
    if config.category == "tool":
        concepts = {
            (query.language, query.concept.casefold())
            for query in queries
            if query.family != "untrusted_metadata_lead"
        }
        required = {
            ("de", "tokenizer"),
            ("de", "bert-architektur"),
            ("de", "bert-modellfamilie"),
            ("de", "vortrainiertes sprachmodell"),
            ("de", "maskiertes sprachmodell"),
            ("de", "worteinbettung"),
            ("en", "tokenizer"),
            ("en", "bert architecture"),
            ("en", "bert family"),
            ("en", "pretrained language model"),
            ("en", "masked language model"),
            ("en", "word embedding"),
        }
        missing = sorted(required - concepts)
        if missing:
            gaps.append(
                "required bilingual architecture families unqueried: "
                + ", ".join(f"{language}:{concept}" for language, concept in missing)
            )
    return tuple(gaps)


def _cross_channel_pivot(url: str, source_channel: str) -> tuple[str, str] | None:
    parsed = _public_https_url(url)
    if parsed is None:
        return None
    host = parsed.hostname
    assert host is not None
    target = _CROSS_CHANNEL_HOSTS.get(host)
    if target is None:
        if source_channel not in _REGISTRY_CHANNELS:
            return None
        target = "institutional"
    if target == source_channel or host in _REGISTRY_HOSTS:
        return None

    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if target == "huggingface" and parts[:1] == ["models"]:
        parts = parts[1:]
    if target == "huggingface" and parts[:1] in (
        ["datasets"],
        ["docs"],
        ["organizations"],
        ["spaces"],
    ):
        return None
    if target == "github":
        if len(parts) != 2:
            return None
        parts[1] = parts[1].removesuffix(".git")
    elif target == "gitlab":
        if "-" in parts:
            parts = parts[: parts.index("-")]
        if len(parts) < 2 or parts[0].casefold() in _GITLAB_RESERVED_ROOTS:
            return None
        parts[-1] = parts[-1].removesuffix(".git")
        if len(parts) > 4:
            parts = [*parts[:3], parts[-1]]
    elif target == "huggingface":
        if len(parts) < 2:
            return None
        parts = parts[:2]
    elif target == "zenodo":
        if len(parts) != 2 or parts[0].casefold() not in {"record", "records"}:
            return None
        parts = ["Zenodo", parts[1]]
    elif target == "general_web_google":
        if not parts:
            return None
        parts = parts[:2]
    else:
        meaningful = [
            part
            for part in parts
            if part.casefold() not in {"en", "project", "projects", "research"}
        ]
        parts = meaningful[-2:] or [host.split(".")[0]]

    term = " ".join(parts)
    safe = normalize_metadata_lead_terms((term,), max_terms=1)
    return (safe[0], target) if safe else None


def _public_https_url(url: str) -> SplitResult | None:
    candidate = url.rstrip(".,;")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname
    if (
        parsed.scheme.casefold() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        return None
    host = host.rstrip(".").casefold()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if host == "localhost" or host.endswith(
        (
            ".local",
            ".localhost",
            ".internal",
            ".home.arpa",
            ".test",
            ".invalid",
            ".example",
        )
    ):
        return None
    for match in _EMBEDDED_IPV4.finditer(host):
        try:
            if not ipaddress.ip_address(match.group().replace("-", ".")).is_global:
                return None
        except ValueError:
            continue
    try:
        if not ipaddress.ip_address(host).is_global:
            return None
    except ValueError:
        if "." not in host:
            return None
    return parsed._replace(netloc=host)


def _negative_gap_terms(
    config: DiscoveryConfig,
    metadata: str,
) -> tuple[str, ...]:
    claims = tuple(match.group(0) for match in _NEGATIVE_CLAIM.finditer(metadata))
    if not claims:
        return ()
    concepts = dict.fromkeys(
        query.concept
        for query in generate_focused_queries(
            config.category,
            config.stage,
            include_named_tagsets=False,
        )
    )
    matching = [
        concept
        for concept in concepts
        if any(
            re.search(
                rf"(?<!\w){re.escape(concept)}(?!\w)",
                claim,
                flags=re.IGNORECASE,
            )
            for claim in claims
        )
    ]
    return tuple(matching[:_MAX_NEGATIVE_GAP_TERMS])


def _controlled_recall_queries(
    queries: Sequence[FocusedQuery],
) -> tuple[FocusedQuery, ...]:
    selected: list[FocusedQuery] = []
    seen_families: set[str] = set()
    for query in queries:
        if (
            "stage_iso_639_3" in controlled_recall_formulations(query)
            and query.family not in seen_families
        ):
            seen_families.add(query.family)
            selected.append(query)
            if len(selected) == _MAX_CONTROLLED_RECALL_QUERIES:
                return tuple(selected)
    return tuple(selected)


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
