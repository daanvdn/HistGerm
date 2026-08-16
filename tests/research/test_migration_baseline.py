"""TASK-MIG-001 baselines: synthetic fixtures parse and denominators are frozen.

This module fulfils the ``TASK-MIG-001`` acceptance criteria from
``plans/histgerm-curator-architecture-migration.md``:

* every synthetic fixture under ``tests/research/fixtures`` parses with the
  current checked-in models, and
* the precise denominators recorded in ``migration-state.json`` are present and
  unchanged.

The measurement helpers below are the *single reproducible source* for the
frozen denominators. Later migration tasks (``TASK-MIG-010`` code reduction and
``TASK-MIG-011`` phrase-test reduction) must reuse these exact functions to
compute their reduction ratios against the frozen baseline.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest

from histgerm.models.common import LanguageStage
from histgerm.research.elicitation import ElicitationConfig, elicit_candidates
from histgerm.research.ledger import load_ledger
from histgerm.research.search_providers import parse_search_html
from histgerm.research.vocabulary_store import load_vocabulary

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
STATE_PATH = ROOT / "migration-state.json"
RESEARCH_SRC = ROOT / "src" / "histgerm" / "research"
AGENT_TESTS = ROOT / "tests" / "agent"
RESEARCH_TESTS = ROOT / "tests" / "research"

# Structural (non-behavioral) containers searched by ``... in <container>``
# assertions under tests/agent/. These hold packaging configuration, not
# agent/skill contract text, so their membership checks are not behavioral
# phrase-presence assertions.
_STRUCTURAL_CONTAINERS = frozenset({"exclusions", "runtime", "imported"})

MODEL_OUTPUT_CANDIDATE = {"candidates": [{"name": "MhgBERT", "aliases": ["MHG BERT"]}]}

# Frozen TASK-MIG-001 denominators. The authoritative durable copy lives in the
# operational (untracked) ``migration-state.json``; these committed constants
# mirror it so the reduction targets in TASK-MIG-010 and TASK-MIG-011 have a
# version-controlled reference even on a fresh checkout without the state file.
EXPECTED = {
    "discovery_protocol_py_lines": 542,
    "discovery_session_py_lines": 456,
    "combined_lines": 998,
    "research_total_lines": 10394,
    "assert_phrases_phrases": 138,
    "direct_in_policy_asserts": 7,
    "loop_in_policy_phrases": 4,
    "structural_in_phrases_excluded": 5,
    "behavioral_phrase_presence_assertions": 149,
    "synthetic_whole_run_abort_count": 19,
    "synthetic_recovery_count": 2,
}


# --------------------------------------------------------------------------- #
# Reproducible baseline measurement helpers (reused by later migration tasks). #
# --------------------------------------------------------------------------- #
def _line_count(path: Path) -> int:
    """Count logical source lines, independent of CR/LF line-ending style."""

    return len(path.read_text(encoding="utf-8").splitlines())


def combined_target_line_count() -> int:
    """Combined line count of the two TASK-MIG-010 reduction-target modules."""

    return _line_count(RESEARCH_SRC / "discovery_protocol.py") + _line_count(
        RESEARCH_SRC / "discovery_session.py"
    )


def research_total_line_count() -> int:
    """Total top-level ``src/histgerm/research`` line count (informational)."""

    return sum(_line_count(path) for path in sorted(RESEARCH_SRC.glob("*.py")))


def _str_constants(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return [
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    return []


def _container_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        return node.value.id
    return None


def _behavioral_phrase_presence(path: Path) -> int:
    """Count exact phrase-presence assertions against contract text in one file.

    Three deterministic forms are counted:

    * every phrase argument of an ``assert_phrases(policy, (...))`` call,
    * every ``assert "<literal>" in <container>`` assertion, and
    * every ``for value in (<literals>): assert value in <container>`` phrase.

    ``in`` checks whose container is a packaging/structural collection (see
    :data:`_STRUCTURAL_CONTAINERS`) are excluded because they assert build
    configuration, not agent/skill contract wording.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    total = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "assert_phrases"
            and len(node.args) >= 2
        ):
            total += len(_str_constants(node.args[1]))
        if isinstance(node, ast.Assert) and isinstance(node.test, ast.Compare):
            compare = node.test
            right = _container_name(compare.comparators[0]) if compare.ops else None
            if (
                len(compare.ops) == 1
                and isinstance(compare.ops[0], ast.In)
                and isinstance(compare.left, ast.Constant)
                and isinstance(compare.left.value, str)
                and right not in _STRUCTURAL_CONTAINERS
            ):
                total += 1
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            literals = _str_constants(node.iter)
            if not literals:
                continue
            container: str | None = None
            for descendant in ast.walk(node):
                if (
                    isinstance(descendant, ast.Assert)
                    and isinstance(descendant.test, ast.Compare)
                    and len(descendant.test.ops) == 1
                    and isinstance(descendant.test.ops[0], ast.In)
                    and isinstance(descendant.test.left, ast.Name)
                    and descendant.test.left.id == node.target.id
                ):
                    container = _container_name(descendant.test.comparators[0])
                    break
            if container is not None and container not in _STRUCTURAL_CONTAINERS:
                total += len(literals)
    return total


def behavioral_phrase_presence_count() -> int:
    """Behavioral phrase-presence assertion count under ``tests/agent/``."""

    return sum(
        _behavioral_phrase_presence(path)
        for path in sorted(AGENT_TESTS.glob("test_*.py"))
    )


def _raises_protocol_error(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "raises"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pytest"
        and bool(node.args)
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "DiscoveryProtocolError"
    )


def synthetic_whole_run_abort_count() -> int:
    """Current synthetic whole-run abort assertions under ``tests/research/``."""

    total = 0
    for path in sorted(RESEARCH_TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        total += sum(_raises_protocol_error(node) for node in ast.walk(tree))
    return total


def synthetic_recovery_count() -> int:
    """Current synthetic resume/recovery test functions under ``tests/research/``."""

    pattern = re.compile(r"resume|recover", re.IGNORECASE)
    total = 0
    for path in sorted(RESEARCH_TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name.startswith("test_")
                and pattern.search(node.name) is not None
            ):
                total += 1
    return total


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        pytest.skip(
            "migration-state.json is operational, untracked machine state and is "
            "absent on a fresh checkout"
        )
    data: dict[str, Any] = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return data


# --------------------------------------------------------------------------- #
# Fixture-parse acceptance: every synthetic fixture parses with current models #
# --------------------------------------------------------------------------- #
def test_ledger_fixture_parses() -> None:
    ledger = load_ledger(FIXTURES / "ledger.yaml")
    assert ledger.schema_version == 1
    assert len(ledger.sweeps) == 9


def test_vocabulary_fixture_parses() -> None:
    vocabulary = load_vocabulary(FIXTURES / "vocabulary.yaml")
    assert vocabulary.schema_version == 1
    assert vocabulary.sources == []
    assert vocabulary.terms == []


def test_model_output_fixture_parses() -> None:
    raw = (FIXTURES / "model_output.txt").read_text(encoding="utf-8")
    assert json.loads(raw) == MODEL_OUTPUT_CANDIDATE

    def model_call(prompt: str, /) -> str:
        if "additional plausible" in prompt:
            return json.dumps({"candidates": []})
        return raw

    result = elicit_candidates(
        model_call,
        category="tool",
        stage=LanguageStage.MHG,
        trusted_records=(),
        ledger_candidates=(),
        config=ElicitationConfig(),
    )
    assert [lead.name for lead in result.leads] == ["MhgBERT"]


def test_provider_response_fixture_parses() -> None:
    document = (FIXTURES / "provider_response.html").read_text(encoding="utf-8")
    results = parse_search_html(document)
    assert len(results) == 1
    assert results[0].url == "https://example.org/mhg-tagger"
    assert results[0].trusted_evidence is False


# --------------------------------------------------------------------------- #
# Version-controlled baseline guarantees (run everywhere, no state file needed) #
# --------------------------------------------------------------------------- #
def test_frozen_denominators_are_internally_consistent() -> None:
    assert (
        EXPECTED["discovery_protocol_py_lines"] + EXPECTED["discovery_session_py_lines"]
        == EXPECTED["combined_lines"]
    )
    assert (
        EXPECTED["assert_phrases_phrases"]
        + EXPECTED["direct_in_policy_asserts"]
        + EXPECTED["loop_in_policy_phrases"]
        == EXPECTED["behavioral_phrase_presence_assertions"]
    )


def test_measurement_functions_execute() -> None:
    """The reusable measurement helpers stay importable and non-degenerate.

    TASK-MIG-010 and TASK-MIG-011 import these helpers to compute reduction
    ratios against :data:`EXPECTED`. They must never raise. The reduction-target
    line and phrase counts stay positive; ``synthetic_whole_run_abort_count`` is
    now zero because TASK-MIG-010 retired the old-exchange whole-run abort
    taxonomy, so its helper must merely evaluate without raising.
    """

    assert combined_target_line_count() > 0
    assert research_total_line_count() >= combined_target_line_count()
    assert behavioral_phrase_presence_count() > 0
    assert synthetic_whole_run_abort_count() == 0
    assert synthetic_recovery_count() > 0


def test_task_mig_010_combined_reduction_meets_target() -> None:
    """TASK-MIG-010 retired the old exchange; the two target modules shrank >=25%.

    The denominator is the frozen TASK-MIG-001 combined baseline (998). "At least
    25% lower" means the current combined line count must be no greater than
    ``floor(998 * 0.75) == 748``; a count of 749 would be only ~24.9% lower and
    must fail. ``combined_target_line_count`` is the single reproducible measure.
    """

    baseline = EXPECTED["combined_lines"]
    threshold = baseline * 3 // 4  # 748: the largest count that is still >=25% lower
    assert threshold == 748
    combined = combined_target_line_count()
    assert combined <= threshold
    assert (baseline - combined) / baseline >= 0.25


# --------------------------------------------------------------------------- #
# Machine-state acceptance: initial schema and frozen denominators             #
# --------------------------------------------------------------------------- #
def test_migration_state_matches_initial_schema(state: dict[str, Any]) -> None:
    assert state["schema_version"] == 1
    assert state["plan_version"] == 1
    assert isinstance(state["run_id"], str) and state["run_id"]
    assert state["branch"] == f"copilot/histgerm-curator-migration-{state['run_id']}"
    for key in ("tasks", "gates", "commits", "checks", "artifacts"):
        assert isinstance(state[key], dict)
    assert "pilot_target" in state
    valid_status = {"pending", "in_progress", "complete", "rolled_back", "failed"}
    task_ids = {f"TASK-MIG-{index:03d}" for index in range(1, 14)}
    assert set(state["tasks"]) == task_ids
    assert set(state["tasks"].values()) <= valid_status


def test_state_records_frozen_baselines(state: dict[str, Any]) -> None:
    baselines = state["artifacts"]["baselines"]
    reduction = baselines["code_reduction_denominator"]
    assert (
        reduction["discovery_protocol_py_lines"]
        == EXPECTED["discovery_protocol_py_lines"]
    )
    assert (
        reduction["discovery_session_py_lines"]
        == EXPECTED["discovery_session_py_lines"]
    )
    assert reduction["combined_lines"] == EXPECTED["combined_lines"]
    assert (
        baselines["research_total_lines"]["lines"] == EXPECTED["research_total_lines"]
    )
    phrase = baselines["phrase_test_reduction_denominator"]
    assert (
        phrase["behavioral_phrase_presence_assertions"]
        == EXPECTED["behavioral_phrase_presence_assertions"]
    )
    assert phrase["assert_phrases_phrases"] == EXPECTED["assert_phrases_phrases"]
    assert phrase["direct_in_policy_asserts"] == EXPECTED["direct_in_policy_asserts"]
    assert phrase["loop_in_policy_phrases"] == EXPECTED["loop_in_policy_phrases"]
    assert (
        phrase["structural_in_phrases_excluded"]
        == EXPECTED["structural_in_phrases_excluded"]
    )
    assert (
        baselines["synthetic_whole_run_abort_count"]["count"]
        == EXPECTED["synthetic_whole_run_abort_count"]
    )
    assert (
        baselines["synthetic_recovery_count"]["count"]
        == EXPECTED["synthetic_recovery_count"]
    )


def _later_task_started(state: dict[str, Any]) -> bool:
    return any(
        state["tasks"][f"TASK-MIG-{index:03d}"] == "complete" for index in range(2, 14)
    )


def test_measurement_methods_reproduce_frozen_baseline(state: dict[str, Any]) -> None:
    """Prove the frozen denominators are reproducible from the current tree.

    Later migration tasks deliberately change the measured files; once any task
    after ``TASK-MIG-001`` is ``complete`` this equality no longer holds and the
    reduction targets are asserted by those tasks instead.
    """

    if _later_task_started(state):
        pytest.skip("later migration tasks may have changed the measured files")
    baselines = state["artifacts"]["baselines"]
    assert (
        combined_target_line_count()
        == baselines["code_reduction_denominator"]["combined_lines"]
    )
    assert research_total_line_count() == baselines["research_total_lines"]["lines"]
    assert (
        behavioral_phrase_presence_count()
        == baselines["phrase_test_reduction_denominator"][
            "behavioral_phrase_presence_assertions"
        ]
    )
    assert (
        synthetic_whole_run_abort_count()
        == baselines["synthetic_whole_run_abort_count"]["count"]
    )
    assert synthetic_recovery_count() == baselines["synthetic_recovery_count"]["count"]
