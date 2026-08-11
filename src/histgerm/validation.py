"""Deterministic catalog-wide validation and command-line reporting."""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from histgerm.models.catalog import Catalog
from histgerm.models.common import (
    HistGermModel,
    KnownValue,
    SelectionScope,
)
from histgerm.models.corpus import Document
from histgerm.models.provenance import (
    ProvenancedRecord,
    provenance_completeness_report,
)

_PROTOCOL = "histgerm.command.v1"
_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "path",
        "local_path",
        "payload",
        "content",
        "resource_payload",
        "model_weights",
        "dataset_content",
        "dictionary_content",
    }
)
_PREFIXES = {
    "resource": "res-",
    "version": "ver-",
    "distribution": "dist-",
    "component": "comp-",
    "document": "doc-",
    "annotation": "ann-",
    "work": "work-",
    "witness": "wit-",
    "publication": "pub-",
    "relationship": "rel-",
    "evidence": "evidence-",
}
_REQUIRED_VOCABULARIES = frozenset(
    {
        "access_requirement",
        "alignment_unit",
        "annotation_production_method",
        "annotation_quality",
        "annotation_task",
        "auxiliary_task",
        "availability",
        "certainty",
        "data_format",
        "dating_method",
        "distribution_kind",
        "editorial_intervention",
        "entity_reference_kind",
        "evidence_source_kind",
        "knowledge_state",
        "language_stage",
        "license_scope",
        "license_status",
        "lifecycle_status",
        "maintenance_status",
        "measurement_origin",
        "name_type",
        "overlap_extent",
        "party_type",
        "permission_state",
        "relationship_kind",
        "resource_category",
        "responsible_party_role",
        "size_unit",
        "suitability_decision",
        "text_layer_kind",
        "transcription_method",
        "unicode_normalization",
        "url_purpose",
        "witness_kind",
    }
)
_REQUIRED_OPEN_REGISTRIES = frozenset(
    {"dialects", "genres", "languages", "licenses", "regions", "text_types"}
)


class CatalogDiagnostic(HistGermModel):
    """One stable path-rich catalog validation diagnostic."""

    code: str
    severity: Literal["error", "warning", "unresolved"] = "error"
    path: str
    model_path: str
    pointer: str
    message: str


class CatalogValidationReport(HistGermModel):
    """Complete deterministic catalog validation result."""

    errors: tuple[CatalogDiagnostic, ...]
    warnings: tuple[CatalogDiagnostic, ...] = ()
    unresolved: tuple[CatalogDiagnostic, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not self.errors


class _Entity:
    def __init__(
        self, kind: str, identifier: str, path: str, source: str, value: object
    ) -> None:
        self.kind = kind
        self.identifier = identifier
        self.path = path
        self.source = source
        self.value = value


def _pointer(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"


def _source_for(identifier: str, default: str, source_paths: Mapping[str, str]) -> str:
    return source_paths.get(identifier, default)


def _diagnostic(code: str, path: str, source: str, message: str) -> CatalogDiagnostic:
    return CatalogDiagnostic(
        code=code,
        path=source,
        model_path=path,
        pointer=_pointer(path),
        message=message,
    )


def _known_values(value: object) -> Iterable[object]:
    if isinstance(value, KnownValue):
        inner = value.value
        if isinstance(inner, (set, frozenset, list, tuple)):
            yield from inner
        else:
            yield inner


def _walk(value: object, path: str) -> Iterable[tuple[str, object]]:
    yield path, value
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from _walk(getattr(value, name), f"{path}/{name}")
    elif isinstance(value, Mapping):
        for key in sorted(value, key=str):
            yield from _walk(value[key], f"{path}/{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}/{index}")


def _catalog_entities(
    catalog: Catalog, source_paths: Mapping[str, str]
) -> tuple[list[_Entity], dict[str, str], dict[str, str]]:
    entities: list[_Entity] = []
    version_owner: dict[str, str] = {}
    child_owner: dict[str, str] = {}

    def add(kind: str, item: Any, path: str, default_source: str) -> None:
        identifier = str(item.id)
        entities.append(
            _Entity(
                kind,
                identifier,
                path,
                _source_for(identifier, default_source, source_paths),
                item,
            )
        )

    for index, resource in enumerate(catalog.resources):
        resource_path = f"/resources/{index}"
        resource_source = _source_for(resource.id, "<catalog>", source_paths)
        add("resource", resource, resource_path, resource_source)
        for version_index, version in enumerate(resource.versions):
            version_path = f"{resource_path}/versions/{version_index}"
            add("version", version, version_path, resource_source)
            version_owner[version.id] = resource.id
            for field, kind in (
                ("distributions", "distribution"),
                ("components", "component"),
                ("documents", "document"),
                ("annotations", "annotation"),
            ):
                for child_index, child in enumerate(getattr(version, field)):
                    add(
                        kind,
                        child,
                        f"{version_path}/{field}/{child_index}",
                        resource_source,
                    )
                    child_owner[child.id] = version.id
    for field, kind in (
        ("works", "work"),
        ("witnesses", "witness"),
        ("publications", "publication"),
        ("relationships", "relationship"),
    ):
        for index, item in enumerate(getattr(catalog, field)):
            add(kind, item, f"/{field}/{index}", "<catalog>")
    for entity in list(entities):
        if isinstance(entity.value, ProvenancedRecord):
            for index, evidence in enumerate(entity.value.evidence):
                add(
                    "evidence",
                    evidence,
                    f"{entity.path}/evidence/{index}",
                    entity.source,
                )
    for registry_name, registry in catalog.registries.root.items():
        for index, term in enumerate(registry.terms):
            add(
                "registry_term",
                term,
                f"/registries/{registry_name}/terms/{index}",
                "<catalog>",
            )
            for evidence_index, evidence in enumerate(term.evidence):
                add(
                    "evidence",
                    evidence,
                    f"/registries/{registry_name}/terms/{index}/evidence/"
                    f"{evidence_index}",
                    "<catalog>",
                )
    return entities, version_owner, child_owner


def _validate_ids(
    entities: list[_Entity], errors: list[CatalogDiagnostic]
) -> dict[str, _Entity]:
    by_id: dict[str, _Entity] = {}
    for entity in entities:
        previous = by_id.get(entity.identifier)
        if previous is not None:
            errors.append(
                _diagnostic(
                    "duplicate_id",
                    f"{entity.path}/id",
                    entity.source,
                    f"ID {entity.identifier!r} duplicates {previous.path} "
                    f"from {previous.source}.",
                )
            )
        else:
            by_id[entity.identifier] = entity
        prefix = _PREFIXES.get(entity.kind)
        if prefix is not None and not entity.identifier.startswith(prefix):
            errors.append(
                _diagnostic(
                    "invalid_id_prefix",
                    f"{entity.path}/id",
                    entity.source,
                    f"{entity.kind} IDs must use prefix {prefix!r}.",
                )
            )
    return by_id


def _normalized_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def _validate_names(catalog: Catalog, errors: list[CatalogDiagnostic]) -> None:
    for index, resource in enumerate(catalog.resources):
        source = "<catalog>"
        names: dict[str, str] = {
            _normalized_name(resource.canonical_name): "/canonical_name"
        }
        for alias_index, alias in enumerate(resource.alternative_names):
            normalized = _normalized_name(alias.text)
            path = f"/resources/{index}/alternative_names/{alias_index}/text"
            previous = names.get(normalized)
            if previous is not None:
                errors.append(
                    _diagnostic(
                        "duplicate_resource_name",
                        path,
                        source,
                        f"Normalized name duplicates {previous} within "
                        f"resource {resource.id!r}.",
                    )
                )
            else:
                names[normalized] = path


def _registered(
    catalog: Catalog,
    vocabulary: str,
    value: str,
    path: str,
    source: str,
    errors: list[CatalogDiagnostic],
) -> None:
    definition = catalog.vocabularies.root.get(vocabulary)
    if definition is None:
        errors.append(
            _diagnostic(
                "missing_vocabulary",
                path,
                source,
                f"Vocabulary {vocabulary!r} is not present in the catalog registry.",
            )
        )
    elif value not in definition.ids:
        errors.append(
            _diagnostic(
                "unregistered_vocabulary_value",
                path,
                source,
                f"Value {value!r} is not registered in vocabulary {vocabulary!r}.",
            )
        )


def _open_registered(
    catalog: Catalog,
    registry: str,
    value: str,
    path: str,
    source: str,
    errors: list[CatalogDiagnostic],
) -> None:
    definition = catalog.registries.root.get(registry)
    ids = {term.id for term in definition.terms} if definition is not None else set()
    if definition is None:
        errors.append(
            _diagnostic(
                "missing_open_registry",
                path,
                source,
                f"Open registry {registry!r} is not present in the catalog.",
            )
        )
    elif value not in ids:
        errors.append(
            _diagnostic(
                "unregistered_open_registry_value",
                path,
                source,
                f"Value {value!r} is not registered in open registry {registry!r}.",
            )
        )


def _validate_vocabularies(
    catalog: Catalog, source_paths: Mapping[str, str], errors: list[CatalogDiagnostic]
) -> None:
    for required_vocabulary in sorted(
        _REQUIRED_VOCABULARIES - catalog.vocabularies.root.keys()
    ):
        errors.append(
            _diagnostic(
                "missing_vocabulary",
                f"/vocabularies/{required_vocabulary}",
                "<catalog>",
                f"Required vocabulary {required_vocabulary!r} is absent.",
            )
        )
    for required_registry in sorted(
        _REQUIRED_OPEN_REGISTRIES - catalog.registries.root.keys()
    ):
        errors.append(
            _diagnostic(
                "missing_open_registry",
                f"/registries/{required_registry}",
                "<catalog>",
                f"Required open registry {required_registry!r} is absent.",
            )
        )

    closed_by_field = {
        "categories": "resource_category",
        "language_stage_ids": "language_stage",
        "source_language_stage_ids": "language_stage",
        "maintenance_status": "maintenance_status",
        "witness_type": "witness_kind",
        "party_type": "party_type",
        "role": "responsible_party_role",
        "name_type": "name_type",
        "dating_method": "dating_method",
        "certainty": "certainty",
        "source_kind": "evidence_source_kind",
        "alignment_unit": "alignment_unit",
        "production_method": "annotation_production_method",
        "quality": "annotation_quality",
        "supported_tasks": "annotation_task",
        "task": "annotation_task",
        "input_formats": "data_format",
        "output_formats": "data_format",
        "format_id": "data_format",
        "inner_format_ids": "data_format",
        "unit": "size_unit",
        "transcription_methods": "transcription_method",
        "unicode_normalization": "unicode_normalization",
        "layer_type": "text_layer_kind",
        "overlap_extent": "overlap_extent",
        "origin": "measurement_origin",
        "machine_readable_availability": "availability",
        "authentication_or_agreement": "access_requirement",
        "public_description": "availability",
        "online_browsing": "availability",
        "download": "availability",
        "api_access": "availability",
        "request_only": "availability",
        "automated_access": "permission_state",
        "model_training": "permission_state",
        "original_redistribution": "permission_state",
        "processed_redistribution": "permission_state",
        "trained_weights_publication": "permission_state",
        "status": "knowledge_state",
    }
    open_by_field = {
        "region_ids": "regions",
        "dialect_ids": "dialects",
        "genres": "genres",
        "text_types": "text_types",
        "target_language_ids": "languages",
        "primary_language_ids": "languages",
        "mixed_language_ids": "languages",
        "license_id": "licenses",
        "language": "languages",
    }
    for root_path, root in _validation_roots(catalog):
        source = _source_for(getattr(root, "id", ""), "<catalog>", source_paths)
        for path, value in _walk(root, root_path):
            if "/claims/" in path or path.endswith("/claims"):
                continue
            field = path.rsplit("/", maxsplit=1)[-1]
            vocabulary = closed_by_field.get(field)
            if field == "kind":
                if "/relationships/" in path:
                    vocabulary = "relationship_kind"
                elif "/access_urls/" in path:
                    vocabulary = "url_purpose"
                elif "/distributions/" in path:
                    vocabulary = "distribution_kind"
                elif "/editorial_interventions/" in path:
                    vocabulary = "editorial_intervention"
            elif field == "task" and "/supervision_suitability/" in path:
                vocabulary = "auxiliary_task"
            elif field == "status" and re.search(
                r"/(?:license|software_license|model_license)/status$", path
            ):
                vocabulary = "license_status"
            elif field == "status" and re.search(
                r"/supervision_suitability/value/\d+/status$", path
            ):
                vocabulary = "suitability_decision"
            elif field == "scopes" and "/license" in path:
                vocabulary = "license_scope"
            elif field == "availability":
                vocabulary = "availability"
            registry = open_by_field.get(field)
            if vocabulary is not None:
                for item in _known_values(value):
                    if isinstance(item, str):
                        _registered(catalog, vocabulary, item, path, source, errors)
                if isinstance(value, (set, frozenset)):
                    for item in value:
                        if isinstance(item, str):
                            _registered(catalog, vocabulary, item, path, source, errors)
                elif isinstance(value, str):
                    _registered(catalog, vocabulary, value, path, source, errors)
            if registry is not None:
                for item in _known_values(value):
                    if isinstance(item, str):
                        _open_registered(catalog, registry, item, path, source, errors)

    for registry_name, open_definition in catalog.registries.root.items():
        normalized: dict[str, str] = {}
        for index, term in enumerate(open_definition.terms):
            for label_index, label in enumerate([term.canonical_label, *term.aliases]):
                key = _normalized_name(label)
                path = (
                    f"/registries/{registry_name}/terms/{index}/canonical_label"
                    if label_index == 0
                    else f"/registries/{registry_name}/terms/{index}/aliases/"
                    f"{label_index - 1}"
                )
                previous = normalized.get(key)
                if previous is not None:
                    errors.append(
                        _diagnostic(
                            "duplicate_registry_name",
                            path,
                            "<catalog>",
                            f"Normalized registry name duplicates {previous}.",
                        )
                    )
                else:
                    normalized[key] = path


def _validation_roots(catalog: Catalog) -> Iterable[tuple[str, BaseModel]]:
    for field in ("resources", "works", "witnesses", "publications", "relationships"):
        for index, value in enumerate(getattr(catalog, field)):
            yield f"/{field}/{index}", value
    for registry_name, registry in catalog.registries.root.items():
        for index, term in enumerate(registry.terms):
            yield f"/registries/{registry_name}/terms/{index}", term


def _reference(
    by_id: Mapping[str, _Entity],
    identifier: str,
    expected: str,
    path: str,
    source: str,
    errors: list[CatalogDiagnostic],
) -> None:
    target = by_id.get(identifier)
    if target is None:
        errors.append(
            _diagnostic(
                "dangling_reference",
                path,
                source,
                f"Reference {identifier!r} does not resolve.",
            )
        )
    elif target.kind != expected:
        errors.append(
            _diagnostic(
                "reference_type_mismatch",
                path,
                source,
                f"Reference {identifier!r} resolves to {target.kind}, not {expected}.",
            )
        )


def _validate_references(
    catalog: Catalog,
    by_id: Mapping[str, _Entity],
    source_paths: Mapping[str, str],
    errors: list[CatalogDiagnostic],
) -> None:
    for resource_index, resource in enumerate(catalog.resources):
        base = f"/resources/{resource_index}"
        source = _source_for(resource.id, "<catalog>", source_paths)
        for publication_id in resource.publication_ids:
            _reference(
                by_id,
                publication_id,
                "publication",
                f"{base}/publication_ids",
                source,
                errors,
            )
        if isinstance(resource.dictionary, KnownValue):
            profile = resource.dictionary.value
            for publication_id in profile.source_publication_ids:
                _reference(
                    by_id,
                    publication_id,
                    "publication",
                    f"{base}/dictionary/value/source_publication_ids",
                    source,
                    errors,
                )
            if isinstance(profile.corpus_occurrence_links, KnownValue):
                for related_id in profile.corpus_occurrence_links.value.resource_ids:
                    _reference(
                        by_id,
                        related_id,
                        "resource",
                        f"{base}/dictionary/value/corpus_occurrence_links/value/"
                        "resource_ids",
                        source,
                        errors,
                    )
        for version_index, version in enumerate(resource.versions):
            version_base = f"{base}/versions/{version_index}"
            for document_index, document in enumerate(version.documents):
                document_base = f"{version_base}/documents/{document_index}"
                for field, kind in (
                    ("work_ids", "work"),
                    ("witness_ids", "witness"),
                    ("edition_witness_ids", "witness"),
                ):
                    for identifier in getattr(document, field):
                        _reference(
                            by_id,
                            identifier,
                            kind,
                            f"{document_base}/{field}",
                            source,
                            errors,
                        )
        if isinstance(resource.tool, KnownValue):
            for field in ("training_data", "evaluation_data"):
                datasets = getattr(resource.tool.value, field)
                if isinstance(datasets, KnownValue):
                    for dataset_index, dataset in enumerate(datasets.value):
                        if isinstance(dataset.resource_id, KnownValue):
                            _reference(
                                by_id,
                                dataset.resource_id.value,
                                "resource",
                                f"{base}/tool/value/{field}/value/{dataset_index}/"
                                "resource_id/value",
                                source,
                                errors,
                            )
    for witness_index, witness in enumerate(catalog.witnesses):
        source = _source_for(witness.id, "<catalog>", source_paths)
        for work_id in witness.work_ids:
            _reference(
                by_id,
                work_id,
                "work",
                f"/witnesses/{witness_index}/work_ids",
                source,
                errors,
            )
    for relationship_index, relationship in enumerate(catalog.relationships):
        source = _source_for(relationship.id, "<catalog>", source_paths)
        for side in ("source", "target"):
            reference = getattr(relationship, side)
            _reference(
                by_id,
                reference.id,
                reference.entity_type,
                f"/relationships/{relationship_index}/{side}",
                source,
                errors,
            )
    for entity in by_id.values():
        if isinstance(entity.value, ProvenancedRecord):
            local_evidence = {item.id for item in entity.value.evidence}
            for path, value in _walk(entity.value, entity.path):
                if path.endswith("/evidence_ids"):
                    evidence_ids = value if isinstance(value, Collection) else ()
                    for evidence_id in evidence_ids:
                        if evidence_id not in local_evidence:
                            errors.append(
                                _diagnostic(
                                    "nonlocal_evidence_reference",
                                    path,
                                    entity.source,
                                    f"Evidence {evidence_id!r} is not local to "
                                    f"record {entity.identifier!r}.",
                                )
                            )
            for evidence_index, evidence in enumerate(entity.value.evidence):
                if isinstance(evidence.publication_id, KnownValue):
                    _reference(
                        by_id,
                        evidence.publication_id.value,
                        "publication",
                        f"{entity.path}/evidence/{evidence_index}/publication_id/value",
                        entity.source,
                        errors,
                    )


def _scope_owner_error(
    identifier: str,
    selected: frozenset[str],
    owner: Mapping[str, str],
) -> bool:
    return bool(selected and owner.get(identifier) not in selected)


def _validate_scopes(
    catalog: Catalog,
    by_id: Mapping[str, _Entity],
    version_owner: Mapping[str, str],
    child_owner: Mapping[str, str],
    source_paths: Mapping[str, str],
    errors: list[CatalogDiagnostic],
) -> None:
    expected = {
        "resource_ids": "resource",
        "version_ids": "version",
        "component_ids": "component",
        "document_ids": "document",
        "annotation_ids": "annotation",
    }
    for root_path, root in _validation_roots(catalog):
        source = _source_for(getattr(root, "id", ""), "<catalog>", source_paths)
        for path, value in _walk(root, root_path):
            if not isinstance(value, SelectionScope):
                continue
            for field, kind in expected.items():
                for identifier in getattr(value, field):
                    _reference(
                        by_id,
                        identifier,
                        kind,
                        f"{path}/{field}",
                        source,
                        errors,
                    )
            for version_id in value.version_ids:
                if _scope_owner_error(version_id, value.resource_ids, version_owner):
                    errors.append(
                        _diagnostic(
                            "incoherent_scope",
                            path,
                            source,
                            f"Version {version_id!r} is not a descendant of the "
                            "selected resource IDs.",
                        )
                    )
            for field in ("component_ids", "document_ids", "annotation_ids"):
                for identifier in getattr(value, field):
                    owner_version = child_owner.get(identifier)
                    if value.version_ids and owner_version not in value.version_ids:
                        errors.append(
                            _diagnostic(
                                "incoherent_scope",
                                path,
                                source,
                                f"{identifier!r} is not a descendant of the "
                                "selected version IDs.",
                            )
                        )
                    resource_id = version_owner.get(owner_version or "")
                    if value.resource_ids and resource_id not in value.resource_ids:
                        errors.append(
                            _diagnostic(
                                "incoherent_scope",
                                path,
                                source,
                                f"{identifier!r} is not a descendant of the "
                                "selected resource IDs.",
                            )
                        )
            if value.component_ids and value.document_ids:
                for document_id in value.document_ids:
                    document = by_id.get(document_id)
                    components = (
                        document.value.component_ids
                        if document is not None and isinstance(document.value, Document)
                        else frozenset()
                    )
                    if not components.intersection(value.component_ids):
                        errors.append(
                            _diagnostic(
                                "incoherent_scope",
                                path,
                                source,
                                f"Document {document_id!r} is not a descendant of "
                                "any selected component.",
                            )
                        )


def _cycle_nodes(edges: Mapping[str, set[str]]) -> set[str]:
    visiting: set[str] = set()
    visited: set[str] = set()
    cyclic: set[str] = set()

    def visit(node: str, trail: list[str]) -> None:
        if node in visiting:
            cyclic.update(trail[trail.index(node) :])
            return
        if node in visited:
            return
        visiting.add(node)
        trail.append(node)
        for target in sorted(edges.get(node, set())):
            visit(target, trail)
        trail.pop()
        visiting.remove(node)
        visited.add(node)

    for node in sorted(edges):
        visit(node, [])
    return cyclic


def _validate_relationships(
    catalog: Catalog,
    by_id: Mapping[str, _Entity],
    version_owner: Mapping[str, str],
    child_owner: Mapping[str, str],
    source_paths: Mapping[str, str],
    errors: list[CatalogDiagnostic],
) -> None:
    graphs: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for index, relationship in enumerate(catalog.relationships):
        path = f"/relationships/{index}"
        source = _source_for(relationship.id, "<catalog>", source_paths)
        for side in ("source", "target"):
            endpoint = getattr(relationship, side)
            scope = getattr(relationship, f"{side}_scope")
            selected: frozenset[str] = getattr(
                scope, f"{endpoint.entity_type}_ids", frozenset()
            )
            if selected and selected != frozenset({endpoint.id}):
                errors.append(
                    _diagnostic(
                        "relationship_scope_mismatch",
                        f"{path}/{side}_scope",
                        source,
                        f"{side} scope must select only endpoint {endpoint.id!r} "
                        "at its entity level.",
                    )
                )
            endpoint_resource = (
                endpoint.id
                if endpoint.entity_type == "resource"
                else version_owner.get(endpoint.id)
                if endpoint.entity_type == "version"
                else version_owner.get(child_owner.get(endpoint.id, ""))
            )
            endpoint_version = (
                endpoint.id
                if endpoint.entity_type == "version"
                else child_owner.get(endpoint.id)
            )
            incompatible = False
            if endpoint.entity_type == "resource":
                incompatible = any(
                    identifier != endpoint.id for identifier in scope.resource_ids
                ) or any(
                    version_owner.get(identifier) != endpoint.id
                    for identifier in scope.version_ids
                )
            elif endpoint.entity_type == "version":
                incompatible = any(
                    identifier != endpoint.id for identifier in scope.version_ids
                )
            if endpoint_resource is not None:
                incompatible = incompatible or any(
                    identifier != endpoint_resource for identifier in scope.resource_ids
                )
            if endpoint_version is not None:
                incompatible = incompatible or any(
                    child_owner.get(identifier) != endpoint_version
                    for identifier in (
                        *scope.component_ids,
                        *scope.document_ids,
                        *scope.annotation_ids,
                    )
                )
            if incompatible:
                errors.append(
                    _diagnostic(
                        "relationship_scope_mismatch",
                        f"{path}/{side}_scope",
                        source,
                        f"{side} scope selects entities outside endpoint "
                        f"{endpoint.id!r}.",
                    )
                )
        if relationship.kind in {"derived_from", "supersedes"}:
            graphs[relationship.kind][relationship.source.id].add(
                relationship.target.id
            )
        elif relationship.kind == "contains":
            graphs["containment"][relationship.source.id].add(relationship.target.id)
        elif relationship.kind == "part_of":
            graphs["containment"][relationship.target.id].add(relationship.source.id)
        if relationship.kind in {"contains", "part_of"}:
            left = by_id.get(relationship.source.id)
            right = by_id.get(relationship.target.id)
            if left is not None and right is not None and left.kind != right.kind:
                errors.append(
                    _diagnostic(
                        "incompatible_relationship_endpoints",
                        path,
                        source,
                        "contains/part_of endpoints must have the same entity type.",
                    )
                )
    for graph_name, edges in sorted(graphs.items()):
        for identifier in sorted(_cycle_nodes(edges)):
            entity = by_id.get(identifier)
            errors.append(
                _diagnostic(
                    "relationship_cycle",
                    entity.path if entity is not None else "/relationships",
                    entity.source if entity is not None else "<catalog>",
                    f"{graph_name} graph contains a cycle through {identifier!r}.",
                )
            )


def _validate_provenance(
    catalog: Catalog,
    source_paths: Mapping[str, str],
    errors: list[CatalogDiagnostic],
) -> None:
    records: list[tuple[str, ProvenancedRecord]] = []
    for field in ("resources", "works", "witnesses", "publications", "relationships"):
        records.extend(
            (f"/{field}/{index}", record)
            for index, record in enumerate(getattr(catalog, field))
        )
    for registry_name, registry in catalog.registries.root.items():
        records.extend(
            (f"/registries/{registry_name}/terms/{index}", term)
            for index, term in enumerate(registry.terms)
        )
    for path, record in records:
        source = _source_for(getattr(record, "id", ""), "<catalog>", source_paths)
        report = provenance_completeness_report(record)
        for issue in report.errors:
            errors.append(
                _diagnostic(
                    f"provenance_{issue.error_code}",
                    f"{path}{issue.pointer}",
                    source,
                    issue.message,
                )
            )


def _validate_payload_policy(catalog: Catalog, errors: list[CatalogDiagnostic]) -> None:
    document = catalog.model_dump(mode="json")
    for path, value in _walk(document, ""):
        key = path.rsplit("/", maxsplit=1)[-1]
        if key in _FORBIDDEN_PAYLOAD_KEYS:
            errors.append(
                _diagnostic(
                    "forbidden_payload_key",
                    path,
                    "<catalog>",
                    f"Key {key!r} may not store local paths or third-party payloads.",
                )
            )
        if isinstance(value, str) and (
            value.lower().startswith("file:")
            or _DRIVE_PATH.match(value)
            or value.startswith(("./", "../", ".\\", "..\\"))
        ):
            errors.append(
                _diagnostic(
                    "forbidden_local_path",
                    path,
                    "<catalog>",
                    "Catalog values may not contain local filesystem paths.",
                )
            )


def validate_catalog(
    catalog: Catalog, *, source_paths: Mapping[str, str] | None = None
) -> CatalogValidationReport:
    """Run all safe independent catalog checks and return stable diagnostics."""

    sources = source_paths or {}
    errors: list[CatalogDiagnostic] = []
    entities, version_owner, child_owner = _catalog_entities(catalog, sources)
    by_id = _validate_ids(entities, errors)
    _validate_names(catalog, errors)
    _validate_vocabularies(catalog, sources, errors)
    _validate_references(catalog, by_id, sources, errors)
    _validate_scopes(catalog, by_id, version_owner, child_owner, sources, errors)
    _validate_relationships(catalog, by_id, version_owner, child_owner, sources, errors)
    _validate_provenance(catalog, sources, errors)
    _validate_payload_policy(catalog, errors)
    ordered = tuple(
        sorted(
            errors,
            key=lambda item: (
                item.path,
                item.model_path,
                item.code,
                item.message,
            ),
        )
    )
    return CatalogValidationReport(errors=ordered)


class _BoundaryUnavailable(Exception):
    pass


def _load_catalog(path: Path) -> tuple[Catalog, Mapping[str, str]]:
    if path.is_file() and path.suffix.casefold() == ".json":
        return Catalog.model_validate_json(path.read_text(encoding="utf-8")), {}
    try:
        from histgerm.loading import load_catalog
    except ImportError as error:
        raise _BoundaryUnavailable(
            "Safe YAML inventory loading is not installed yet. Provide a JSON "
            "Catalog fixture, or complete P2-LOADING-SERIALIZATION before "
            "validating authoring YAML."
        ) from error
    loaded = load_catalog(path)
    if isinstance(loaded, Catalog):
        return loaded, {}
    if (
        isinstance(loaded, tuple)
        and len(loaded) == 2
        and isinstance(loaded[0], Catalog)
        and isinstance(loaded[1], Mapping)
    ):
        return loaded
    raise _BoundaryUnavailable(
        "histgerm.loading.load_catalog returned an unsupported boundary value."
    )


def _envelope(
    status: str,
    *,
    errors: Iterable[CatalogDiagnostic | Mapping[str, object]] = (),
    result: Mapping[str, object] | None = None,
) -> dict[str, object]:
    serialized: list[object] = []
    for error in errors:
        if isinstance(error, CatalogDiagnostic):
            serialized.append(error.model_dump(mode="json"))
        else:
            serialized.append(dict(error))
    return {
        "protocol": _PROTOCOL,
        "command": "validation.inventory",
        "status": status,
        "errors": serialized,
        "warnings": [],
        "unresolved": [],
        "changes": [],
        "result": dict(result or {}),
    }


def _print_human(report: CatalogValidationReport) -> None:
    if report.is_valid:
        print("Catalog validation passed.")
        return
    for error in report.errors:
        print(
            f"{error.path}:{error.model_path}: {error.code}: {error.message}",
            file=sys.stderr,
        )
    print(
        f"Catalog validation failed with {len(report.errors)} error(s).",
        file=sys.stderr,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the stable inventory validation command."""

    parser = argparse.ArgumentParser(prog="python -m histgerm.validation")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inventory = subparsers.add_parser("inventory")
    inventory.add_argument("path", nargs="?", default="inventory")
    inventory.add_argument("--format", choices=("human", "json"), default="human")
    arguments = parser.parse_args(argv)
    output_format = str(arguments.format)
    try:
        catalog, sources = _load_catalog(Path(str(arguments.path)))
        report = validate_catalog(catalog, source_paths=sources)
    except (
        OSError,
        json.JSONDecodeError,
        ValidationError,
        _BoundaryUnavailable,
    ) as error:
        diagnostic = {
            "code": "input_error",
            "severity": "error",
            "path": str(arguments.path),
            "model_path": "",
            "pointer": "",
            "message": str(error),
        }
        if output_format == "json":
            print(
                json.dumps(
                    _envelope("error", errors=[diagnostic]),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            print(f"{arguments.path}: input_error: {error}", file=sys.stderr)
        return 2
    if output_format == "json":
        print(
            json.dumps(
                _envelope(
                    "ok" if report.is_valid else "error",
                    errors=report.errors,
                    result={"error_count": len(report.errors)},
                ),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        _print_human(report)
    return 0 if report.is_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CatalogDiagnostic",
    "CatalogValidationReport",
    "main",
    "validate_catalog",
]
