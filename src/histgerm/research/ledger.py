# fmt: off
# ruff: noqa: E501
"""Validated, revisioned operations for the discovery ledger."""
from __future__ import annotations

import os
from calendar import monthrange
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from yaml.events import AliasEvent  # type: ignore[import-untyped]
from yaml.nodes import MappingNode, ScalarNode  # type: ignore[import-untyped]
from yaml.tokens import (  # type: ignore[import-untyped]
    AliasToken,
    AnchorToken,
    TagToken,
)

from histgerm.catalog import load_catalog
from histgerm.models import BaseResource, LanguageStage

from ._persistence import (
    bounded_file_lock,
    remove_temporary,
    replace_atomically,
    stable_lock_path,
    write_durable_temporary,
)
from .models import (
    CandidateEntry,
    CandidateResearchResult,
    DiscoveryLedger,
    ResourceCategory,
    SearchPass,
    SweepEntry,
)

type LedgerPath = str | os.PathLike[str]
_CATEGORIES: tuple[ResourceCategory, ...] = ('corpus', 'tool', 'dictionary')
_STAGES: tuple[LanguageStage, ...] = tuple(LanguageStage)
_FINAL_DISPOSITIONS = {'added', 'duplicate', 'out_of_scope', 'blocked'}
_UTF8_BOM = b'\xef\xbb\xbf'
_LOCK_TIMEOUT_SECONDS = 10.0

class LedgerRevisionError(ValueError):
    """Report an optimistic-concurrency revision mismatch."""

class LedgerPolicyError(ValueError):
    """Report an operation that cannot preserve ledger policy."""

class LedgerWriteError(OSError):
    """Report failure to durably create or replace a ledger."""

class _RestrictedLoader(yaml.SafeLoader):  # type: ignore[misc]
    """Load only ordinary YAML without aliases or duplicate mapping keys."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):
            raise yaml.constructor.ConstructorError(None, None, 'YAML aliases are not allowed in discovery ledgers', self.peek_event().start_mark)
        return super().compose_node(parent, index)

    def construct_mapping(self, node: MappingNode, deep: bool=False) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        for key_node, value_node in node.value:
            if not isinstance(key_node, ScalarNode) or key_node.tag != 'tag:yaml.org,2002:str':
                raise yaml.constructor.ConstructorError('while constructing a mapping', node.start_mark, 'mapping keys must be plain strings', key_node.start_mark)
            key = self.construct_scalar(key_node)
            if not key or key != key.strip() or any(ord(character) < 32 for character in key):
                raise yaml.constructor.ConstructorError('while constructing a mapping', node.start_mark, f'found invalid mapping key {key!r}', key_node.start_mark)
            if key in mapping:
                raise yaml.constructor.ConstructorError('while constructing a mapping', node.start_mark, f'found duplicate key {key!r}', key_node.start_mark)
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping

def load_ledger(path: LedgerPath) -> DiscoveryLedger:
    """Load and fully validate one ledger through the restricted YAML loader."""
    ledger_path = Path(path)
    data = ledger_path.read_bytes()
    if data.startswith(_UTF8_BOM):
        raise ValueError('discovery ledger must not contain a UTF-8 BOM')
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as error:
        raise ValueError('discovery ledger must be valid UTF-8') from error
    for token in yaml.scan(text, Loader=yaml.SafeLoader):
        if isinstance(token, AnchorToken):
            feature = 'anchors'
        elif isinstance(token, AliasToken):
            feature = 'aliases'
        elif isinstance(token, TagToken):
            feature = 'explicit tags'
        else:
            continue
        raise yaml.constructor.ConstructorError(None, None, f'YAML {feature} are not allowed in discovery ledgers', token.start_mark)
    raw = yaml.load(text, Loader=_RestrictedLoader)
    if not isinstance(raw, Mapping):
        raise ValueError('discovery ledger YAML must contain one mapping')
    return DiscoveryLedger.model_validate(raw)

def validate_ledger(path: LedgerPath) -> DiscoveryLedger:
    """Load a ledger without mutating it."""
    return load_ledger(path)

def initialize_ledger(path: LedgerPath, *, on: date) -> DiscoveryLedger:
    """Atomically create the authoritative initial ledger without overwriting."""
    ledger = DiscoveryLedger(schema_version=1, revision=0, initialized_on=on, updated_on=on, sweeps=[SweepEntry(id=f'{category}-{stage.value}', category=category, stage=stage, state='not_started', pass_count=0, consecutive_empty_passes=0, passes=[]) for category in _CATEGORIES for stage in _STAGES], candidates=_bootstrap_candidates(on))
    ledger_path = Path(path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _write_temporary(ledger_path, ledger)
    try:
        os.link(temporary, ledger_path)
    except FileExistsError:
        _remove_temporary(temporary)
        raise
    except OSError as error:
        _remove_temporary(temporary)
        raise LedgerWriteError(f'could not create ledger {ledger_path}') from error
    _remove_temporary(temporary)
    return load_ledger(ledger_path)

def _bootstrap_candidates(on: date) -> list[CandidateEntry]:
    """Build initial candidates only from validated trusted inventory records."""
    catalog = load_catalog()
    candidates: list[CandidateEntry] = []
    inventory: tuple[tuple[ResourceCategory, Sequence[BaseResource], str], ...] = (
        ('corpus', catalog.corpora, 'covered_stages'),
        ('tool', catalog.tools, 'supported_stages'),
        ('dictionary', catalog.dictionaries, 'covered_stages'),
    )
    for category, records, stage_field in inventory:
        for record in records:
            discovery_urls = list(dict.fromkeys(str(source.url) for source in record.sources))
            if not discovery_urls:
                raise LedgerPolicyError(f'trusted bootstrap resource {record.id!r} has no source URLs')
            stages = getattr(record, stage_field)
            candidates.append(CandidateEntry(id=f'candidate-{record.id}', name=record.name, aliases=list(record.aliases or []), source_wordings=[record.name], category=category, discovered_on=on, last_checked_on=record.reviewed_on, discovery_urls=discovery_urls, discovery_stage_claims=[stage.value for stage in stages] if stages else None, disposition='added', resource_id=record.id, disposition_reason=record.description, refreshed_existing=False))
    return candidates

def select_next_sweep(ledger: DiscoveryLedger, *, category: ResourceCategory | None=None, stage: LanguageStage | None=None) -> SweepEntry | CandidateEntry:
    """Select unfinished discovery first, then the oldest stale resource."""
    if category is not None and category not in _CATEGORIES:
        raise LedgerPolicyError(f'unknown resource category {category!r}')
    if stage is not None and stage not in _STAGES:
        raise LedgerPolicyError(f'unknown language stage {stage!r}')
    category_order = {value: index for index, value in enumerate(_CATEGORIES)}
    stage_order = {value: index for index, value in enumerate(_STAGES)}
    eligible = [sweep for sweep in ledger.sweeps if sweep.state != 'complete' and (category is None or sweep.category == category) and (stage is None or sweep.stage == stage)]
    if eligible:
        return min(eligible, key=lambda sweep: (sweep.state != 'in_progress', category_order[sweep.category], stage_order[sweep.stage]))
    if category is not None or stage is not None:
        raise LedgerPolicyError('no unfinished sweep matches the requested filters')
    cutoff = _months_before(date.today(), 6)
    stale = [candidate for candidate in ledger.candidates if candidate.resource_id is not None and candidate.last_checked_on <= cutoff and (candidate.disposition in {'added', 'duplicate'})]
    if not stale:
        raise LedgerPolicyError('discovery is complete and no resource is stale')
    return min(stale, key=lambda candidate: (candidate.last_checked_on, candidate.id))

def record_search_pass(path: LedgerPath, search_pass: SearchPass, *, expected_revision: int) -> DiscoveryLedger:
    """Append one typed search pass to the sweep named by its stable pass ID."""
    ledger_path = Path(path)
    with _locked_ledger(ledger_path, expected_revision) as ledger:
        sweep_index = _sweep_index_for_pass(ledger, search_pass)
        sweep = ledger.sweeps[sweep_index]
        if sweep.state == 'complete':
            raise LedgerPolicyError(f'sweep {sweep.id!r} is already complete')
        if any(search_pass.id == existing.id for item in ledger.sweeps for existing in item.passes):
            raise LedgerPolicyError(f'search pass {search_pass.id!r} already exists')
        candidates = {candidate.id: candidate for candidate in ledger.candidates}
        missing = sorted(set(search_pass.candidate_ids) - candidates.keys())
        if missing:
            raise LedgerPolicyError(f'search pass references unknown candidates {missing!r}')
        if search_pass.complete:
            pending = sorted(candidate_id for candidate_id in search_pass.candidate_ids if candidates[candidate_id].disposition not in _FINAL_DISPOSITIONS)
            if pending:
                raise LedgerPolicyError(f'complete search pass has pending candidates {pending!r}')
        passes = [*sweep.passes, search_pass]
        if len(passes) > 1 and passes[-2].run_on > search_pass.run_on:
            raise LedgerPolicyError('search passes must be recorded in run-date order')
        trailing_empty = _trailing_empty_passes(passes)
        sweeps = list(ledger.sweeps)
        sweeps[sweep_index] = SweepEntry(id=sweep.id, category=sweep.category, stage=sweep.stage, state='complete' if trailing_empty == 2 else 'in_progress', pass_count=len(passes), consecutive_empty_passes=trailing_empty, last_run_on=search_pass.run_on, passes=passes)
        updated = ledger.model_copy(update={'updated_on': max(ledger.updated_on, search_pass.run_on), 'sweeps': sweeps})
        return _commit(ledger_path, updated, expected_revision)

def upsert_candidate(path: LedgerPath, candidate: CandidateEntry, *, expected_revision: int) -> DiscoveryLedger:
    """Add or replace one typed candidate and atomically commit the ledger."""
    ledger_path = Path(path)
    with _locked_ledger(ledger_path, expected_revision) as ledger:
        candidates = [existing for existing in ledger.candidates if existing.id != candidate.id]
        candidates.append(candidate)
        updated = ledger.model_copy(update={'updated_on': max(ledger.updated_on, candidate.last_checked_on), 'candidates': candidates})
        return _commit(ledger_path, updated, expected_revision)

def apply_research_result(path: LedgerPath, result: CandidateResearchResult, *, expected_revision: int) -> DiscoveryLedger:
    """Apply one validated worker result to its existing ledger candidate."""
    ledger_path = Path(path)
    with _locked_ledger(ledger_path, expected_revision) as ledger:
        try:
            current = next(candidate for candidate in ledger.candidates if candidate.id == result.candidate_id)
        except StopIteration as error:
            raise LedgerPolicyError(f'candidate {result.candidate_id!r} does not exist') from error
        if current.category != result.category:
            raise LedgerPolicyError('research result category does not match the ledger candidate')
        resource_id: str | None = None
        refreshed_existing = current.refreshed_existing if result.disposition == 'duplicate' else False
        if result.disposition == 'added':
            assert result.proposed_record is not None
            resource_id = result.proposed_record.id
        elif result.disposition == 'duplicate':
            assert result.matched_resource_id is not None
            resource_id = result.matched_resource_id
            if result.proposed_record is not None:
                if result.proposed_record.id != resource_id:
                    raise LedgerPolicyError('a refreshed duplicate record must match matched_resource_id')
                refreshed_existing = True
        checked_on = max([current.last_checked_on, *(item.accessed_on for item in result.evidence)])
        candidate_data = current.model_dump(mode='python')
        candidate_data.update({'name': result.canonical_name or current.name, 'last_checked_on': checked_on, 'disposition': result.disposition, 'resource_id': resource_id, 'disposition_reason': result.summary, 'evidence_gaps': result.evidence_gaps or None, 'refreshed_existing': refreshed_existing})
        applied = CandidateEntry.model_validate(candidate_data)
        candidates = [applied if candidate.id == applied.id else candidate for candidate in ledger.candidates]
        updated = ledger.model_copy(update={'updated_on': max(ledger.updated_on, checked_on), 'candidates': candidates})
        return _commit(ledger_path, updated, expected_revision)

def _months_before(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 - months
    year, month_index = divmod(index, 12)
    month = month_index + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))

@contextmanager
def _locked_ledger(path: Path, expected_revision: int) -> Iterator[DiscoveryLedger]:
    with _ledger_file_lock(_lock_path_for_ledger(path)):
        ledger = load_ledger(path)
        _require_revision(ledger, expected_revision)
        yield ledger

def _lock_path_for_ledger(path: Path) -> Path:
    """Return stable external lock state unique to this ledger checkout path."""
    return stable_lock_path(path, namespace='ledger-locks-v1')

@contextmanager
def _ledger_file_lock(lock: Path) -> Iterator[None]:
    """Hold an OS-backed exclusive lock; the stable lock file is never evicted."""
    with bounded_file_lock(lock, label='ledger', timeout=_LOCK_TIMEOUT_SECONDS, on_timeout=LedgerWriteError):
        yield

def _require_revision(ledger: DiscoveryLedger, expected_revision: int) -> None:
    if ledger.revision != expected_revision:
        raise LedgerRevisionError(f'expected revision {expected_revision}, found {ledger.revision}')

def _sweep_index_for_pass(ledger: DiscoveryLedger, search_pass: SearchPass) -> int:
    matches = [index for index, sweep in enumerate(ledger.sweeps) if search_pass.id.startswith(f'pass-{sweep.id}-')]
    if len(matches) != 1:
        raise LedgerPolicyError("search pass ID must start with 'pass-<category>-<stage>-'")
    return matches[0]

def _trailing_empty_passes(passes: list[SearchPass]) -> int:
    count = 0
    for search_pass in reversed(passes):
        if not search_pass.complete or search_pass.new_candidate_ids:
            break
        count = min(2, count + 1)
    return count

def _canonicalize(ledger: DiscoveryLedger) -> DiscoveryLedger:
    category_order = {value: index for index, value in enumerate(_CATEGORIES)}
    stage_order = {value: index for index, value in enumerate(_STAGES)}
    sweeps = sorted(ledger.sweeps, key=lambda sweep: (category_order[sweep.category], stage_order[sweep.stage]))
    candidates = sorted(ledger.candidates, key=lambda candidate: candidate.id)
    return DiscoveryLedger.model_validate(ledger.model_copy(update={'sweeps': sweeps, 'candidates': candidates}))

def _commit(path: Path, ledger: DiscoveryLedger, expected_revision: int) -> DiscoveryLedger:
    validated = _canonicalize(DiscoveryLedger.model_validate(ledger.model_copy(update={'revision': expected_revision + 1})))
    temporary = _write_temporary(path, validated)
    try:
        replace_atomically(temporary, path)
    except OSError as error:
        raise LedgerWriteError(f'could not atomically replace ledger {path}') from error
    return load_ledger(path)

def _write_temporary(path: Path, ledger: DiscoveryLedger) -> Path:
    try:
        payload = yaml.safe_dump(ledger.model_dump(mode='json', exclude_none=True), sort_keys=False, allow_unicode=True).encode('utf-8')
        return write_durable_temporary(path, payload, prefix=f'.{path.name}.', suffix='.tmp')
    except (OSError, yaml.YAMLError) as error:
        raise LedgerWriteError(f'could not write temporary ledger for {path}') from error

def _remove_temporary(path: Path) -> None:
    try:
        remove_temporary(path)
    except OSError as error:
        raise LedgerWriteError(f'could not remove temporary ledger {path}') from error
__all__ = ['LedgerPolicyError', 'LedgerRevisionError', 'LedgerWriteError', 'apply_research_result', 'initialize_ledger', 'load_ledger', 'record_search_pass', 'select_next_sweep', 'upsert_candidate', 'validate_ledger']
