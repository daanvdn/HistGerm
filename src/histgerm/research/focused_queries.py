"""Focused, bilingual query construction for research discovery."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Literal

from histgerm.models import LanguageStage

type ResourceCategory = Literal["corpus", "tool", "dictionary"]
type QueryLanguage = Literal["de", "en"]

_STAGE_TERMS: dict[LanguageStage, dict[QueryLanguage, str]] = {
    LanguageStage.OHG: {"de": "Althochdeutsch", "en": "Old High German"},
    LanguageStage.MHG: {"de": "Mittelhochdeutsch", "en": "Middle High German"},
    LanguageStage.ENHG: {
        "de": "Frühneuhochdeutsch",
        "en": "Early New High German",
    },
}

_CONCEPTS: dict[ResourceCategory, dict[str, dict[QueryLanguage, tuple[str, ...]]]] = {
    "corpus": {
        "corpus": {
            "de": ("Korpus", "Textkorpus", "Sprachkorpus"),
            "en": ("corpus", "text corpus", "language corpus"),
        },
        "collection": {
            "de": ("Textsammlung", "Sprachdaten", "Textdatenbank"),
            "en": ("text collection", "language data", "text database"),
        },
        "edition": {
            "de": ("digitale Edition", "Textedition"),
            "en": ("digital edition", "text edition"),
        },
        "treebank": {
            "de": ("Baumbank", "syntaktisch annotiertes Korpus"),
            "en": ("treebank", "syntactically annotated corpus"),
        },
    },
    "tool": {
        "tagging": {
            "de": ("Tagger", "POS-Tagger", "Tagging", "Wortartenannotation"),
            "en": ("tagger", "POS tagger", "part-of-speech tagging"),
        },
        "morphology": {
            "de": (
                "morphologische Annotation",
                "morphosyntaktische Annotation",
                "Flexionsanalyse",
            ),
            "en": (
                "morphological annotation",
                "morphosyntactic analysis",
                "morphological analyzer",
            ),
        },
        "lemmatization": {
            "de": ("Lemmatisierer", "Lemmatisierung", "Grundformbestimmung"),
            "en": ("lemmatizer", "lemmatization", "lemma prediction"),
        },
        "normalization": {
            "de": ("Normalisierung", "Schreibvariantennormalisierung"),
            "en": (
                "normalization",
                "spelling normalization",
                "historical spelling normalization",
            ),
        },
        "parsing": {
            "de": ("Parser", "Dependenzparser", "Syntaxanalyse"),
            "en": ("parser", "dependency parser", "syntactic analysis"),
        },
        "segmentation": {
            "de": ("Tokenisierung", "Satzsegmentierung"),
            "en": ("tokenizer", "tokenization", "sentence segmentation"),
        },
        "models": {
            "de": ("Sprachmodell", "Transformer-Modell", "Wortrepräsentation"),
            "en": ("language model", "transformer model", "embeddings"),
        },
        "pipelines": {
            "de": ("NLP-Werkzeug", "Annotationswerkzeug", "Sprachverarbeitung"),
            "en": (
                "NLP tool",
                "annotation tool",
                "language-processing pipeline",
            ),
        },
    },
    "dictionary": {
        "dictionary": {
            "de": ("Wörterbuch", "historisches Wörterbuch", "Online-Wörterbuch"),
            "en": ("dictionary", "historical dictionary", "online dictionary"),
        },
        "lexicon": {
            "de": ("Lexikon", "Wortschatz", "Lemmaliste"),
            "en": ("lexicon", "vocabulary", "lemma list"),
        },
        "glossary": {
            "de": ("Glossar", "Glossensammlung"),
            "en": ("glossary", "gloss collection"),
        },
        "etymology": {
            "de": ("etymologisches Wörterbuch", "Wortgeschichte"),
            "en": ("etymological dictionary", "word history"),
        },
    },
}

_TAGSET_QUALIFIERS: tuple[str, ...] = ("STTS", "HiTS")


@dataclass(frozen=True, slots=True)
class FocusedQuery:
    """One auditable query containing one stage, concept, and optional qualifier."""

    category: ResourceCategory
    stage: LanguageStage
    language: QueryLanguage
    family: str
    stage_term: str
    concept: str
    qualifier: str | None = None
    trusted_evidence: Literal[False] = False

    @property
    def text(self) -> str:
        """Return the exact query text."""

        parts = (self.stage_term, self.concept, self.qualifier)
        return " ".join(part for part in parts if part is not None)


def iter_focused_queries(
    category: ResourceCategory,
    stage: LanguageStage | str,
    *,
    qualifiers: Iterable[str] = (),
    include_named_tagsets: bool = True,
) -> Iterator[FocusedQuery]:
    """Yield deterministic bilingual queries without combining concept families."""

    selected_stage = stage if isinstance(stage, LanguageStage) else LanguageStage(stage)
    extra_qualifiers = _clean_qualifiers(qualifiers)
    query_languages: tuple[QueryLanguage, ...] = ("de", "en")
    for family, languages in _CONCEPTS[category].items():
        for language in query_languages:
            stage_term = _STAGE_TERMS[selected_stage][language]
            for concept in languages[language]:
                yield FocusedQuery(
                    category=category,
                    stage=selected_stage,
                    language=language,
                    family=family,
                    stage_term=stage_term,
                    concept=concept,
                )
                for qualifier in extra_qualifiers:
                    yield FocusedQuery(
                        category=category,
                        stage=selected_stage,
                        language=language,
                        family=family,
                        stage_term=stage_term,
                        concept=concept,
                        qualifier=qualifier,
                    )
    if category == "tool" and include_named_tagsets:
        for qualifier in _TAGSET_QUALIFIERS:
            for language in query_languages:
                yield FocusedQuery(
                    category=category,
                    stage=selected_stage,
                    language=language,
                    family="tagging",
                    stage_term=_STAGE_TERMS[selected_stage][language],
                    concept=_CONCEPTS["tool"]["tagging"][language][1],
                    qualifier=qualifier,
                )


def generate_focused_queries(
    category: ResourceCategory,
    stage: LanguageStage | str,
    *,
    qualifiers: Iterable[str] = (),
    include_named_tagsets: bool = True,
) -> tuple[FocusedQuery, ...]:
    """Build a reusable immutable focused-query matrix."""

    return tuple(
        iter_focused_queries(
            category,
            stage,
            qualifiers=qualifiers,
            include_named_tagsets=include_named_tagsets,
        )
    )


def bounded_exclusion_groups(
    names: Iterable[str],
    *,
    max_names: int = 5,
    max_characters: int = 180,
) -> tuple[tuple[str, ...], ...]:
    """Partition untrusted known names into bounded negative-query groups."""

    if max_names < 1 or max_characters < 4:
        raise ValueError("exclusion bounds must be positive")
    cleaned = _unique_clean_terms(names)
    groups: list[tuple[str, ...]] = []
    current: list[str] = []
    current_length = 0
    for name in cleaned:
        encoded_length = len(name) + 3
        if encoded_length > max_characters:
            continue
        if current and (
            len(current) == max_names
            or current_length + 1 + encoded_length > max_characters
        ):
            groups.append(tuple(current))
            current = []
            current_length = 0
        current.append(name)
        current_length += encoded_length + (1 if current_length else 0)
    if current:
        groups.append(tuple(current))
    return tuple(groups)


def apply_exclusion_group(query: FocusedQuery, names: Sequence[str]) -> str:
    """Append one bounded exclusion group while preserving the focused query."""

    exclusions = " ".join(f'-"{name}"' for name in _unique_clean_terms(names))
    return f"{query.text} {exclusions}" if exclusions else query.text


def _clean_qualifiers(values: Iterable[str]) -> tuple[str, ...]:
    cleaned = _unique_clean_terms(values)
    if any(len(value) > 80 for value in cleaned):
        raise ValueError("query qualifiers must not exceed 80 characters")
    return cleaned


def _unique_clean_terms(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = " ".join(value.split()).replace('"', "")
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key not in seen:
            seen.add(key)
            result.append(cleaned)
    return tuple(result)


__all__ = [
    "FocusedQuery",
    "QueryLanguage",
    "ResourceCategory",
    "apply_exclusion_group",
    "bounded_exclusion_groups",
    "generate_focused_queries",
    "iter_focused_queries",
]
