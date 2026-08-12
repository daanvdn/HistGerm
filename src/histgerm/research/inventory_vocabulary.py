"""Transient, bounded vocabulary mining from trusted inventory metadata."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit

from histgerm.catalog import Catalog
from histgerm.models import Corpus, Dictionary, LanguageStage, Tool

from .models import ResourceCategory

type CatalogRecord = Corpus | Tool | Dictionary

_SPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[^\W_]+(?:[+#.-][^\W_]+)*", re.UNICODE)
_ACRONYM_RE = re.compile(r"\b[A-ZÄÖÜ][A-ZÄÖÜ0-9-]{1,11}\b")
_PAREN_ALIAS_RE = re.compile(
    r"\b([A-ZÄÖÜ][\wÄÖÜäöüß-]+(?:\s+[A-ZÄÖÜ][\wÄÖÜäöüß-]+){1,7})"
    r"\s*\(([A-ZÄÖÜ][A-ZÄÖÜ0-9-]{1,11})\)"
)
_FORMAT_RE = re.compile(
    r"\b(?:TEI(?:[- ]?P5)?|XML|JSON(?:-LD)?|CSV|TSV|RDF|"
    r"CoNLL(?:-U)?|TigerXML|PAULA|EXMARaLDA|ANNIS|plain text)\b",
    re.IGNORECASE,
)
_TAGSET_RE = re.compile(
    r"\b(?:HiTS|STTS|Universal Dependencies|UD|TIGER(?: tagset)?|"
    r"TEI(?:[- ]?P5)?|ISOcat|OLiA)\b",
    re.IGNORECASE,
)

_STAGE_TERMS: dict[LanguageStage, tuple[str, ...]] = {
    LanguageStage.OHG: ("old high german", "althochdeutsch", "ohg", "ahd"),
    LanguageStage.MHG: ("middle high german", "mittelhochdeutsch", "mhg", "mhd"),
    LanguageStage.ENHG: (
        "early new high german",
        "frühneuhochdeutsch",
        "enhg",
        "fnhd",
    ),
}
_CATEGORY_TERMS: dict[str, tuple[str, ...]] = {
    "corpus": (
        "corpus",
        "korpus",
        "text collection",
        "textsammlung",
        "dataset",
        "sprachdaten",
    ),
    "tool": (
        "tool",
        "werkzeug",
        "tagger",
        "lemmatizer",
        "lemmatisierer",
        "parser",
        "language model",
        "sprachmodell",
    ),
    "dictionary": (
        "dictionary",
        "wörterbuch",
        "woerterbuch",
        "lexicon",
        "lexikon",
        "wordbook",
        "wortschatz",
    ),
}
_TASK_PHRASES = (
    "part-of-speech tagging",
    "part of speech tagging",
    "pos tagging",
    "wortartenannotation",
    "morphological tagging",
    "morphologische annotation",
    "morphosyntactic analysis",
    "morphosyntaktische annotation",
    "lemmatization",
    "lemmatisation",
    "lemmatisierung",
    "lemma prediction",
    "grundformbestimmung",
    "normalization",
    "normalisierung",
    "spelling normalization",
    "schreibvariantennormalisierung",
    "dependency parsing",
    "dependenzparsing",
    "syntactic analysis",
    "syntaxanalyse",
    "tokenization",
    "tokenisierung",
    "sentence segmentation",
    "satzsegmentierung",
    "named entity recognition",
    "sprachmodell",
    "language model",
)
_RESOURCE_PHRASES = tuple(
    dict.fromkeys(term for terms in _CATEGORY_TERMS.values() for term in terms)
)
_GENERIC = frozenset(
    {
        "about",
        "account",
        "contact",
        "cookie",
        "documentation",
        "download",
        "english",
        "german",
        "help",
        "home",
        "imprint",
        "index",
        "login",
        "menu",
        "navigation",
        "news",
        "privacy",
        "project",
        "search",
        "skip to content",
        "start",
        "terms",
        "welcome",
    }
)
_PAYLOAD_PURPOSES = (
    "archive",
    "binary",
    "data",
    "download",
    "file",
    "installer",
    "model",
    "package",
    "weights",
)
_REPOSITORY_PURPOSES = ("github", "gitlab", "repository", "source_code", "repo")
_DOCUMENTATION_PURPOSES = (
    "documentation",
    "docs",
    "guide",
    "installation",
    "manual",
    "reference",
    "usage",
)
_HOMEPAGE_PURPOSES = ("homepage", "project", "website", "site")


class URLKind(StrEnum):
    """Approved inventory URL roles for vocabulary mining."""

    HOMEPAGE = "homepage"
    OFFICIAL_REPOSITORY = "official_repository"
    DOCUMENTATION = "documentation"
    METADATA = "metadata"


class VocabularyKind(StrEnum):
    """Kinds of discovery terms produced by the miner."""

    TASK = "task"
    RESOURCE_TYPE = "resource_type"
    ALIAS = "alias"
    TAGSET_STANDARD = "tagset_standard"
    FORMAT = "format"
    PROJECT = "project"
    RELATED_NAME = "related_name"


@dataclass(frozen=True, slots=True)
class InventoryURL:
    """One deduplicated, trusted metadata URL and its inventory provenance."""

    url: str
    kinds: tuple[URLKind, ...]
    resource_ids: tuple[str, ...]
    source_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FetchedDocument:
    """Structural response accepted from an injected approved transport."""

    url: str
    content_type: str
    body: bytes


class FetchedDocumentLike(Protocol):
    """Structural response returned by an approved bounded transport."""

    @property
    def url(self) -> str: ...

    @property
    def content_type(self) -> str: ...

    @property
    def body(self) -> bytes: ...


class BoundedTransport(Protocol):
    """Retrieve one approved URL while enforcing the supplied byte ceiling."""

    def __call__(self, url: str, *, max_bytes: int) -> FetchedDocumentLike: ...


@dataclass(frozen=True, slots=True)
class ClassifierCandidate:
    """A bounded, untrusted phrase offered to an optional classifier."""

    normalized: str
    wording: str
    suggested_kind: VocabularyKind
    source_url: str


class BoundedClassifier(Protocol):
    """Select only supplied normalized candidates, up to ``max_terms``."""

    def __call__(
        self,
        candidates: tuple[ClassifierCandidate, ...],
        *,
        max_terms: int,
    ) -> Sequence[str]: ...


@dataclass(frozen=True, slots=True)
class VocabularyTerm:
    """One normalized term retaining every bounded exact source wording."""

    normalized: str
    kind: VocabularyKind
    wordings: tuple[str, ...]
    source_urls: tuple[str, ...]
    source_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MiningGap:
    """A retrieval gap that makes no claim about resource availability."""

    url: str
    reason: str


@dataclass(frozen=True, slots=True)
class InventoryVocabulary:
    """In-memory result of one bounded inventory vocabulary run."""

    terms: tuple[VocabularyTerm, ...]
    urls: tuple[InventoryURL, ...]
    gaps: tuple[MiningGap, ...]
    fetched_pages: int
    fetched_bytes: int
    classifier_gap: str | None = None


@dataclass(frozen=True, slots=True)
class VocabularyLimits:
    """Hard limits for retrieval, extraction, classification, and output."""

    max_pages: int = 32
    max_page_bytes: int = 512 * 1024
    max_total_bytes: int = 8 * 1024 * 1024
    max_concurrency: int = 4
    max_visible_characters: int = 100_000
    max_candidates_per_page: int = 128
    max_classifier_candidates: int = 256
    max_classifier_terms: int = 64
    max_terms: int = 256
    max_wordings_per_term: int = 4
    max_sources_per_term: int = 8

    def __post_init__(self) -> None:
        values = (
            self.max_pages,
            self.max_page_bytes,
            self.max_total_bytes,
            self.max_concurrency,
            self.max_visible_characters,
            self.max_candidates_per_page,
            self.max_classifier_candidates,
            self.max_classifier_terms,
            self.max_terms,
            self.max_wordings_per_term,
            self.max_sources_per_term,
        )
        if any(not isinstance(value, int) or value < 1 for value in values):
            raise ValueError("all vocabulary limits must be positive integers")
        if self.max_concurrency > 16:
            raise ValueError("max_concurrency must not exceed 16")


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    """Offline-safe visible and metadata text extracted from one response."""

    title: str | None
    headings: tuple[str, ...]
    metadata: tuple[str, ...]
    visible_text: str


class _VisibleHTMLParser(HTMLParser):
    def __init__(self, max_characters: int) -> None:
        super().__init__(convert_charrefs=True)
        self.max_characters = max_characters
        self.hidden_depth = 0
        self.title_depth = 0
        self.heading_depth = 0
        self.title_parts: list[str] = []
        self.heading_parts: list[str] = []
        self.headings: list[str] = []
        self.metadata: list[str] = []
        self.visible: list[str] = []
        self.visible_characters = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "template", "svg", "canvas", "noscript"}:
            self.hidden_depth += 1
            return
        if self.hidden_depth:
            return
        if tag == "title":
            self.title_depth += 1
        if tag in {"h1", "h2", "h3"}:
            self.heading_depth += 1
            self.heading_parts = []
        if tag == "meta":
            values = {name.casefold(): value or "" for name, value in attrs}
            key = (values.get("name") or values.get("property") or "").casefold()
            if key in {
                "application-name",
                "citation_title",
                "description",
                "keywords",
                "og:description",
                "og:site_name",
                "og:title",
            }:
                self._append_metadata(values.get("content", ""))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "template", "svg", "canvas", "noscript"}:
            self.hidden_depth = max(0, self.hidden_depth - 1)
            return
        if self.hidden_depth:
            return
        if tag == "title":
            self.title_depth = max(0, self.title_depth - 1)
        if tag in {"h1", "h2", "h3"} and self.heading_depth:
            heading = _clean_wording(" ".join(self.heading_parts))
            if heading:
                self.headings.append(heading)
            self.heading_parts = []
            self.heading_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.hidden_depth:
            return
        text = _clean_wording(data)
        if not text:
            return
        if self.title_depth:
            self.title_parts.append(text)
        if self.heading_depth:
            self.heading_parts.append(text)
        remaining = self.max_characters - self.visible_characters
        if remaining > 0:
            accepted = text[:remaining]
            self.visible.append(accepted)
            self.visible_characters += len(accepted)

    def _append_metadata(self, value: str) -> None:
        cleaned = _clean_wording(value)
        if cleaned and sum(map(len, self.metadata)) < self.max_characters:
            self.metadata.append(cleaned)


@dataclass(slots=True)
class _TermAccumulator:
    wordings: list[str]
    source_urls: list[str]
    source_fields: list[str]


def normalize_term(value: str) -> str:
    """Normalize Unicode, case, punctuation, and whitespace for deduplication."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = "".join(
        character if character.isalnum() or character in {"+", "#"} else " "
        for character in normalized
    )
    return _SPACE_RE.sub(" ", normalized).strip()


def _clean_wording(value: str) -> str:
    return _SPACE_RE.sub(" ", value).strip(" \t\r\n|:;,.–—-")


def _canonical_url(value: str) -> str | None:
    parsed = urlsplit(value)
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    scheme = parsed.scheme.casefold()
    hostname = parsed.hostname.casefold()
    port = parsed.port
    if port is not None and port != (443 if scheme == "https" else 80):
        hostname = f"{hostname}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit(SplitResult(scheme, hostname, path, parsed.query, ""))


def _url_kind(purpose: str) -> URLKind | None:
    normalized = purpose.casefold()
    if any(word in normalized for word in _PAYLOAD_PURPOSES):
        return None
    if any(word in normalized for word in _REPOSITORY_PURPOSES):
        return URLKind.OFFICIAL_REPOSITORY
    if any(word in normalized for word in _DOCUMENTATION_PURPOSES):
        return URLKind.DOCUMENTATION
    if any(word in normalized for word in _HOMEPAGE_PURPOSES):
        return URLKind.HOMEPAGE
    return URLKind.METADATA


def _record_category(record: CatalogRecord) -> str:
    if isinstance(record, Corpus):
        return "corpus"
    if isinstance(record, Tool):
        return "tool"
    return "dictionary"


def _records(catalog: Catalog) -> Iterable[CatalogRecord]:
    yield from catalog.corpora
    yield from catalog.tools
    yield from catalog.dictionaries


def enumerate_inventory_urls(catalog: Catalog) -> tuple[InventoryURL, ...]:
    """Enumerate and deduplicate approved metadata URLs from every category."""

    collected: dict[str, tuple[set[URLKind], set[str], set[str]]] = {}

    def add(record_id: str, field: str, value: object, kind: URLKind) -> None:
        canonical = _canonical_url(str(value))
        if canonical is None:
            return
        kinds, resource_ids, fields = collected.setdefault(
            canonical, (set(), set(), set())
        )
        kinds.add(kind)
        resource_ids.add(record_id)
        fields.add(field)

    for record in _records(catalog):
        for purpose, url in (record.links or {}).items():
            kind = _url_kind(purpose)
            if kind is not None:
                add(record.id, f"links.{purpose}", url, kind)
        for source in record.sources:
            add(record.id, f"sources.{source.id}", source.url, URLKind.METADATA)
        if isinstance(record, Corpus):
            for version in record.versions:
                for purpose, url in (version.links or {}).items():
                    kind = _url_kind(purpose)
                    if kind is not None:
                        add(
                            record.id,
                            f"versions.{version.id}.links.{purpose}",
                            url,
                            kind,
                        )
        if isinstance(record, Tool):
            for index, url in enumerate(record.hugging_face_links or []):
                add(
                    record.id,
                    f"hugging_face_links.{index}",
                    url,
                    URLKind.OFFICIAL_REPOSITORY,
                )
        if isinstance(record, Dictionary):
            for index, url in enumerate(record.search_links or []):
                add(
                    record.id,
                    f"search_links.{index}",
                    url,
                    URLKind.METADATA,
                )

    return tuple(
        InventoryURL(
            url=url,
            kinds=tuple(sorted(kinds, key=str)),
            resource_ids=tuple(sorted(resource_ids)),
            source_fields=tuple(sorted(fields)),
        )
        for url, (kinds, resource_ids, fields) in collected.items()
    )


def extract_document(
    body: bytes,
    content_type: str,
    *,
    max_characters: int,
) -> ExtractedDocument:
    """Extract text from HTML or metadata without executing embedded content."""

    if max_characters < 1:
        raise ValueError("max_characters must be positive")
    media_type = content_type.partition(";")[0].strip().casefold()
    text = body.decode("utf-8", errors="replace")
    if media_type in {"text/html", "application/xhtml+xml"}:
        parser = _VisibleHTMLParser(max_characters)
        parser.feed(text)
        parser.close()
        title = _clean_wording(" ".join(parser.title_parts)) or None
        return ExtractedDocument(
            title=title,
            headings=tuple(parser.headings),
            metadata=tuple(parser.metadata),
            visible_text=_clean_wording(" ".join(parser.visible)),
        )
    if (
        media_type.startswith("text/")
        or media_type.endswith("+json")
        or media_type == "application/json"
    ):
        strings: list[str] = []
        if media_type.endswith("+json") or media_type == "application/json":
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = text
            _collect_json_strings(payload, strings, max_characters)
            text = " ".join(strings)
        return ExtractedDocument(
            title=None,
            headings=(),
            metadata=(),
            visible_text=_clean_wording(text[:max_characters]),
        )
    raise ValueError(f"unsupported vocabulary content type {media_type!r}")


def _collect_json_strings(value: object, output: list[str], limit: int) -> None:
    if sum(map(len, output)) >= limit:
        return
    if isinstance(value, str):
        output.append(value[: max(0, limit - sum(map(len, output)))])
    elif isinstance(value, list):
        for item in value:
            _collect_json_strings(item, output, limit)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_json_strings(item, output, limit)


def _contains_term(text: str, terms: Iterable[str]) -> bool:
    normalized = normalize_term(text)
    padded = f" {normalized} "
    return any(f" {normalize_term(term)} " in padded for term in terms)


def _phrase_matches(text: str, phrases: Iterable[str]) -> Iterable[str]:
    for phrase in phrases:
        pattern = re.compile(rf"\b{re.escape(phrase)}\b", re.IGNORECASE)
        yield from (match.group(0) for match in pattern.finditer(text))


def _page_candidates(
    document: ExtractedDocument,
    *,
    url: str,
    category: str,
    stages: frozenset[LanguageStage],
    limit: int,
) -> tuple[ClassifierCandidate, ...]:
    sections = tuple(
        value
        for value in (
            document.title,
            *document.headings,
            *document.metadata,
            document.visible_text,
        )
        if value
    )
    context = " ".join(sections)
    stage_relevant = any(
        _contains_term(context, _STAGE_TERMS[stage]) for stage in stages
    )
    category_relevant = _contains_term(context, _CATEGORY_TERMS[category])
    candidates: list[ClassifierCandidate] = []
    seen: set[tuple[VocabularyKind, str, str]] = set()

    def add(wording: str, kind: VocabularyKind, *, strong: bool = False) -> None:
        cleaned = _clean_wording(wording)
        normalized = normalize_term(cleaned)
        token_count = len(_WORD_RE.findall(cleaned))
        if (
            not normalized
            or normalized in _GENERIC
            or token_count == 0
            or token_count > 12
            or len(cleaned) > 120
            or (not strong and not (stage_relevant and category_relevant))
        ):
            return
        key = (kind, normalized, cleaned)
        if key not in seen and len(candidates) < limit:
            seen.add(key)
            candidates.append(ClassifierCandidate(normalized, cleaned, kind, url))

    for section in sections:
        for wording in _phrase_matches(section, _TASK_PHRASES):
            add(wording, VocabularyKind.TASK, strong=stage_relevant)
        for wording in _phrase_matches(section, _RESOURCE_PHRASES):
            if normalize_term(wording) in {
                normalize_term(term) for term in _CATEGORY_TERMS[category]
            }:
                add(wording, VocabularyKind.RESOURCE_TYPE, strong=stage_relevant)
        for match in _TAGSET_RE.finditer(section):
            add(match.group(0), VocabularyKind.TAGSET_STANDARD, strong=stage_relevant)
        for match in _FORMAT_RE.finditer(section):
            add(match.group(0), VocabularyKind.FORMAT, strong=stage_relevant)
        for match in _PAREN_ALIAS_RE.finditer(section):
            add(match.group(1), VocabularyKind.PROJECT)
            add(match.group(2), VocabularyKind.ALIAS)
        for match in _ACRONYM_RE.finditer(section):
            acronym = match.group(0)
            if acronym.casefold() not in {
                "html",
                "http",
                "https",
                "ohg",
                "mhg",
                "enhg",
            }:
                add(acronym, VocabularyKind.ALIAS)

    for optional_section in (document.title, *document.headings, *document.metadata):
        if optional_section is not None:
            add(optional_section, VocabularyKind.PROJECT)
    return tuple(candidates)


def _record_stages(record: CatalogRecord) -> frozenset[LanguageStage]:
    if isinstance(record, Corpus):
        return frozenset(record.covered_stages)
    if isinstance(record, Tool):
        return frozenset(record.supported_stages or [])
    return frozenset(record.covered_stages or [])


def _inventory_terms(
    catalog: Catalog,
    *,
    category: str,
    stages: frozenset[LanguageStage],
) -> Iterable[tuple[str, VocabularyKind, str]]:
    for record in _records(catalog):
        record_stages = _record_stages(record)
        if stages and record_stages and not stages.intersection(record_stages):
            continue
        record_category = _record_category(record)
        yield (
            record.name,
            (
                VocabularyKind.PROJECT
                if record_category == category
                else VocabularyKind.RELATED_NAME
            ),
            f"inventory:{record.id}:name",
        )
        for alias in record.aliases or []:
            yield alias, VocabularyKind.ALIAS, f"inventory:{record.id}:aliases"
        if isinstance(record, Tool):
            for task in record.tasks:
                yield (
                    task.value.replace("_", " "),
                    VocabularyKind.TASK,
                    f"inventory:{record.id}:tasks",
                )
            formats = [*(record.input_formats or []), *(record.output_formats or [])]
            for value in formats:
                yield value, VocabularyKind.FORMAT, f"inventory:{record.id}:formats"
        if isinstance(record, Corpus):
            for version in record.versions:
                for layer in version.annotations:
                    if layer.tagset_name:
                        yield (
                            layer.tagset_name,
                            VocabularyKind.TAGSET_STANDARD,
                            f"inventory:{record.id}:tagsets",
                        )


def mine_inventory_vocabulary(
    catalog: Catalog,
    *,
    category: ResourceCategory,
    stages: Iterable[LanguageStage],
    transport: BoundedTransport,
    classifier: BoundedClassifier | None = None,
    limits: VocabularyLimits | None = None,
) -> InventoryVocabulary:
    """Build a bounded transient vocabulary from all trusted catalog categories."""

    limits = limits or VocabularyLimits()
    if category not in _CATEGORY_TERMS:
        raise ValueError("category must be corpus, tool, or dictionary")
    wanted_stages = frozenset(stages)
    if not wanted_stages:
        raise ValueError("at least one target stage is required")
    urls = enumerate_inventory_urls(catalog)
    selected = urls[: min(limits.max_pages, limits.max_total_bytes)]
    page_budget = min(
        limits.max_page_bytes,
        max(1, limits.max_total_bytes // max(1, len(selected))),
    )

    def fetch(
        entry: InventoryURL,
    ) -> tuple[InventoryURL, FetchedDocumentLike | Exception]:
        try:
            return entry, transport(entry.url, max_bytes=page_budget)
        except Exception as error:
            return entry, error

    if selected:
        with ThreadPoolExecutor(
            max_workers=min(limits.max_concurrency, len(selected))
        ) as executor:
            fetched = tuple(executor.map(fetch, selected))
    else:
        fetched = ()

    accumulators: dict[tuple[VocabularyKind, str], _TermAccumulator] = {}

    def retain(
        wording: str,
        kind: VocabularyKind,
        source_url: str,
        source_field: str,
    ) -> None:
        cleaned = _clean_wording(wording)
        normalized = normalize_term(cleaned)
        if not normalized or normalized in _GENERIC:
            return
        accumulator = accumulators.setdefault(
            (kind, normalized), _TermAccumulator([], [], [])
        )
        if (
            cleaned not in accumulator.wordings
            and len(accumulator.wordings) < limits.max_wordings_per_term
        ):
            accumulator.wordings.append(cleaned)
        if (
            source_url not in accumulator.source_urls
            and len(accumulator.source_urls) < limits.max_sources_per_term
        ):
            accumulator.source_urls.append(source_url)
        if source_field not in accumulator.source_fields:
            accumulator.source_fields.append(source_field)

    for wording, kind, source in _inventory_terms(
        catalog, category=category, stages=wanted_stages
    ):
        retain(wording, kind, source, source)

    gaps: list[MiningGap] = []
    classifier_candidates: list[ClassifierCandidate] = []
    fetched_pages = 0
    fetched_bytes = 0
    deterministic: set[tuple[VocabularyKind, str]] = set()
    candidate_fields: dict[tuple[VocabularyKind, str, str], tuple[str, ...]] = {}
    for entry, response in fetched:
        if isinstance(response, Exception):
            gaps.append(MiningGap(entry.url, str(response) or type(response).__name__))
            continue
        if len(response.body) > page_budget:
            gaps.append(
                MiningGap(entry.url, f"transport exceeded {page_budget} byte limit")
            )
            continue
        try:
            document = extract_document(
                response.body,
                response.content_type,
                max_characters=limits.max_visible_characters,
            )
        except ValueError as error:
            gaps.append(MiningGap(entry.url, str(error)))
            continue
        fetched_pages += 1
        fetched_bytes += len(response.body)
        candidates = _page_candidates(
            document,
            url=entry.url,
            category=category,
            stages=wanted_stages,
            limit=limits.max_candidates_per_page,
        )
        for candidate in candidates:
            key = (candidate.suggested_kind, candidate.normalized)
            candidate_fields[
                (candidate.suggested_kind, candidate.normalized, candidate.source_url)
            ] = entry.source_fields
            if candidate.suggested_kind in {
                VocabularyKind.TASK,
                VocabularyKind.RESOURCE_TYPE,
                VocabularyKind.TAGSET_STANDARD,
                VocabularyKind.FORMAT,
            }:
                deterministic.add(key)
            if len(classifier_candidates) < limits.max_classifier_candidates:
                classifier_candidates.append(candidate)

    accepted = (
        set(deterministic)
        if classifier is not None
        else {
            (candidate.suggested_kind, candidate.normalized)
            for candidate in classifier_candidates
        }
    )
    classifier_gap: str | None = None
    if classifier is not None and classifier_candidates:
        offered = tuple(classifier_candidates[: limits.max_classifier_candidates])
        allowed = {candidate.normalized for candidate in offered}
        try:
            selected_terms = tuple(
                classifier(offered, max_terms=limits.max_classifier_terms)
            )
            for normalized in selected_terms[: limits.max_classifier_terms]:
                normalized = normalize_term(normalized)
                if normalized in allowed:
                    accepted.update(
                        (candidate.suggested_kind, candidate.normalized)
                        for candidate in offered
                        if candidate.normalized == normalized
                    )
        except Exception as error:
            classifier_gap = str(error) or type(error).__name__

    for candidate in classifier_candidates:
        key = (candidate.suggested_kind, candidate.normalized)
        if key not in accepted:
            continue
        fields = candidate_fields.get(
            (candidate.suggested_kind, candidate.normalized, candidate.source_url),
            (),
        )
        retain(
            candidate.wording,
            candidate.suggested_kind,
            candidate.source_url,
            fields[0] if fields else "page",
        )

    terms = tuple(
        VocabularyTerm(
            normalized=normalized,
            kind=kind,
            wordings=tuple(accumulator.wordings),
            source_urls=tuple(accumulator.source_urls),
            source_fields=tuple(accumulator.source_fields),
        )
        for (kind, normalized), accumulator in list(accumulators.items())[
            : limits.max_terms
        ]
    )
    return InventoryVocabulary(
        terms=terms,
        urls=urls,
        gaps=tuple(gaps),
        fetched_pages=fetched_pages,
        fetched_bytes=fetched_bytes,
        classifier_gap=classifier_gap,
    )


__all__ = [
    "BoundedClassifier",
    "BoundedTransport",
    "ClassifierCandidate",
    "ExtractedDocument",
    "FetchedDocument",
    "FetchedDocumentLike",
    "InventoryURL",
    "InventoryVocabulary",
    "MiningGap",
    "URLKind",
    "VocabularyKind",
    "VocabularyLimits",
    "VocabularyTerm",
    "enumerate_inventory_urls",
    "extract_document",
    "mine_inventory_vocabulary",
    "normalize_term",
]
