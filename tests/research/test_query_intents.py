"""TASK-MIG-003: canonical structured query-intent registry and coverage.

These tests cover the query-intent registry itself:

* the registry is canonical, unique, and its legacy substring views reproduce
  the exact term tables the coverage gate previously embedded (so nothing is
  dual-maintained),
* the coverage matrix lists the required intents per category-stage cell,
* concept classification and identifier parsing are deterministic, and
* the artifact hashes recorded in ``migration-state.json`` are reproducible
  from this tree.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import pass_data

from histgerm.research import load_ledger
from histgerm.research.models import SearchPass
from histgerm.research.query_intents import (
    INTENT_BY_ID,
    INTENT_REGISTRY,
    architecture_terms,
    category_terms,
    classify_intent,
    coverage_matrix,
    format_intent_id,
    is_registered_intent,
    parse_intent_id,
    registry_snapshot,
    required_intent_ids,
    stage_terms,
)

ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = ROOT / "migration-state.json"
LIVE_LEDGER = ROOT / "research" / "discovery-ledger.yaml"

# The exact substring tables the legacy coverage gate embedded before the
# registry existed. The registry must reproduce them so the migration does not
# dual-maintain discovery vocabulary.
_LEGACY_STAGE_TERMS: dict[str, dict[str, tuple[str, ...]]] = {
    "ohg": {"de": ("Althochdeutsch", "OHG"), "en": ("Old High German", "OHG")},
    "mhg": {"de": ("Mittelhochdeutsch", "MHG"), "en": ("Middle High German", "MHG")},
    "enhg": {
        "de": ("Frühneuhochdeutsch", "ENHG"),
        "en": ("Early New High German", "ENHG"),
    },
}
_LEGACY_CATEGORY_TERMS: dict[str, dict[str, tuple[str, ...]]] = {
    "corpus": {
        "de": ("Korpus", "Textkorpus", "Textsammlung", "Sprachdaten"),
        "en": ("corpus", "text collection", "dataset", "language data"),
    },
    "tool": {
        "de": ("Tagger", "Lemmatisierer", "Parser", "Sprachmodell"),
        "en": ("tagger", "lemmatizer", "parser", "language model"),
    },
    "dictionary": {
        "de": ("Wörterbuch", "Lexikon", "Wortschatz"),
        "en": ("dictionary", "lexicon", "vocabulary"),
    },
}
_LEGACY_ARCHITECTURE_TERMS: dict[str, tuple[str, ...]] = {
    "de": (
        "Tokenizer",
        "BERT-Architektur",
        "BERT-Modellfamilie",
        "vortrainiertes Sprachmodell",
        "maskiertes Sprachmodell",
        "Worteinbettung",
    ),
    "en": (
        "tokenizer",
        "BERT architecture",
        "BERT family",
        "pretrained language model",
        "masked language model",
        "word embedding",
    ),
}

CHANNEL_CLASSES = (
    "web_de",
    "web_en",
    "clarin",
    "olac",
    "zenodo",
    "institutional",
    "github",
    "huggingface",
)


# --------------------------------------------------------------------------- #
# Reusable structured-pass builders (shared with test_models and recording).   #
# --------------------------------------------------------------------------- #
def structured_pass_data(
    *, category: str = "tool", stage: str = "mhg", suffix: str = "intents"
) -> dict[str, Any]:
    """Build a complete pass that covers every required intent and channel."""

    required = sorted(required_intent_ids(category, stage))
    rotation = ["clarin", "olac", "zenodo", "institutional", "github", "huggingface"]
    assigned_web = {"de": False, "en": False}
    queries: list[dict[str, Any]] = []
    rotation_index = 0
    for intent_id in required:
        parsed = parse_intent_id(intent_id)
        assert parsed is not None
        language = parsed[3]
        if not assigned_web[language]:
            channel = "web_de" if language == "de" else "web_en"
            assigned_web[language] = True
        else:
            channel = rotation[rotation_index % len(rotation)]
            rotation_index += 1
        queries.append(
            {
                "query": f"{language} {channel} {intent_id}",
                "language": language,
                "channel": channel,
                "source_urls": [f"https://example.org/{channel}"],
                "completed": True,
                "intent_id": intent_id,
            }
        )
    present = {query["channel"] for query in queries}
    for channel in CHANNEL_CLASSES:
        if channel in present:
            continue
        intent_id = required[0]
        parsed = parse_intent_id(intent_id)
        assert parsed is not None
        queries.append(
            {
                "query": f"{parsed[3]} {channel} {intent_id}",
                "language": parsed[3],
                "channel": channel,
                "source_urls": [f"https://example.org/{channel}"],
                "completed": True,
                "intent_id": intent_id,
            }
        )
    return {
        "id": f"pass-{category}-{stage}-{suffix}",
        "run_on": "2026-08-12",
        "queries": queries,
        "candidate_ids": [],
        "new_candidate_ids": [],
        "complete": True,
    }


def term_stuffed_pass_data(
    *, category: str = "tool", stage: str = "mhg"
) -> dict[str, Any]:
    """Return a legacy term-stuffed pass with a single declared intent."""

    data = pass_data(category=category, stage=stage, suffix="stuffed")
    required = sorted(required_intent_ids(category, stage))
    for query in data["queries"]:
        parsed_language = query["language"]
        for intent_id in required:
            parsed = parse_intent_id(intent_id)
            assert parsed is not None
            if parsed[3] == parsed_language:
                query["intent_id"] = intent_id
                break
    return data


# --------------------------------------------------------------------------- #
# Canonical migration artifacts (reproduced when recording the hashes).        #
# --------------------------------------------------------------------------- #
def compatibility_artifact() -> dict[str, Any]:
    """Return the durable read-compatibility contract for TASK-MIG-003."""

    return {
        "intent_field": {"name": "intent_id", "optional": True, "default": None},
        "records_without_intent_use_substring_path": True,
        "records_with_intent_use_structured_path": True,
        "no_registry_dual_maintained": True,
        "existing_ledger": {
            "path": "research/discovery-ledger.yaml",
            "revision": 95,
            "validates_without_mutation": True,
        },
    }


def term_stuffing_artifact() -> dict[str, Any]:
    """Return the durable term-stuffing rejection/completion outcome."""

    return {
        "category": "tool",
        "stage": "mhg",
        "required_intents": 20,
        "term_stuffed_single_intent": {
            "declared_intents": 1,
            "completes": False,
            "error_contains": "missing required query intents",
        },
        "legacy_substring_same_text_completes": True,
        "per_intent_records": {"declared_intents": 20, "completes": True},
    }


def _sha256(payload: object) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def artifact_hashes() -> dict[str, str]:
    """Return the four TASK-MIG-003 artifact hashes computed from this tree."""

    return {
        "registry_schema_sha256": _sha256(registry_snapshot()),
        "coverage_matrix_sha256": _sha256(coverage_matrix()),
        "compatibility_result_sha256": _sha256(compatibility_artifact()),
        "term_stuffing_outcome_sha256": _sha256(term_stuffing_artifact()),
    }


# --------------------------------------------------------------------------- #
# Registry structure                                                          #
# --------------------------------------------------------------------------- #
def test_registry_is_canonical_and_unique() -> None:
    assert len(INTENT_REGISTRY) == 84
    identifiers = [intent.intent_id for intent in INTENT_REGISTRY]
    assert len(identifiers) == len(set(identifiers))
    assert set(identifiers) == set(INTENT_BY_ID)
    for intent in INTENT_REGISTRY:
        assert intent.intent_id == format_intent_id(
            intent.category, intent.stage, intent.family, intent.language
        )
        assert intent.stage_terms == stage_terms(intent.stage, intent.language)
        assert intent.concept_terms


def test_legacy_substring_views_match_registry() -> None:
    for stage, languages in _LEGACY_STAGE_TERMS.items():
        for language, terms in languages.items():
            assert stage_terms(stage, language) == terms
    for category, languages in _LEGACY_CATEGORY_TERMS.items():
        for language, terms in languages.items():
            assert category_terms(category, language) == terms
    for language, terms in _LEGACY_ARCHITECTURE_TERMS.items():
        assert architecture_terms(language) == terms


def test_coverage_matrix_lists_required_intents() -> None:
    matrix = coverage_matrix()
    assert set(matrix) == {
        f"{category}-{stage}"
        for category in ("corpus", "tool", "dictionary")
        for stage in ("ohg", "mhg", "enhg")
    }
    assert len(matrix["tool-mhg"]) == 20
    assert len(matrix["corpus-mhg"]) == 4
    assert len(matrix["dictionary-mhg"]) == 4
    for cell, identifiers in matrix.items():
        category, stage = cell.split("-")
        assert set(identifiers) == set(required_intent_ids(category, stage))
        assert identifiers == sorted(identifiers)
        assert all(is_registered_intent(identifier) for identifier in identifiers)


def test_classify_intent_maps_concepts_and_generation_families() -> None:
    # A canonical concept resolves to its required intent regardless of the
    # authored focused family.
    assert (
        classify_intent("tool", "mhg", "de", "Tokenizer", "segmentation")
        == "intent-tool-mhg-tokenization-de"
    )
    assert (
        classify_intent("tool", "mhg", "en", "BERT family", "models")
        == "intent-tool-mhg-bert_family-en"
    )
    # A non-required concept keeps its own focused family as a generation intent.
    generation = classify_intent(
        "tool", "mhg", "de", "morphologische Annotation", "morphology"
    )
    assert generation == "intent-tool-mhg-morphology-de"
    assert not is_registered_intent(generation)


def test_parse_intent_id_roundtrip_and_rejects_malformed() -> None:
    for intent in INTENT_REGISTRY:
        parsed = parse_intent_id(intent.intent_id)
        assert parsed == (
            intent.category,
            intent.stage,
            intent.family,
            intent.language,
        )
    assert parse_intent_id("intent-tool-mhg-tagging-fr") is None
    assert parse_intent_id("intent-tool-xxx-tagging-de") is None
    assert parse_intent_id("tagging") is None


# --------------------------------------------------------------------------- #
# Read compatibility with the existing ledger                                 #
# --------------------------------------------------------------------------- #
def test_existing_ledger_validates_without_intents() -> None:
    ledger = load_ledger(LIVE_LEDGER)
    assert ledger.schema_version == 1
    records = [
        record
        for sweep in ledger.sweeps
        for search_pass in sweep.passes
        for record in search_pass.queries
    ]
    assert records, "the live ledger should contain recorded query records"
    assert all(record.intent_id is None for record in records)


def test_structured_and_legacy_passes_both_validate() -> None:
    structured = SearchPass.model_validate(structured_pass_data())
    assert structured.complete
    assert any(query.intent_id is not None for query in structured.queries)
    legacy = SearchPass.model_validate(pass_data(category="tool"))
    assert legacy.complete
    assert all(query.intent_id is None for query in legacy.queries)


# --------------------------------------------------------------------------- #
# Artifact hashes recorded in migration-state.json                            #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        pytest.skip("migration-state.json is absent on a fresh checkout")
    data: dict[str, Any] = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return data


def test_recorded_artifact_hashes_match(state: dict[str, Any]) -> None:
    recorded = state["artifacts"]["query_intents"]
    computed = artifact_hashes()
    for name, digest in computed.items():
        assert recorded[name] == digest, name
