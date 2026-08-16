"""Executable budget for behavioral phrase-presence contract assertions.

``TASK-MIG-001`` froze the exact behavioral phrase-presence assertion count
under ``tests/agent/test_*.py`` at **149** as the sole denominator for the
``TASK-MIG-011`` reduction target. ``TASK-MIG-011`` requires that count to be at
least 50% lower, i.e. no greater than ``floor(149 * 0.5) == 74``.

This test reproduces the frozen counting method with an AST parser so the
reduction is a machine-checked contract rather than a one-off measurement, and
so the migration cannot silently regress by reintroducing brittle exact-wording
assertions. The counting method matches the recorded denominator exactly:

* every phrase argument of ``assert_phrases()``;
* every ``assert "literal" in <text>``;
* every ``for v in (literals): assert v in <text>``.

Structural ``in`` checks against packaging containers (source-exclude
``exclusions``) and against computed collections (set/list/dict literals or
comprehensions) test data shapes, not agent/skill/doc contract text, so they are
excluded.
"""

from __future__ import annotations

import ast
from pathlib import Path

AGENT_TESTS_DIR = Path(__file__).parent
FROZEN_BASELINE = 149
REDUCTION_CEILING = FROZEN_BASELINE // 2  # 74: at least 50% lower than 149.

# Structural container names whose `in` checks are packaging assertions.
_STRUCTURAL_HINTS = ("exclusions", "source-exclude")

# `in` checks whose right operand is a computed collection assert against data
# structures, not agent/skill/doc contract text.
_COLLECTION_RHS = (
    ast.Set,
    ast.SetComp,
    ast.ListComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.List,
    ast.Tuple,
    ast.Dict,
)


def _string_elements(node: ast.AST) -> list[str] | None:
    if isinstance(node, (ast.Tuple, ast.List)):
        out: list[str] = []
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                out.append(elt.value)
            else:
                return None
        return out
    return None


def _is_contract_text_rhs(node: ast.AST) -> bool:
    return not isinstance(node, _COLLECTION_RHS)


def _count_file(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    count = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "assert_phrases"
            and len(node.args) >= 2
        ):
            phrases = _string_elements(node.args[1])
            if phrases is not None:
                count += len(phrases)

        if isinstance(node, ast.For):
            elems = _string_elements(node.iter)
            if (
                elems is not None
                and isinstance(node.target, ast.Name)
                and len(node.body) == 1
                and isinstance(node.body[0], ast.Assert)
            ):
                test = node.body[0].test
                if (
                    isinstance(test, ast.Compare)
                    and len(test.ops) == 1
                    and isinstance(test.ops[0], ast.In)
                    and isinstance(test.left, ast.Name)
                    and test.left.id == node.target.id
                ):
                    right = test.comparators[0]
                    right_src = ast.unparse(right)
                    if not any(
                        h in right_src for h in _STRUCTURAL_HINTS
                    ) and _is_contract_text_rhs(right):
                        count += len(elems)

        if isinstance(node, ast.Assert):
            test = node.test
            if (
                isinstance(test, ast.Compare)
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.In)
                and isinstance(test.left, ast.Constant)
                and isinstance(test.left.value, str)
            ):
                right = test.comparators[0]
                right_src = ast.unparse(right)
                if not any(
                    h in right_src for h in _STRUCTURAL_HINTS
                ) and _is_contract_text_rhs(right):
                    count += 1
    return count


def count_behavioral_phrase_presence_assertions() -> int:
    return sum(_count_file(path) for path in sorted(AGENT_TESTS_DIR.glob("test_*.py")))


def test_behavioral_phrase_presence_assertions_meet_reduction_target() -> None:
    """The counted assertions stay at or below the 50%-reduction ceiling."""

    actual = count_behavioral_phrase_presence_assertions()
    # At least 50% lower than the frozen 149 baseline: 2 * actual <= 149 holds
    # exactly when actual <= 74.
    assert 2 * actual <= FROZEN_BASELINE, (actual, REDUCTION_CEILING)
    assert actual <= REDUCTION_CEILING, (actual, REDUCTION_CEILING)
