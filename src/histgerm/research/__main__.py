# fmt: off
# ruff: noqa: E501
"""JSON command-line interface for the discovery ledger."""
from __future__ import annotations

import argparse
import json
import sys
from calendar import monthrange
from datetime import date
from pathlib import Path
from typing import Any, NoReturn

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ValidationError

from histgerm.models import LanguageStage

from .discovery_orchestration import (
    DiscoveryConfig,
    DiscoveryDependencies,
    run_discovery,
)
from .ledger import (
    LedgerPolicyError,
    LedgerRevisionError,
    LedgerWriteError,
    apply_research_result,
    initialize_ledger,
    record_search_pass,
    select_next_sweep,
    upsert_candidate,
    validate_ledger,
)
from .models import CandidateEntry, CandidateResearchResult, DiscoveryLedger, SearchPass

_DEFAULT_LEDGER = Path('research') / 'discovery-ledger.yaml'
_MUTATING_COMMANDS = {'record-search', 'upsert-candidate', 'apply-result'}

class _ArgumentError(ValueError):
    """Represent an argparse failure without writing non-JSON output."""

class _CapabilityError(RuntimeError):
    """Report a missing injected external discovery capability."""

class _Parser(argparse.ArgumentParser):

    def error(self, message: str) -> NoReturn:
        raise _ArgumentError(message)

def _parser() -> _Parser:
    parser = _Parser(prog='python -m histgerm.research')
    subparsers = parser.add_subparsers(dest='command', required=True)
    for command in ('bootstrap', 'validate', 'status'):
        child = subparsers.add_parser(command)
        _common_arguments(child)
    next_parser = subparsers.add_parser('next')
    _common_arguments(next_parser)
    next_parser.add_argument('--category', choices=('corpus', 'tool', 'dictionary'))
    next_parser.add_argument('--stage', choices=tuple(stage.value for stage in LanguageStage))
    discover = subparsers.add_parser('discover')
    discover.add_argument('--category', required=True, choices=('corpus', 'tool', 'dictionary'))
    discover.add_argument('--stage', required=True, choices=tuple(stage.value for stage in LanguageStage))
    discover.add_argument('--qualifier', action='append', default=[])
    discover.add_argument('--max-mined-terms', type=int, default=8)
    discover.add_argument('--max-exclusion-groups', type=int, default=2)
    discover.add_argument('--format', choices=('json',), default='json')
    for command in sorted(_MUTATING_COMMANDS):
        child = subparsers.add_parser(command)
        _common_arguments(child)
        child.add_argument('--expected-revision', required=True, type=int)
        child.add_argument('--input', required=True, type=Path)
    return parser

def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--ledger', type=Path, default=_DEFAULT_LEDGER)
    parser.add_argument('--format', choices=('json',), default='json')

def _success(command: str, revision: int, result: dict[str, Any]) -> int:
    _emit({'ok': True, 'command': command, 'revision': revision, 'result': result})
    return 0

def _failure(command: str, code: str, path: str, message: str, exit_code: int) -> int:
    _emit({'ok': False, 'command': command, 'errors': [{'code': code, 'path': path, 'message': message}]})
    return exit_code

def _emit(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(',', ':')))
    sys.stdout.write('\n')

def _read_input(path: Path, model: type[BaseModel]) -> BaseModel:
    try:
        text = path.read_text(encoding='utf-8')
    except UnicodeDecodeError as error:
        raise ValueError('input JSON must be valid UTF-8') from error
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError('input JSON must contain one object')
    return model.model_validate(raw)

def _validation_path(error: ValidationError) -> str:
    first = error.errors()[0]
    return '.'.join(str(part) for part in first['loc']) or 'input'

def _six_months_before(day: date) -> date:
    month_index = day.year * 12 + day.month - 1 - 6
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))

def _status(ledger: DiscoveryLedger) -> dict[str, Any]:
    dispositions = {disposition: sum(candidate.disposition == disposition for candidate in ledger.candidates) for disposition in ('pending', 'added', 'duplicate', 'out_of_scope', 'blocked')}
    blocked = [{'id': candidate.id, 'name': candidate.name, 'category': candidate.category, 'evidence_gaps': candidate.evidence_gaps or []} for candidate in ledger.candidates if candidate.disposition == 'blocked']
    stale_cutoff = _six_months_before(date.today())
    stale_resources = [{'candidate_id': candidate.id, 'resource_id': candidate.resource_id, 'name': candidate.name, 'last_checked_on': candidate.last_checked_on.isoformat()} for candidate in ledger.candidates if candidate.resource_id is not None and candidate.last_checked_on <= stale_cutoff]
    return {'matrix': [{'id': sweep.id, 'state': sweep.state, 'pass_count': sweep.pass_count, 'consecutive_empty_passes': sweep.consecutive_empty_passes, 'last_run_on': sweep.last_run_on.isoformat() if sweep.last_run_on is not None else None} for sweep in ledger.sweeps], 'candidates': {'total': len(ledger.candidates), **dispositions}, 'blocked': blocked, 'stale_resources': stale_resources}

def _run(arguments: argparse.Namespace, discovery_dependencies: DiscoveryDependencies | None) -> int:
    command: str = arguments.command
    if command == 'discover':
        if discovery_dependencies is None:
            raise _CapabilityError('discover requires injected model, retrieval, provider, and inspection capabilities')
        discovery_result = run_discovery(
            DiscoveryConfig(
                category=arguments.category,
                stage=LanguageStage(arguments.stage),
                qualifiers=tuple(arguments.qualifier),
                max_mined_terms=arguments.max_mined_terms,
                max_exclusion_groups=arguments.max_exclusion_groups,
            ),
            discovery_dependencies,
        )
        _emit({'ok': True, 'command': command, 'result': discovery_result.as_json()})
        return 0
    ledger_path: Path = arguments.ledger
    if command == 'bootstrap':
        ledger = initialize_ledger(ledger_path, on=date.today())
        return _success(command, ledger.revision, {'schema_version': ledger.schema_version, 'sweeps': len(ledger.sweeps)})
    ledger = validate_ledger(ledger_path)
    if command == 'validate':
        return _success(command, ledger.revision, {'schema_version': ledger.schema_version, 'sweeps': len(ledger.sweeps), 'candidates': len(ledger.candidates)})
    if command == 'status':
        return _success(command, ledger.revision, _status(ledger))
    if command == 'next':
        sweep = select_next_sweep(ledger, category=arguments.category, stage=LanguageStage(arguments.stage) if arguments.stage is not None else None)
        return _success(command, ledger.revision, sweep.model_dump(mode='json'))
    if arguments.expected_revision < 0:
        raise _ArgumentError('--expected-revision must be non-negative')
    model_by_command: dict[str, type[BaseModel]] = {'record-search': SearchPass, 'upsert-candidate': CandidateEntry, 'apply-result': CandidateResearchResult}
    payload = _read_input(arguments.input, model_by_command[command])
    result: dict[str, Any]
    if command == 'record-search':
        assert isinstance(payload, SearchPass)
        updated = record_search_pass(ledger_path, payload, expected_revision=arguments.expected_revision)
        result = {'search_pass_id': payload.id}
    elif command == 'upsert-candidate':
        assert isinstance(payload, CandidateEntry)
        updated = upsert_candidate(ledger_path, payload, expected_revision=arguments.expected_revision)
        result = {'candidate_id': payload.id, 'disposition': payload.disposition}
    else:
        assert isinstance(payload, CandidateResearchResult)
        updated = apply_research_result(ledger_path, payload, expected_revision=arguments.expected_revision)
        applied = next(candidate for candidate in updated.candidates if candidate.id == payload.candidate_id)
        result = {'candidate_id': applied.id, 'disposition': applied.disposition, 'resource_id': applied.resource_id}
    return _success(command, updated.revision, result)

def main(
    argv: list[str] | None=None,
    *,
    discovery_dependencies: DiscoveryDependencies | None = None,
) -> int:
    """Run one CLI command and emit exactly one JSON response."""
    if argv is None and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raw_arguments = sys.argv[1:] if argv is None else argv
    command = raw_arguments[0] if raw_arguments else ''
    try:
        arguments = _parser().parse_args(raw_arguments)
        return _run(arguments, discovery_dependencies)
    except _ArgumentError as error:
        return _failure(command, 'invalid_arguments', 'arguments', str(error), 2)
    except LedgerRevisionError as error:
        return _failure(command, 'stale_revision', 'revision', str(error), 3)
    except LedgerPolicyError as error:
        return _failure(command, 'policy_violation', 'operation', str(error), 5)
    except _CapabilityError as error:
        return _failure(command, 'capability_unavailable', 'discovery', str(error), 6)
    except (LedgerWriteError, OSError) as error:
        return _failure(command, 'filesystem_error', 'filesystem', str(error), 4)
    except ValidationError as error:
        return _failure(command, 'validation_error', _validation_path(error), str(error), 2)
    except (yaml.YAMLError, yaml.MarkedYAMLError) as error:
        return _failure(command, 'invalid_ledger', 'ledger', str(error), 2)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
        return _failure(command, 'invalid_input', 'input', str(error), 2)
if __name__ == '__main__':
    raise SystemExit(main())
