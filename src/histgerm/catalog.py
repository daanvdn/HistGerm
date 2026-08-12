"""Minimal loading and query facade for HistGerm V2 resources."""

from __future__ import annotations

from collections.abc import Iterable
from itertools import product
from typing import Literal

from pydantic import ValidationError

from .loading import (
    InventoryCompositionError,
    InventoryModelError,
    LoadingDiagnostic,
    discover_bundled_yaml,
    load_bundled_yaml,
)
from .models import (
    AnnotationType,
    Corpus,
    CorpusText,
    CorpusVersion,
    Dictionary,
    LanguageStage,
    LegalPermission,
    Task,
    Tool,
)

type CoverageDimension = Literal["stage", "dialect", "annotation_type", "tagset"]
type CatalogRecord = Corpus | Tool | Dictionary
type LegalRecord = CorpusText | CatalogRecord

_LEGAL_FIELDS = (
    "model_training",
    "original_data_redistribution",
    "processed_data_redistribution",
    "trained_weight_publication",
)
_COVERAGE_DIMENSIONS = frozenset({"stage", "dialect", "annotation_type", "tagset"})


def _normalize(value: str) -> str:
    """Normalize a non-ID string for exact matching and grouping."""

    return " ".join(value.split()).casefold()


def _enum_value[T: (LanguageStage, AnnotationType, Task)](
    value: T | str, enum_type: type[T]
) -> T:
    """Validate an enum filter using its exact authored value."""

    return value if isinstance(value, enum_type) else enum_type(value)


def _model_error(path: str, error: ValidationError) -> InventoryModelError:
    """Convert one Pydantic failure into a stable loading diagnostic."""

    first = error.errors(include_url=False)[0]
    return InventoryModelError(
        LoadingDiagnostic(
            code="model_validation",
            path=path,
            location="/".join(str(part) for part in first["loc"]),
            message=str(first["msg"]),
        )
    )


class Catalog:
    """An ordered in-memory collection with the approved V2 query helpers."""

    def __init__(
        self,
        *,
        corpora: Iterable[Corpus] = (),
        tools: Iterable[Tool] = (),
        dictionaries: Iterable[Dictionary] = (),
    ) -> None:
        """Create a catalog while preserving the supplied record order."""

        self.corpora = list(corpora)
        self.tools = list(tools)
        self.dictionaries = list(dictionaries)
        corpus_ids = {corpus.id for corpus in self.corpora}
        qualified_text_ids = {
            f"{corpus.id}:{text.id}"
            for corpus in self.corpora
            for version in corpus.versions
            for text in version.texts
        }
        for corpus in self.corpora:
            corpus.validate_inventory_references(corpus_ids, qualified_text_ids)

    def _owned_texts(
        self,
    ) -> Iterable[tuple[Corpus, CorpusVersion, CorpusText]]:
        """Yield each text with its owner and version in stable load order."""

        for corpus in self.corpora:
            for version in corpus.versions:
                for text in version.texts:
                    yield corpus, version, text

    def _text_owner(self, requested: CorpusText) -> tuple[Corpus, CorpusVersion]:
        """Resolve a catalog text object to its owning corpus and version."""

        equal_matches: list[tuple[Corpus, CorpusVersion]] = []
        for corpus, version, text in self._owned_texts():
            if text is requested:
                return corpus, version
            if text == requested:
                equal_matches.append((corpus, version))
        if len(equal_matches) == 1:
            return equal_matches[0]
        raise ValueError("text does not resolve uniquely within this catalog")

    def find_corpora(self, *, stage: LanguageStage | str | None = None) -> list[Corpus]:
        """Find corpora whose evidenced coverage includes the requested stage."""

        wanted_stage = None if stage is None else _enum_value(stage, LanguageStage)
        if wanted_stage is None:
            return list(self.corpora)
        return [
            corpus for corpus in self.corpora if wanted_stage in corpus.covered_stages
        ]

    def find_texts(
        self,
        *,
        corpus_id: str | None = None,
        text_id: str | None = None,
        stage: LanguageStage | str | None = None,
        dialect: str | None = None,
        annotation_type: AnnotationType | str | None = None,
        tagset: str | None = None,
        date_contains: str | None = None,
        has_overlap: bool | None = None,
    ) -> list[CorpusText]:
        """Find inline texts using corpus-qualified, AND-combined filters."""

        if text_id is not None and corpus_id is None:
            raise ValueError("text_id requires corpus_id")
        if text_id is not None and ":" in text_id:
            raise ValueError("text_id must be a bare corpus-local ID")
        wanted_stage = None if stage is None else _enum_value(stage, LanguageStage)
        wanted_type = (
            None
            if annotation_type is None
            else _enum_value(annotation_type, AnnotationType)
        )
        wanted_dialect = None if dialect is None else _normalize(dialect)
        wanted_tagset = None if tagset is None else _normalize(tagset)
        wanted_date = None if date_contains is None else date_contains.casefold()
        matches: list[CorpusText] = []
        for corpus, version, text in self._owned_texts():
            if corpus_id is not None and corpus.id != corpus_id:
                continue
            if text_id is not None and text.id != text_id:
                continue
            if wanted_stage is not None and wanted_stage not in text.stages:
                continue
            if (
                wanted_dialect is not None
                and _normalize(text.dialect) != wanted_dialect
            ):
                continue
            referenced_layers = [
                layer
                for layer in version.annotations
                if layer.id in text.annotation_ids
            ]
            if wanted_type is not None and not any(
                layer.type is wanted_type for layer in referenced_layers
            ):
                continue
            if wanted_tagset is not None and not any(
                layer.tagset_name is not None
                and _normalize(layer.tagset_name) == wanted_tagset
                for layer in referenced_layers
            ):
                continue
            if wanted_date is not None and wanted_date not in text.date.casefold():
                continue
            if has_overlap is not None and bool(text.overlaps) is not has_overlap:
                continue
            matches.append(text)
        return matches

    def find_tools(
        self,
        *,
        task: Task | str | None = None,
        stage: LanguageStage | str | None = None,
        output_format: str | None = None,
    ) -> list[Tool]:
        """Find tools using task, stage, and normalized format membership."""

        wanted_task = None if task is None else _enum_value(task, Task)
        wanted_stage = None if stage is None else _enum_value(stage, LanguageStage)
        wanted_format = None if output_format is None else _normalize(output_format)
        return [
            tool
            for tool in self.tools
            if (wanted_task is None or wanted_task in tool.tasks)
            and (wanted_stage is None or wanted_stage in (tool.supported_stages or []))
            and (
                wanted_format is None
                or any(
                    _normalize(value) == wanted_format
                    for value in tool.output_formats or []
                )
            )
        ]

    def find_dictionaries(
        self,
        *,
        stage: LanguageStage | str | None = None,
        lexical_feature: str | None = None,
        machine_readable: bool | None = None,
    ) -> list[Dictionary]:
        """Find dictionaries using stage, feature, and Boolean filters."""

        wanted_stage = None if stage is None else _enum_value(stage, LanguageStage)
        wanted_feature = (
            None if lexical_feature is None else _normalize(lexical_feature)
        )
        return [
            dictionary
            for dictionary in self.dictionaries
            if (
                wanted_stage is None
                or wanted_stage in (dictionary.covered_stages or [])
            )
            and (
                wanted_feature is None
                or any(
                    _normalize(value) == wanted_feature
                    for value in dictionary.lexical_features or []
                )
            )
            and (
                machine_readable is None
                or dictionary.machine_readable is machine_readable
            )
        ]

    def legal_warnings(self, records: Iterable[LegalRecord]) -> list[dict[str, object]]:
        """Return prohibited and unclear permission rows without overlap data."""

        warnings: list[dict[str, object]] = []
        for record in records:
            resource: Corpus | Tool | Dictionary
            if isinstance(record, CorpusText):
                resource, _ = self._text_owner(record)
                text_id: str | None = f"{resource.id}:{record.id}"
            else:
                resource = record
                text_id = None
            for field in _LEGAL_FIELDS:
                value = getattr(resource.access, field)
                if value is LegalPermission.PERMITTED:
                    continue
                warnings.append(
                    {
                        "resource_id": resource.id,
                        "text_id": text_id,
                        "field": field,
                        "value": value.value,
                        "source_ids": list(resource.access.source_ids or []),
                        "note": resource.access.note,
                    }
                )
        return warnings

    def overlap_warnings(
        self, records: Iterable[CorpusText | Corpus]
    ) -> list[dict[str, object]]:
        """Return authored corpus or text overlap rows in input order."""

        warnings: list[dict[str, object]] = []
        for record in records:
            if isinstance(record, CorpusText):
                corpus, _ = self._text_owner(record)
                owner_id = f"{corpus.id}:{record.id}"
            else:
                owner_id = record.id
            for overlap in record.overlaps or []:
                warnings.append(
                    {
                        "owner_id": owner_id,
                        "relationship": overlap.relationship.value,
                        "with": overlap.with_,
                        "note": overlap.note,
                        "source_ids": list(overlap.source_ids or []),
                    }
                )
        return warnings

    def _coverage_values(
        self, text: CorpusText, dimension: CoverageDimension
    ) -> list[str]:
        """Return authored values for one text coverage dimension."""

        if dimension == "stage":
            return [stage.value for stage in text.stages]
        if dimension == "dialect":
            return [text.dialect]
        _, version = self._text_owner(text)
        layers = [
            layer for layer in version.annotations if layer.id in text.annotation_ids
        ]
        if dimension == "annotation_type":
            return [layer.type.value for layer in layers]
        return [layer.tagset_name for layer in layers if layer.tagset_name is not None]

    def coverage_summary(
        self,
        texts: Iterable[CorpusText],
        *,
        by: list[CoverageDimension],
    ) -> list[dict[str, object]]:
        """Group texts into stable plain rows across selected dimensions."""

        if not by:
            raise ValueError("by must contain at least one coverage dimension")
        if len(by) != len(set(by)):
            raise ValueError("by must not contain duplicate dimensions")
        invalid = set(by) - _COVERAGE_DIMENSIONS
        if invalid:
            raise ValueError(f"unsupported coverage dimensions: {sorted(invalid)!r}")

        groups: dict[tuple[str, ...], tuple[dict[str, str], set[str]]] = {}
        for text in texts:
            corpus, _ = self._text_owner(text)
            qualified_id = f"{corpus.id}:{text.id}"
            value_lists = [self._coverage_values(text, dimension) for dimension in by]
            for values in product(*value_lists):
                key = tuple(_normalize(value) for value in values)
                if key not in groups:
                    groups[key] = (dict(zip(by, values, strict=True)), set())
                groups[key][1].add(qualified_id)

        rows: list[dict[str, object]] = []
        for dimensions, text_ids in groups.values():
            sorted_ids = sorted(text_ids)
            rows.append(
                {
                    **dimensions,
                    "text_count": len(sorted_ids),
                    "text_ids": sorted_ids,
                }
            )
        return rows


def load_catalog(*, package: str = "histgerm", directory: str = "data") -> Catalog:
    """Load all bundled V2 resources into a stable ordered catalog."""

    corpora: list[Corpus] = []
    tools: list[Tool] = []
    dictionaries: list[Dictionary] = []
    for path in discover_bundled_yaml(package=package, directory=directory):
        category, separator, _ = path.partition("/")
        if not separator or category not in {
            "corpora",
            "tools",
            "dictionaries",
        }:
            raise InventoryCompositionError(
                LoadingDiagnostic(
                    code="unknown_resource_category",
                    path=path,
                    location="",
                    message="resource must be under corpora, tools, or dictionaries",
                )
            )
        try:
            payload = load_bundled_yaml(path, package=package, directory=directory)
            if category == "corpora":
                corpora.append(Corpus.model_validate(payload))
            elif category == "tools":
                tools.append(Tool.model_validate(payload))
            else:
                dictionaries.append(Dictionary.model_validate(payload))
        except ValidationError as error:
            raise _model_error(path, error) from error
    return Catalog(corpora=corpora, tools=tools, dictionaries=dictionaries)


__all__ = ["Catalog", "load_catalog"]
