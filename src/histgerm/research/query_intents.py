"""Canonical structured query intents for focused discovery.

This module is the single source of truth for focused-discovery coverage
vocabulary:

* canonical bilingual stage recall terms,
* the required completion concept families (core plus tool architecture),
* the stable search-channel classes and their accepted aliases, and
* the typed :class:`QueryIntent` registry used to validate completed-pass
  coverage from structured intent records rather than gameable substrings.

The registry replaces the ad-hoc substring tables that previously lived in
``models.py``. Both the structured intent-coverage path and the legacy
substring compatibility path (kept only so existing ledger records without an
``intent_id`` keep validating) draw their required terms from this one
registry, so no discovery vocabulary is dual-maintained.

A :class:`QueryIntent` is identified by exactly one
``(category, stage, family, language)`` cell, so a single authored query can
never satisfy more than one intent. That is the structural property that makes
term-stuffed queries unable to complete a pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

type IntentCategory = Literal["corpus", "tool", "dictionary"]
type IntentLanguage = Literal["de", "en"]
type IntentStage = Literal["ohg", "mhg", "enhg"]
type ConceptKind = Literal["core", "architecture"]

_CATEGORIES: tuple[IntentCategory, ...] = ("corpus", "tool", "dictionary")
_STAGES: tuple[IntentStage, ...] = ("ohg", "mhg", "enhg")
_LANGUAGES: tuple[IntentLanguage, ...] = ("de", "en")

INTENT_ID_PATTERN = (
    r"^intent-(?:corpus|tool|dictionary)-(?:ohg|mhg|enhg)"
    r"-[a-z][a-z0-9_]*-(?:de|en)$"
)
_INTENT_ID_RE = re.compile(
    r"^intent-(corpus|tool|dictionary)-(ohg|mhg|enhg)-([a-z][a-z0-9_]*)-(de|en)$"
)

# Canonical bilingual stage recall terms. The first entry is the spelled-out
# form and the second the controlled abbreviation.
STAGE_TERMS: dict[IntentStage, dict[IntentLanguage, tuple[str, ...]]] = {
    "ohg": {"de": ("Althochdeutsch", "OHG"), "en": ("Old High German", "OHG")},
    "mhg": {"de": ("Mittelhochdeutsch", "MHG"), "en": ("Middle High German", "MHG")},
    "enhg": {
        "de": ("Frühneuhochdeutsch", "ENHG"),
        "en": ("Early New High German", "ENHG"),
    },
}

# Ordered required completion concept families. Flattening each category's
# per-language terms in declaration order reproduces the exact substring tables
# the legacy coverage gate used, so existing ledgers validate unchanged.
type _FamilyTable = tuple[tuple[str, dict[IntentLanguage, tuple[str, ...]]], ...]

_CORE_CONCEPTS: dict[IntentCategory, _FamilyTable] = {
    "corpus": (
        (
            "corpus",
            {"de": ("Korpus", "Textkorpus"), "en": ("corpus", "text collection")},
        ),
        (
            "collection",
            {"de": ("Textsammlung", "Sprachdaten"), "en": ("dataset", "language data")},
        ),
    ),
    "tool": (
        ("tagging", {"de": ("Tagger",), "en": ("tagger",)}),
        ("lemmatization", {"de": ("Lemmatisierer",), "en": ("lemmatizer",)}),
        ("parsing", {"de": ("Parser",), "en": ("parser",)}),
        ("language_model", {"de": ("Sprachmodell",), "en": ("language model",)}),
    ),
    "dictionary": (
        ("dictionary", {"de": ("Wörterbuch",), "en": ("dictionary",)}),
        ("lexicon", {"de": ("Lexikon", "Wortschatz"), "en": ("lexicon", "vocabulary")}),
    ),
}

_ARCHITECTURE_CONCEPTS: dict[IntentCategory, _FamilyTable] = {
    "tool": (
        ("tokenization", {"de": ("Tokenizer",), "en": ("tokenizer",)}),
        (
            "bert_architecture",
            {"de": ("BERT-Architektur",), "en": ("BERT architecture",)},
        ),
        ("bert_family", {"de": ("BERT-Modellfamilie",), "en": ("BERT family",)}),
        (
            "pretrained_language_model",
            {
                "de": ("vortrainiertes Sprachmodell",),
                "en": ("pretrained language model",),
            },
        ),
        (
            "masked_language_model",
            {"de": ("maskiertes Sprachmodell",), "en": ("masked language model",)},
        ),
        ("word_embedding", {"de": ("Worteinbettung",), "en": ("word embedding",)}),
    ),
}

# Stable search-channel classes and the aliases each accepts.
CHANNELS: dict[str, frozenset[str]] = {
    "web_de": frozenset({"web_de", "general_web_de", "german_web"}),
    "web_en": frozenset({"web_en", "general_web_en", "english_web"}),
    "clarin": frozenset({"clarin", "clarin_vlo"}),
    "olac": frozenset({"olac"}),
    "zenodo": frozenset({"zenodo", "research_repositories"}),
    "institutional": frozenset({"institutional", "institutional_catalogs"}),
    "github": frozenset({"github", "github_search"}),
    "huggingface": frozenset({"huggingface", "hugging_face"}),
}


@dataclass(frozen=True, slots=True)
class QueryIntent:
    """One canonical focused-discovery coverage cell.

    Each intent pins exactly one ``(category, stage, family, language)`` cell to
    its canonical stage and concept terms. The ``intent_id`` is the durable,
    deterministic reference stored on search-query records.
    """

    intent_id: str
    category: IntentCategory
    stage: IntentStage
    family: str
    language: IntentLanguage
    kind: ConceptKind
    stage_terms: tuple[str, ...]
    concept_terms: tuple[str, ...]


def format_intent_id(
    category: IntentCategory | str,
    stage: IntentStage | str,
    family: str,
    language: IntentLanguage | str,
) -> str:
    """Return the deterministic intent identifier for one coverage cell."""

    return f"intent-{category}-{stage}-{family}-{language}"


def parse_intent_id(
    value: str,
) -> tuple[IntentCategory, IntentStage, str, IntentLanguage] | None:
    """Return the parsed intent cell, or ``None`` for a malformed identifier."""

    match = _INTENT_ID_RE.match(value)
    if match is None:
        return None
    category, stage, family, language = match.groups()
    return (
        cast_category(category),
        cast_stage(stage),
        family,
        cast_language(language),
    )


def cast_category(value: str) -> IntentCategory:
    if value not in _CATEGORIES:
        raise ValueError(f"unknown intent category {value!r}")
    return value


def cast_stage(value: str) -> IntentStage:
    if value not in _STAGES:
        raise ValueError(f"unknown intent stage {value!r}")
    return value


def cast_language(value: str) -> IntentLanguage:
    if value not in _LANGUAGES:
        raise ValueError(f"unknown intent language {value!r}")
    return value


def _iter_family_tables(
    category: IntentCategory,
) -> tuple[tuple[ConceptKind, _FamilyTable], ...]:
    return (
        ("core", _CORE_CONCEPTS.get(category, ())),
        ("architecture", _ARCHITECTURE_CONCEPTS.get(category, ())),
    )


def _build_registry() -> tuple[QueryIntent, ...]:
    intents: list[QueryIntent] = []
    for category in _CATEGORIES:
        for stage in _STAGES:
            for kind, table in _iter_family_tables(category):
                for family, terms in table:
                    for language in _LANGUAGES:
                        intents.append(
                            QueryIntent(
                                intent_id=format_intent_id(
                                    category, stage, family, language
                                ),
                                category=category,
                                stage=stage,
                                family=family,
                                language=language,
                                kind=kind,
                                stage_terms=STAGE_TERMS[stage][language],
                                concept_terms=terms[language],
                            )
                        )
    return tuple(intents)


def _build_family_index() -> dict[tuple[IntentCategory, IntentLanguage, str], str]:
    index: dict[tuple[IntentCategory, IntentLanguage, str], str] = {}
    for category in _CATEGORIES:
        for _kind, table in _iter_family_tables(category):
            for family, terms in table:
                for language in _LANGUAGES:
                    for term in terms[language]:
                        index[(category, language, term.casefold())] = family
    return index


INTENT_REGISTRY: tuple[QueryIntent, ...] = _build_registry()
INTENT_BY_ID: dict[str, QueryIntent] = {
    intent.intent_id: intent for intent in INTENT_REGISTRY
}
_FAMILY_INDEX: dict[tuple[IntentCategory, IntentLanguage, str], str] = (
    _build_family_index()
)


def is_registered_intent(intent_id: str) -> bool:
    """Return whether ``intent_id`` names a required registry intent."""

    return intent_id in INTENT_BY_ID


def required_intent_ids(
    category: IntentCategory | str, stage: IntentStage | str
) -> frozenset[str]:
    """Return every required intent id for one category-stage pass cell."""

    return frozenset(
        intent.intent_id
        for intent in INTENT_REGISTRY
        if intent.category == category and intent.stage == stage
    )


def classify_intent(
    category: IntentCategory | str,
    stage: IntentStage | str,
    language: IntentLanguage | str,
    concept: str,
    family: str,
) -> str:
    """Return the canonical intent id for one authored focused query.

    A query whose concept is a canonical required concept resolves to that
    required intent; any other query resolves to a generation intent named for
    its own focused family. Either way the result is deterministic and unique to
    one ``(category, stage, family, language)`` cell.
    """

    resolved_family = _FAMILY_INDEX.get(
        (category, language, concept.casefold()),  # type: ignore[arg-type]
        family,
    )
    return format_intent_id(category, stage, resolved_family, language)


def stage_terms(
    stage: IntentStage | str, language: IntentLanguage | str
) -> tuple[str, ...]:
    """Return the canonical stage recall terms (legacy substring view)."""

    return STAGE_TERMS[cast_stage(str(stage))][cast_language(str(language))]


def category_terms(
    category: IntentCategory | str, language: IntentLanguage | str
) -> tuple[str, ...]:
    """Return the flattened required core concept terms (legacy substring view)."""

    resolved = cast_language(str(language))
    return tuple(
        term
        for _family, terms in _CORE_CONCEPTS[cast_category(str(category))]
        for term in terms[resolved]
    )


def architecture_terms(language: IntentLanguage | str) -> tuple[str, ...]:
    """Return the flattened required tool architecture terms (legacy view)."""

    resolved = cast_language(str(language))
    return tuple(
        term
        for _family, terms in _ARCHITECTURE_CONCEPTS["tool"]
        for term in terms[resolved]
    )


def registry_snapshot() -> dict[str, object]:
    """Return a deterministic, JSON-serializable snapshot of the registry."""

    return {
        "intent_id_pattern": INTENT_ID_PATTERN,
        "channels": {
            name: sorted(aliases) for name, aliases in sorted(CHANNELS.items())
        },
        "intents": [
            {
                "intent_id": intent.intent_id,
                "category": intent.category,
                "stage": intent.stage,
                "family": intent.family,
                "language": intent.language,
                "kind": intent.kind,
                "stage_terms": list(intent.stage_terms),
                "concept_terms": list(intent.concept_terms),
            }
            for intent in INTENT_REGISTRY
        ],
    }


def coverage_matrix() -> dict[str, list[str]]:
    """Return the required intent ids per category-stage cell, sorted."""

    matrix: dict[str, list[str]] = {}
    for category in _CATEGORIES:
        for stage in _STAGES:
            matrix[f"{category}-{stage}"] = sorted(required_intent_ids(category, stage))
    return matrix


__all__ = [
    "CHANNELS",
    "INTENT_BY_ID",
    "INTENT_ID_PATTERN",
    "INTENT_REGISTRY",
    "STAGE_TERMS",
    "ConceptKind",
    "IntentCategory",
    "IntentLanguage",
    "IntentStage",
    "QueryIntent",
    "architecture_terms",
    "category_terms",
    "classify_intent",
    "coverage_matrix",
    "format_intent_id",
    "is_registered_intent",
    "parse_intent_id",
    "registry_snapshot",
    "required_intent_ids",
    "stage_terms",
]
