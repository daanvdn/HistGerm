"""Focused, bilingual query construction for research discovery."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Literal

from histgerm.models import LanguageStage

from .query_intents import classify_intent

type ResourceCategory = Literal["corpus", "tool", "dictionary"]
type QueryLanguage = Literal["de", "en"]
type QueryFormulation = Literal[
    "plain",
    "exact_stage",
    "exact_stage_and_concept",
    "stage_abbreviation",
    "stage_iso_639_3",
]

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
            "de": ("Tokenizer", "Tokenisierung", "Satzsegmentierung"),
            "en": ("tokenizer", "tokenization", "sentence segmentation"),
        },
        "named_entity_recognition": {
            "de": ("Named-Entity-Erkennung", "Entitätenerkennung"),
            "en": ("named-entity recognition", "NER"),
        },
        "machine_translation": {
            "de": ("maschinelle Übersetzung", "NMT"),
            "en": ("machine translation", "NMT"),
        },
        "coreference": {
            "de": ("Koreferenzauflösung", "Koreferenzerkennung"),
            "en": ("coreference resolution", "coreference detection"),
        },
        "semantic_role_labeling": {
            "de": ("semantische Rollenannotation", "SRL"),
            "en": ("semantic role labeling", "SRL"),
        },
        "relation_extraction": {
            "de": ("Relationsextraktion", "Beziehungsextraktion"),
            "en": ("relation extraction", "relationship extraction"),
        },
        "sentence_embeddings": {
            "de": ("Satz-Embeddings", "Satzeinbettungen"),
            "en": ("sentence embeddings", "sentence embedding"),
        },
        "sentiment_analysis": {
            "de": ("Sentimentanalyse", "Stimmungsanalyse"),
            "en": ("sentiment analysis", "opinion mining"),
        },
        "compound_splitting": {
            "de": ("Kompositazerlegung", "Kompositum-Splitting"),
            "en": ("compound splitting", "compound decomposition"),
        },
        "finite_state_morphology": {
            "de": ("Finite-State-Morphologie", "Morphologie mit endlichen Automaten"),
            "en": ("finite-state morphology", "finite-state morphological analysis"),
        },
        "models": {
            "de": (
                "BERT",
                "BERT-Modell",
                "BERT-Architektur",
                "BERT-Modellfamilie",
                "Sprachmodell",
                "vortrainiertes Sprachmodell",
                "maskiertes Sprachmodell",
                "Transformer-Modell",
                "Transformer-Architektur",
                "Wort-Embedding",
                "Wort-Embeddings",
                "Worteinbettung",
                "Worteinbettungen",
                "kontextuelle String-Embeddings",
                "LSTM",
                "RNN",
                "sprachübergreifender Transfer",
            ),
            "en": (
                "BERT",
                "BERT model",
                "BERT architecture",
                "BERT family",
                "language model",
                "pretrained language model",
                "masked language model",
                "transformer model",
                "transformer architecture",
                "word embedding",
                "word embeddings",
                "contextual string embeddings",
                "LSTM",
                "RNN",
                "cross-lingual transfer",
            ),
        },
        "pipelines": {
            "de": (
                "NLP-Werkzeug",
                "Annotationswerkzeug",
                "Sprachverarbeitung",
                "generative Sprachmodell-Pipeline",
            ),
            "en": (
                "NLP tool",
                "annotation tool",
                "language-processing pipeline",
                "generative language-model pipeline",
            ),
        },
        "constituency_parsing": {
            "de": ("Konstituentenparser",),
            "en": ("constituency parser",),
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
_STAGE_ABBREVIATIONS: dict[LanguageStage, str] = {
    LanguageStage.OHG: "OHG",
    LanguageStage.MHG: "MHG",
    LanguageStage.ENHG: "ENHG",
}
_STAGE_ISO_639_3: dict[LanguageStage, str] = {
    LanguageStage.MHG: "gmh",
}
_DOUBLE_QUOTES = str.maketrans({character: " " for character in '"“”„‟'})
_SAFE_LEAD_TERM = re.compile(r"^@?[^\W_][\w ._-]*$", re.UNICODE)
_QUERY_OPERATORS = frozenset({"and", "not", "or"})


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

        return render_query(self)

    @property
    def intent_id(self) -> str:
        """Return the canonical structured query intent for this query."""

        return classify_intent(
            self.category,
            self.stage.value,
            self.language,
            self.concept,
            self.family,
        )


def render_query(
    query: FocusedQuery,
    formulation: QueryFormulation = "exact_stage",
) -> str:
    """Render one stage, one concept, and at most one separate qualifier."""

    stage = _clean_term(query.stage_term)
    concept = _clean_term(query.concept)
    qualifier = _clean_term(query.qualifier) if query.qualifier is not None else None
    if not stage or not concept:
        raise ValueError("query stage and concept must not be empty")
    if formulation == "plain":
        rendered_stage = stage
        rendered_concept = concept
        rendered_qualifier = qualifier
    elif formulation == "exact_stage":
        rendered_stage = _quote_phrase(stage)
        rendered_concept = concept
        rendered_qualifier = _quote_phrase(qualifier) if qualifier else None
    elif formulation == "exact_stage_and_concept":
        rendered_stage = _quote_phrase(stage)
        rendered_concept = _quote_phrase(concept)
        rendered_qualifier = _quote_phrase(qualifier) if qualifier else None
    elif formulation == "stage_abbreviation":
        rendered_stage = _STAGE_ABBREVIATIONS[query.stage]
        rendered_concept = concept
        rendered_qualifier = _quote_phrase(qualifier) if qualifier else None
    elif formulation == "stage_iso_639_3":
        try:
            rendered_stage = _STAGE_ISO_639_3[query.stage]
        except KeyError as error:
            raise ValueError(
                f"no controlled ISO 639-3 recall form for {query.stage.value!r}"
            ) from error
        rendered_concept = concept
        rendered_qualifier = _quote_phrase(qualifier) if qualifier else None
    else:
        raise ValueError(f"unknown query formulation {formulation!r}")
    return " ".join(
        part
        for part in (rendered_stage, rendered_concept, rendered_qualifier)
        if part is not None
    )


def controlled_recall_formulations(query: FocusedQuery) -> tuple[QueryFormulation, ...]:
    """Return bounded stage formulations from precision to controlled recall."""

    formulations: list[QueryFormulation] = ["exact_stage"]
    if len(query.concept.split()) > 1:
        formulations.append("exact_stage_and_concept")
    formulations.append("stage_abbreviation")
    if query.stage in _STAGE_ISO_639_3:
        formulations.append("stage_iso_639_3")
    return tuple(formulations)


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

    queries: list[FocusedQuery] = []
    rendered: set[str] = set()
    for query in iter_focused_queries(
        category,
        stage,
        qualifiers=qualifiers,
        include_named_tagsets=include_named_tagsets,
    ):
        key = render_query(query).casefold()
        if key not in rendered:
            rendered.add(key)
            queries.append(query)
    return tuple(queries)


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


def apply_exclusion_group(
    query: FocusedQuery,
    names: Sequence[str],
    *,
    formulation: QueryFormulation = "exact_stage",
) -> str:
    """Append one bounded exclusion group while preserving the focused query."""

    exclusions = " ".join(f'-"{name}"' for name in _unique_clean_terms(names))
    base = render_query(query, formulation)
    return f"{base} {exclusions}" if exclusions else base


def normalize_metadata_lead_terms(
    values: Iterable[str],
    *,
    max_terms: int = 8,
    max_words: int = 4,
    max_characters: int = 48,
) -> tuple[str, ...]:
    """Normalize untrusted metadata into bounded, query-safe lead terms."""

    if max_terms < 0 or max_words < 1 or max_characters < 1:
        raise ValueError("metadata lead bounds must be non-negative")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_term(value)
        if (
            not cleaned
            or len(cleaned) > max_characters
            or len(cleaned.split()) > max_words
            or not _SAFE_LEAD_TERM.fullmatch(cleaned)
            or not _QUERY_OPERATORS.isdisjoint(
                word.casefold() for word in cleaned.split()
            )
        ):
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
        if len(result) == max_terms:
            break
    return tuple(result)


def _clean_qualifiers(values: Iterable[str]) -> tuple[str, ...]:
    cleaned = _unique_clean_terms(values)
    if any(len(value) > 80 for value in cleaned):
        raise ValueError("query qualifiers must not exceed 80 characters")
    return cleaned


def _unique_clean_terms(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_term(value)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key not in seen:
            seen.add(key)
            result.append(cleaned)
    return tuple(result)


def _clean_term(value: str) -> str:
    return " ".join(value.translate(_DOUBLE_QUOTES).split())


def _quote_phrase(value: str) -> str:
    return f'"{value}"' if len(value.split()) > 1 else value


__all__ = [
    "FocusedQuery",
    "QueryFormulation",
    "QueryLanguage",
    "ResourceCategory",
    "apply_exclusion_group",
    "bounded_exclusion_groups",
    "controlled_recall_formulations",
    "generate_focused_queries",
    "iter_focused_queries",
    "normalize_metadata_lead_terms",
    "render_query",
]
