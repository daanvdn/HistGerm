# fmt: off
# ruff: noqa: E501
"""Strict auxiliary models for inventory-curator research state."""
from __future__ import annotations

import re
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
    model_validator,
)

from histgerm.models import Corpus, Dictionary, LanguageStage, LegalPermission, Tool

from .query_intents import (
    CHANNELS as _CHANNELS,
)
from .query_intents import (
    INTENT_ID_PATTERN as _INTENT_ID_PATTERN,
)
from .query_intents import (
    architecture_terms,
    category_terms,
    parse_intent_id,
    required_intent_ids,
    stage_terms,
)

type ResourceCategory = Literal['corpus', 'tool', 'dictionary']
type CandidateDisposition = Literal['pending', 'added', 'duplicate', 'out_of_scope', 'blocked']
type SweepState = Literal['not_started', 'in_progress', 'complete']
type SearchLanguage = Literal['de', 'en']
type SourceKind = Literal['canonical_project', 'institutional', 'repository', 'registry', 'model_card', 'primary_scholarly']
type RiskFlag = Literal['legal_change', 'legal_conflict', 'identity_conflict', 'schema_change', 'availability_change', 'inaccessible_evidence']
__all__ = ['CandidateDisposition', 'CandidateEntry', 'CandidateResearchResult', 'DiscoveryLedger', 'EvidenceExcerpt', 'RequestDestination', 'ResourceCategory', 'RiskFlag', 'SearchLanguage', 'SearchPass', 'SearchQueryRecord', 'SourceKind', 'SweepEntry', 'SweepState', 'resolve_request_destination']
_STABLE_ID_PATTERN = '^[a-z0-9]+(?:-[a-z0-9]+)*$'
_CANDIDATE_ID_PATTERN = '^candidate-[a-z0-9]+(?:-[a-z0-9]+)*$'
_CHANNEL_PATTERN = '^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$'
_SUPPORT_RE = re.compile('^[a-z][a-z0-9_]*(?:\\.[a-z0-9]+(?:[-_][a-z0-9]+)*)*$')
_CATEGORY_PREFIXES: dict[ResourceCategory, str] = {'corpus': 'corpus-', 'tool': 'tool-', 'dictionary': 'dictionary-'}
_STAGE_SUPPORT_FIELDS: dict[ResourceCategory, str] = {'corpus': 'covered_stages', 'tool': 'supported_stages', 'dictionary': 'covered_stages'}
_FINAL_DISPOSITIONS = {'added', 'duplicate', 'out_of_scope', 'blocked'}
_NON_PUBLIC_NAMES = ('.localhost', '.local', '.internal', '.home.arpa', '.test', '.invalid', '.example')
_EMBEDDED_IPV4_RE = re.compile(r'(?<!\d)(?:\d{1,3}[.-]){3}\d{1,3}(?!\d)')

def _non_global_embedded_address(host: str) -> bool:
    for match in _EMBEDDED_IPV4_RE.finditer(host):
        try:
            address = ip_address(match.group().replace('-', '.'))
        except ValueError:
            continue
        if not address.is_global:
            return True
    return False

def _public_http_url(value: HttpUrl) -> HttpUrl:
    if value.username is not None or value.password is not None:
        raise ValueError('URLs must not contain credentials')
    host = (value.host or '').rstrip('.').lower().strip('[]')
    if not host or host == 'localhost' or host.endswith(_NON_PUBLIC_NAMES):
        raise ValueError('URL host must be public')
    if _non_global_embedded_address(host):
        raise ValueError('URL host embeds a non-public address')
    try:
        address = ip_address(host)
    except ValueError as error:
        if '.' not in host:
            raise ValueError('URL host must be a public DNS name') from error
    else:
        if not address.is_global:
            raise ValueError('URL host must be a public address')
    return value
type PublicHttpUrl = Annotated[HttpUrl, AfterValidator(_public_http_url)]
type AuthoredSeedText = Annotated[str, StringConstraints(strip_whitespace=False)]
type _ResolverResult = tuple[int, int, int, str, tuple[Any, ...]]
type AddressResolver = Callable[..., Sequence[_ResolverResult]]

@dataclass(frozen=True)
class RequestDestination:
    """A request boundary result pinned to one validated DNS answer."""
    url: HttpUrl
    hostname: str
    port: int
    connect_ip: IPv4Address | IPv6Address

def resolve_request_destination(value: str | HttpUrl, *, resolver: AddressResolver=socket.getaddrinfo) -> RequestDestination:
    """Validate and resolve immediately before each request, including redirects.

    The transport must connect to ``connect_ip`` while retaining ``hostname`` for
    HTTP Host and TLS SNI; it must not resolve the hostname again.
    """
    url = _public_http_url(value if isinstance(value, HttpUrl) else HttpUrl(value))
    hostname = (url.host or '').rstrip('.').lower().strip('[]')
    port = url.port or (443 if url.scheme == 'https' else 80)
    try:
        answers = resolver(hostname, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise ValueError(f'could not resolve request destination {hostname!r}') from error
    addresses = [ip_address(answer[4][0]) for answer in answers]
    if not addresses:
        raise ValueError(f'request destination {hostname!r} has no addresses')
    if any(not address.is_global for address in addresses):
        raise ValueError(f'request destination {hostname!r} resolved to a non-public address')
    return RequestDestination(url=url, hostname=hostname, port=port, connect_ip=addresses[0])

def _reject_empty_strings(value: Any) -> Any:
    """Reject empty strings recursively in model input."""
    if isinstance(value, str) and (not value.strip()):
        raise ValueError('empty strings are not allowed')
    if isinstance(value, list):
        for item in value:
            _reject_empty_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_empty_strings(key)
            _reject_empty_strings(item)
    return value

def _require_unique(values: Sequence[str], label: str) -> None:
    """Require unique string values while preserving authored order."""
    if len(values) != len(set(values)):
        raise ValueError(f'{label} must be unique')

def _require_category_prefix(resource_id: str, category: ResourceCategory, label: str) -> None:
    """Require a resource identifier to match its category."""
    if not resource_id.startswith(_CATEGORY_PREFIXES[category]):
        raise ValueError(f'{label} must start with {_CATEGORY_PREFIXES[category]!r}')

class _ResearchModel(BaseModel):
    """Apply strict research-model input and assignment behavior."""
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, validate_assignment=True, populate_by_name=False)

    @field_validator('*', mode='before')
    @classmethod
    def reject_empty_strings(cls, value: Any) -> Any:
        """Reject empty strings before validating any field."""
        return _reject_empty_strings(value)

class SearchQueryRecord(_ResearchModel):
    """Record one executed query in one stable search channel."""
    query: str
    language: SearchLanguage
    channel: str = Field(pattern=_CHANNEL_PATTERN)
    source_urls: list[PublicHttpUrl]
    completed: bool
    intent_id: str | None = Field(default=None, pattern=_INTENT_ID_PATTERN)
    note: str | None = None

    @model_validator(mode='after')
    def validate_coverage_record(self) -> SearchQueryRecord:
        if not self.source_urls and (not self.note):
            raise ValueError('a query without source URLs requires a policy note')
        if self.completed and not self.source_urls and not any(marker in (self.note or '').casefold() for marker in ('inapplicable', 'not applicable')):
            raise ValueError('a completed query without source URLs requires an explicit inapplicable policy reason')
        if self.intent_id is not None and self.intent_id.rsplit('-', 1)[-1] != self.language:
            raise ValueError(f'query intent {self.intent_id!r} does not match query language {self.language!r}')
        return self

class SearchPass(_ResearchModel):
    """Record one search round for a category-stage sweep cell."""
    id: str = Field(pattern=_STABLE_ID_PATTERN)
    run_on: date
    queries: list[SearchQueryRecord] = Field(min_length=1)
    candidate_ids: list[str]
    new_candidate_ids: list[str]
    complete: bool
    note: str | None = None

    @field_validator('candidate_ids', 'new_candidate_ids')
    @classmethod
    def validate_candidate_ids(cls, values: list[str]) -> list[str]:
        """Require unique candidate identifiers with the ledger prefix."""
        _require_unique(values, 'search pass candidate IDs')
        for value in values:
            if re.fullmatch(_CANDIDATE_ID_PATTERN, value) is None:
                raise ValueError('candidate IDs must use the candidate-... form')
        return values

    @model_validator(mode='after')
    def validate_search_pass(self) -> SearchPass:
        """Validate new-candidate membership and pass completion."""
        unknown = set(self.new_candidate_ids) - set(self.candidate_ids)
        if unknown:
            raise ValueError(f'new_candidate_ids must also occur in candidate_ids: {sorted(unknown)!r}')
        if self.complete and any(not query.completed for query in self.queries):
            raise ValueError('a complete search pass cannot contain incomplete queries')
        if self.complete:
            self._validate_required_coverage()
        return self

    def _validate_required_coverage(self) -> None:
        match = re.match('^pass-(corpus|tool|dictionary)-(ohg|mhg|enhg)-', self.id)
        if match is None:
            raise ValueError('complete pass ID must identify category and stage')
        category, stage = match.groups()
        channels = {query.channel for query in self.queries}
        missing_channels = [name for name, aliases in _CHANNELS.items() if channels.isdisjoint(aliases)]
        if missing_channels:
            raise ValueError(f'complete pass is missing required channels {missing_channels!r}')
        if any(query.intent_id is not None for query in self.queries):
            self._validate_intent_coverage(category, stage)
        else:
            self._validate_substring_coverage(category, stage)
        german_web = any(query.language == 'de' and query.channel in _CHANNELS['web_de'] for query in self.queries)
        english_web = any(query.language == 'en' and query.channel in _CHANNELS['web_en'] for query in self.queries)
        if not german_web or not english_web:
            raise ValueError('complete pass requires German and English web search')

    def _validate_intent_coverage(self, category: str, stage: str) -> None:
        """Prove completed coverage from typed intent records, not substrings.

        Each query record contributes at most its single declared intent, so a
        term-stuffed query can never satisfy more than one required intent.
        """
        declared: set[str] = set()
        for query in self.queries:
            if query.intent_id is None:
                continue
            parsed = parse_intent_id(query.intent_id)
            if parsed is None:
                raise ValueError(f'query intent {query.intent_id!r} is not a valid intent id')
            intent_category, intent_stage, _family, intent_language = parsed
            if intent_category != category or intent_stage != stage:
                raise ValueError(f'query intent {query.intent_id!r} does not target pass {category}-{stage}')
            if intent_language != query.language:
                raise ValueError(f'query intent {query.intent_id!r} does not match query language {query.language!r}')
            declared.add(query.intent_id)
        missing = required_intent_ids(category, stage) - declared
        if missing:
            raise ValueError(f'complete pass is missing required query intents {sorted(missing)!r}')

    def _validate_substring_coverage(self, category: str, stage: str) -> None:
        """Validate legacy records lacking intent ids by canonical substring."""
        languages: tuple[SearchLanguage, ...] = ('de', 'en')
        for language in languages:
            texts = [query.query.casefold() for query in self.queries if query.language == language]
            missing_pairs = [f'{stage_term} + {category_term}' for stage_term in stage_terms(stage, language) for category_term in category_terms(category, language) if not any(stage_term.casefold() in text and category_term.casefold() in text for text in texts)]
            if missing_pairs:
                raise ValueError(f'complete pass is missing {language} query families {missing_pairs!r}')
            if category == 'tool':
                missing_architectures = [term for term in architecture_terms(language) if not any(term.casefold() in text for text in texts)]
                if missing_architectures:
                    raise ValueError(f'complete pass is missing {language} tool architecture families {missing_architectures!r}')

class CandidateEntry(_ResearchModel):
    """Represent one durable evaluated discovery candidate."""
    id: str = Field(pattern=_CANDIDATE_ID_PATTERN)
    name: str
    aliases: list[AuthoredSeedText] = Field(default_factory=list)
    source_wordings: list[AuthoredSeedText] = Field(default_factory=list)
    category: ResourceCategory
    discovered_on: date
    last_checked_on: date
    discovery_urls: list[PublicHttpUrl] = Field(min_length=1)
    discovery_stage_claims: list[str] | None = None
    disposition: CandidateDisposition
    resource_id: str | None = Field(default=None, pattern=_STABLE_ID_PATTERN)
    disposition_reason: str | None = None
    evidence_gaps: list[str] | None = None
    refreshed_existing: bool

    @field_validator('discovery_urls')
    @classmethod
    def validate_discovery_urls(cls, values: list[HttpUrl]) -> list[HttpUrl]:
        """Require unique HTTP(S) discovery URLs."""
        _require_unique([str(value) for value in values], 'discovery URLs')
        return values

    @field_validator('aliases', 'source_wordings')
    @classmethod
    def validate_seed_text(cls, values: list[str], info: Any) -> list[str]:
        """Retain authored seed text in order without collapsing distinct values."""
        _require_unique(values, info.field_name.replace('_', ' '))
        return values

    @model_validator(mode='after')
    def validate_disposition(self) -> CandidateEntry:
        """Validate evidence requirements for the candidate disposition."""
        if self.disposition in {'added', 'duplicate'} and self.resource_id is None:
            raise ValueError(f'{self.disposition} requires resource_id')
        if self.disposition == 'out_of_scope' and self.disposition_reason is None:
            raise ValueError('out_of_scope requires disposition_reason')
        if self.disposition == 'blocked' and (not self.evidence_gaps):
            raise ValueError('blocked requires at least one evidence gap')
        if self.refreshed_existing and (self.disposition != 'duplicate' or self.resource_id is None):
            raise ValueError('refreshed_existing requires a duplicate matched resource_id')
        if self.resource_id is not None:
            _require_category_prefix(self.resource_id, self.category, 'candidate resource_id')
        return self

class SweepEntry(_ResearchModel):
    """Represent one category-stage cell in the discovery sweep matrix."""
    id: str = Field(pattern=_STABLE_ID_PATTERN)
    category: ResourceCategory
    stage: LanguageStage
    state: SweepState
    pass_count: int = Field(ge=0)
    consecutive_empty_passes: int = Field(ge=0, le=2)
    last_run_on: date | None = None
    passes: list[SearchPass]

    @model_validator(mode='after')
    def validate_sweep(self) -> SweepEntry:
        """Validate identity, ordering, and pass-derived sweep counters."""
        expected_id = f'{self.category}-{self.stage.value}'
        if self.id != expected_id:
            raise ValueError(f'sweep id must be {expected_id!r}')
        if self.pass_count != len(self.passes):
            raise ValueError('pass_count must equal the number of passes')
        pass_ids = [search_pass.id for search_pass in self.passes]
        _require_unique(pass_ids, 'pass IDs within a sweep')
        run_dates = [search_pass.run_on for search_pass in self.passes]
        if run_dates != sorted(run_dates):
            raise ValueError('passes must be ordered by run_on')
        expected_last_run = run_dates[-1] if run_dates else None
        if self.last_run_on != expected_last_run:
            raise ValueError('last_run_on must match the final pass')
        trailing_empty = 0
        for search_pass in reversed(self.passes):
            if not search_pass.complete or search_pass.new_candidate_ids:
                break
            trailing_empty = min(2, trailing_empty + 1)
        if self.consecutive_empty_passes != trailing_empty:
            raise ValueError('consecutive_empty_passes must match trailing complete empty passes')
        if self.state == 'not_started' and self.passes:
            raise ValueError('not_started sweeps cannot contain passes')
        if self.state == 'complete' and self.consecutive_empty_passes != 2:
            raise ValueError('complete sweeps require two consecutive empty passes')
        return self

class DiscoveryLedger(_ResearchModel):
    """Represent the complete durable discovery ledger."""
    schema_version: Literal[1]
    revision: int = Field(ge=0)
    initialized_on: date
    updated_on: date
    sweeps: list[SweepEntry] = Field(min_length=9, max_length=9)
    candidates: list[CandidateEntry]

    @model_validator(mode='after')
    def validate_ledger(self) -> DiscoveryLedger:
        """Validate matrix coverage and all cross-record references."""
        expected_cells = {(category, stage) for category in ('corpus', 'tool', 'dictionary') for stage in LanguageStage}
        actual_cells = {(sweep.category, sweep.stage) for sweep in self.sweeps}
        if actual_cells != expected_cells:
            raise ValueError('sweeps must contain every category-stage pair exactly once')
        _require_unique([sweep.id for sweep in self.sweeps], 'ledger sweep IDs')
        _require_unique([search_pass.id for sweep in self.sweeps for search_pass in sweep.passes], 'ledger pass IDs')
        _require_unique([candidate.id for candidate in self.candidates], 'ledger candidate IDs')
        candidates = {candidate.id: candidate for candidate in self.candidates}
        for sweep in self.sweeps:
            discovered_ids = {candidate_id for search_pass in sweep.passes for candidate_id in search_pass.candidate_ids}
            missing = discovered_ids - candidates.keys()
            if missing:
                raise ValueError(f'sweep {sweep.id!r} references unknown candidates {sorted(missing)!r}')
            wrong_category = sorted(candidate_id for candidate_id in discovered_ids if candidates[candidate_id].category != sweep.category)
            if wrong_category:
                raise ValueError(f'sweep {sweep.id!r} references candidates from another category {wrong_category!r}')
            if sweep.state == 'complete':
                unfinished = sorted(candidate_id for candidate_id in discovered_ids if candidates[candidate_id].disposition not in _FINAL_DISPOSITIONS)
                if unfinished:
                    raise ValueError(f'complete sweep {sweep.id!r} has pending candidates {unfinished!r}')
        return self

class EvidenceExcerpt(_ResearchModel):
    """Represent one transient canonical or primary evidence excerpt."""
    url: PublicHttpUrl
    accessed_on: date
    kind: SourceKind
    supports: list[str] = Field(min_length=1)
    title: str | None = None
    quote: str | None = None
    note: str | None = None

    @field_validator('supports')
    @classmethod
    def validate_supports(cls, values: list[str]) -> list[str]:
        """Require unique dotted support names."""
        _require_unique(values, 'evidence support names')
        if any(_SUPPORT_RE.fullmatch(value) is None for value in values):
            raise ValueError('supports entries must be dotted support names')
        return values
type _ProposedRecord = Corpus | Tool | Dictionary
type _ResearchDisposition = Literal['added', 'duplicate', 'out_of_scope', 'blocked']

class CandidateResearchResult(_ResearchModel):
    """Represent the sole accepted worker-to-coordinator result."""
    candidate_id: str = Field(pattern=_CANDIDATE_ID_PATTERN)
    category: ResourceCategory
    disposition: _ResearchDisposition
    canonical_name: str | None = None
    verified_stages: list[LanguageStage]
    evidence: list[EvidenceExcerpt]
    evidence_gaps: list[str]
    matched_resource_id: str | None = Field(default=None, pattern=_STABLE_ID_PATTERN)
    proposed_record: _ProposedRecord | None = None
    risk_flags: list[RiskFlag]
    summary: str

    @field_validator('verified_stages')
    @classmethod
    def validate_verified_stages(cls, values: list[LanguageStage]) -> list[LanguageStage]:
        """Require unique directly evidenced stages."""
        _require_unique([value.value for value in values], 'verified stages')
        return values

    @field_validator('risk_flags')
    @classmethod
    def validate_risk_flags(cls, values: list[RiskFlag]) -> list[RiskFlag]:
        """Require unique risk flags."""
        _require_unique(values, 'risk flags')
        return values

    @model_validator(mode='after')
    def validate_result(self) -> CandidateResearchResult:
        """Validate disposition, identity, stage, category, and direct legal evidence."""
        expected_type = {'corpus': Corpus, 'tool': Tool, 'dictionary': Dictionary}[self.category]
        if self.proposed_record is not None and (not isinstance(self.proposed_record, expected_type)):
            raise ValueError('proposed_record type must match category')
        if self.matched_resource_id is not None:
            _require_category_prefix(self.matched_resource_id, self.category, 'matched_resource_id')
            if self.disposition != 'duplicate' and 'identity_conflict' not in self.risk_flags:
                raise ValueError('a matched_resource_id outside a duplicate is identity ambiguity requiring the identity_conflict risk flag')
        if 'identity_conflict' in self.risk_flags and self.disposition == 'added':
            raise ValueError('identity ambiguity flagged identity_conflict cannot produce an added result')
        if self.disposition == 'added':
            if not self.verified_stages:
                raise ValueError('added requires an in-scope verified stage')
            if not self.evidence:
                raise ValueError('added requires evidence')
            if self.proposed_record is None:
                raise ValueError('added requires a proposed_record')
            _require_category_prefix(self.proposed_record.id, self.category, 'proposed record ID')
            self._validate_stage_evidence()
        if self.disposition == 'duplicate' and self.matched_resource_id is None:
            raise ValueError('duplicate requires matched_resource_id')
        if self.disposition == 'out_of_scope':
            if self.verified_stages:
                raise ValueError('out_of_scope cannot have an in-scope verified stage')
            if not self.evidence:
                raise ValueError('out_of_scope requires direct evidence')
        if self.disposition == 'blocked' and (not self.evidence_gaps):
            raise ValueError('blocked requires one or more evidence gaps')
        if self.proposed_record is not None:
            self._validate_legal_evidence()
        return self

    def _validate_stage_evidence(self) -> None:
        """Require canonical excerpt evidence grounding every added verified stage."""
        support_field = _STAGE_SUPPORT_FIELDS[self.category]
        grounded = {support for excerpt in self.evidence for support in excerpt.supports}
        for stage in self.verified_stages:
            support = f'{support_field}.{stage.value}'
            if support not in grounded:
                raise ValueError(f'added verified stage {stage.value!r} requires canonical evidence with supports={support!r}')

    def _validate_legal_evidence(self) -> None:
        """Require matching quoted worker evidence for direct legal claims."""
        if self.proposed_record is None:
            return
        access = self.proposed_record.access
        sources = {source.id: source for source in self.proposed_record.sources}
        for field_name in ('model_training', 'original_data_redistribution', 'processed_data_redistribution', 'trained_weight_publication'):
            if getattr(access, field_name) is LegalPermission.UNCLEAR:
                continue
            support = f'access.{field_name}'
            matching_sources = [sources[source_id] for source_id in access.source_ids or [] if source_id in sources and support in sources[source_id].supports and (sources[source_id].quote is not None)]
            if not any(support in excerpt.supports and excerpt.quote == source.quote and (str(excerpt.url) == str(source.url)) for source in matching_sources for excerpt in self.evidence):
                raise ValueError(f'{field_name} requires matching quoted worker evidence')
