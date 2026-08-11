"""Immutable relationship traversal and overlap-aware size summaries."""

from __future__ import annotations

import unicodedata
from collections import defaultdict, deque
from collections.abc import Iterable
from decimal import Decimal
from typing import Literal, cast

from pydantic import field_validator, model_validator

from histgerm.models.catalog import Catalog
from histgerm.models.common import (
    HistGermModel,
    JsonPointer,
    KnownValue,
    NonEmptyStr,
    SelectionScope,
    StableId,
    VocabularyId,
)
from histgerm.models.corpus import SizeMeasurement
from histgerm.models.relationships import Relationship
from histgerm.query.filters import HistGermQueryError, InvalidQueryError

type SizeUnit = Literal[
    "document", "sentence", "orthographic_word", "token", "character", "byte"
]
type Exactness = Literal["exact", "non_exact", "not_computable"]
type Direction = Literal["outgoing", "incoming", "both"]


class AmbiguousMeasurementError(HistGermQueryError):
    def __init__(self, scope: SelectionScope, paths: Iterable[str]) -> None:
        rendered = ", ".join(sorted(paths))
        super().__init__(
            "multiple non-equivalent measurements match scope "
            f"{scope.model_dump_json()}: {rendered}"
        )


class IncompatibleMeasurementError(HistGermQueryError):
    def __init__(self, unit: str, method: str) -> None:
        super().__init__(
            f"no size measurement for unit {unit!r} is compatible with "
            f"counting method {method!r}"
        )


class RelationshipCycleError(HistGermQueryError):
    def __init__(self, graph: str, id_path: Iterable[str]) -> None:
        super().__init__(f"{graph} relationship cycle detected: {' -> '.join(id_path)}")
        self.graph = graph
        self.id_path = tuple(id_path)


class InvalidOverlapError(HistGermQueryError):
    def __init__(self, relationship_id: str, reason: str) -> None:
        super().__init__(
            f"overlap relationship {relationship_id!r} is inconsistent: {reason}"
        )
        self.relationship_id = relationship_id


class RelationshipRequest(HistGermModel):
    resource_id: StableId
    kinds: frozenset[VocabularyId] | None = None
    direction: Direction = "both"
    transitive: bool = False

    @field_validator("kinds", mode="before")
    @classmethod
    def freeze_kinds(cls, value: object) -> object:
        return frozenset(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_request(self) -> RelationshipRequest:
        if not self.resource_id.startswith("res-"):
            raise InvalidQueryError("relationship resource_id must identify a resource")
        if self.kinds is not None and not self.kinds:
            raise InvalidQueryError("relationship kinds must not be empty")
        return self


class RelationshipSnapshot(HistGermModel):
    relationship_id: StableId
    source_id: StableId
    target_id: StableId
    kind: VocabularyId
    directional: bool
    source_scope: SelectionScope
    target_scope: SelectionScope
    overlap_extent: Literal["exact", "contains", "partial", "unknown"] | None
    certainty_status: Literal[
        "known", "unknown", "not_applicable", "not_publicly_available"
    ]
    certainty_id: VocabularyId | None
    evidence_ids: tuple[StableId, ...]


class RelationshipPath(HistGermModel):
    relationship_ids: tuple[StableId, ...]
    endpoint_ids: tuple[StableId, ...]


class RelationshipResult(HistGermModel):
    relationships: tuple[RelationshipSnapshot, ...]
    paths: tuple[RelationshipPath, ...]
    cycles: tuple[RelationshipPath, ...]
    warnings: tuple[str, ...]


class SizeSummaryRequest(HistGermModel):
    selection: SelectionScope
    unit: SizeUnit
    counting_method: NonEmptyStr | None = None
    deduplicate: bool = True

    @model_validator(mode="after")
    def require_version(self) -> SizeSummaryRequest:
        if not self.selection.version_ids:
            raise InvalidQueryError(
                "size selection must identify at least one resource version"
            )
        return self


class MeasurementRef(HistGermModel):
    measurement_path: JsonPointer
    unit: SizeUnit
    value: Decimal
    counting_method: str
    origin: Literal["reported", "locally_computed"]
    scope: SelectionScope
    uncertainty_note: str | None
    evidence_ids: tuple[StableId, ...]


class SizeExclusion(HistGermModel):
    measurement_path: JsonPointer
    reason: Literal[
        "exact_duplicate",
        "contained_scope",
        "incompatible_unit",
        "incompatible_counting_method",
        "superseded_measurement",
    ]
    relationship_ids: tuple[StableId, ...]
    canonical_measurement_path: JsonPointer | None


class SizeAdjustment(HistGermModel):
    relationship_id: StableId
    overlap_measurement_path: JsonPointer
    subtracted_value: Decimal
    unit: SizeUnit
    counting_method: str
    source_measurement_paths: tuple[JsonPointer, ...]


class UnresolvedOverlap(HistGermModel):
    relationship_id: StableId
    source_scope: SelectionScope
    target_scope: SelectionScope
    reason: Literal[
        "extent_unknown",
        "partial_overlap_unquantified",
        "overlap_unit_incompatible",
        "overlap_counting_method_incompatible",
        "overlap_scope_incompatible",
        "conflicting_overlap_measurements",
    ]
    evidence_ids: tuple[StableId, ...]


class SizeSummary(HistGermModel):
    request: SizeSummaryRequest
    selected_measurements: tuple[MeasurementRef, ...]
    counted_measurement_paths: tuple[JsonPointer, ...]
    exclusions: tuple[SizeExclusion, ...]
    adjustments: tuple[SizeAdjustment, ...]
    assumptions: tuple[str, ...]
    unresolved_overlaps: tuple[UnresolvedOverlap, ...]
    total: Decimal | None
    exactness: Exactness
    unit: SizeUnit
    counting_method: str | None


class _LocatedMeasurement(HistGermModel):
    path: JsonPointer
    measurement: SizeMeasurement


def _normalize_method(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _scope_key(scope: SelectionScope) -> tuple[tuple[str, ...], ...]:
    return (
        tuple(sorted(scope.resource_ids)),
        tuple(sorted(scope.version_ids)),
        tuple(sorted(scope.component_ids)),
        tuple(sorted(scope.document_ids)),
        tuple(sorted(scope.annotation_ids)),
    )


def _scope_matches_selection(scope: SelectionScope, selected: SelectionScope) -> bool:
    if not scope.version_ids.intersection(selected.version_ids):
        return False
    specific = (
        (scope.annotation_ids, selected.annotation_ids),
        (scope.document_ids, selected.document_ids),
        (scope.component_ids, selected.component_ids),
    )
    for measurement_ids, selected_ids in specific:
        if measurement_ids:
            return bool(measurement_ids.intersection(selected_ids))
    return True


def _relationship_evidence_ids(relationship: Relationship) -> tuple[StableId, ...]:
    ids = {item.id for item in relationship.evidence}
    if isinstance(relationship.overlap_measurement, KnownValue):
        ids.update(relationship.overlap_measurement.value.evidence_ids)
    return tuple(sorted(ids))


def _snapshot(relationship: Relationship) -> RelationshipSnapshot:
    certainty_id = (
        relationship.certainty.value
        if isinstance(relationship.certainty, KnownValue)
        else None
    )
    return RelationshipSnapshot(
        relationship_id=relationship.id,
        source_id=relationship.source.id,
        target_id=relationship.target.id,
        kind=relationship.kind,
        directional=relationship.directional,
        source_scope=relationship.source_scope,
        target_scope=relationship.target_scope,
        overlap_extent=relationship.overlap_extent,
        certainty_status=relationship.certainty.status,
        certainty_id=certainty_id,
        evidence_ids=_relationship_evidence_ids(relationship),
    )


def _canonical_cycle(nodes: list[str]) -> tuple[str, ...]:
    body = nodes[:-1]
    smallest = min(body)
    index = body.index(smallest)
    rotated = body[index:] + body[:index]
    return tuple([*rotated, rotated[0]])


def _find_cycle(
    edges: dict[str, list[tuple[str, str]]],
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    state: dict[str, int] = {}
    node_stack: list[str] = []
    relationship_stack: list[str] = []

    def visit(node: str) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
        state[node] = 1
        node_stack.append(node)
        for target, relationship_id in sorted(edges.get(node, [])):
            if state.get(target, 0) == 0:
                relationship_stack.append(relationship_id)
                found = visit(target)
                if found is not None:
                    return found
                relationship_stack.pop()
            elif state[target] == 1:
                start = node_stack.index(target)
                raw_nodes = [*node_stack[start:], target]
                raw_relationships = [*relationship_stack[start:], relationship_id]
                canonical_nodes = _canonical_cycle(raw_nodes)
                offset = raw_nodes[:-1].index(canonical_nodes[0])
                canonical_relationships = tuple(
                    raw_relationships[offset:] + raw_relationships[:offset]
                )
                return canonical_nodes, canonical_relationships
        node_stack.pop()
        state[node] = 2
        return None

    for node in sorted(edges):
        if state.get(node, 0) == 0:
            found = visit(node)
            if found is not None:
                return found
    return None


def _graph_edges(
    relationships: Iterable[Relationship], graph: str
) -> dict[str, list[tuple[str, str]]]:
    edges: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for relationship in relationships:
        if graph == "containment":
            if relationship.kind == "contains":
                source, target = relationship.source.id, relationship.target.id
            elif relationship.kind == "part_of":
                source, target = relationship.target.id, relationship.source.id
            else:
                continue
        elif relationship.kind == "derived_from":
            source, target = relationship.source.id, relationship.target.id
        else:
            continue
        edges[source].append((target, relationship.id))
    return edges


def _validate_acyclic(relationships: Iterable[Relationship]) -> None:
    relationship_list = list(relationships)
    for graph in ("containment", "derivation"):
        found = _find_cycle(_graph_edges(relationship_list, graph))
        if found is not None:
            raise RelationshipCycleError(graph, found[0])


def _located_measurements(catalog: Catalog) -> tuple[_LocatedMeasurement, ...]:
    located: list[_LocatedMeasurement] = []
    for resource_index, resource in enumerate(catalog.resources):
        for version_index, version in enumerate(resource.versions):
            base = f"/resources/{resource_index}/versions/{version_index}"
            for index, measurement in enumerate(version.size_measurements):
                located.append(
                    _LocatedMeasurement(
                        path=f"{base}/size_measurements/{index}",
                        measurement=measurement,
                    )
                )
            for component_index, component in enumerate(version.components):
                for index, measurement in enumerate(component.size_measurements):
                    located.append(
                        _LocatedMeasurement(
                            path=(
                                f"{base}/components/{component_index}"
                                f"/size_measurements/{index}"
                            ),
                            measurement=measurement,
                        )
                    )
            for document_index, document in enumerate(version.documents):
                for index, measurement in enumerate(document.size_measurements):
                    located.append(
                        _LocatedMeasurement(
                            path=(
                                f"{base}/documents/{document_index}"
                                f"/size_measurements/{index}"
                            ),
                            measurement=measurement,
                        )
                    )
            for annotation_index, annotation in enumerate(version.annotations):
                for index, measurement in enumerate(annotation.coverage_measurements):
                    located.append(
                        _LocatedMeasurement(
                            path=(
                                f"{base}/annotations/{annotation_index}"
                                f"/coverage_measurements/{index}"
                            ),
                            measurement=measurement,
                        )
                    )
    return tuple(sorted(located, key=lambda item: item.path))


def _measurement_ref(located: _LocatedMeasurement) -> MeasurementRef:
    measurement = located.measurement
    note = (
        measurement.uncertainty_note.value
        if isinstance(measurement.uncertainty_note, KnownValue)
        else None
    )
    return MeasurementRef(
        measurement_path=located.path,
        unit=cast(SizeUnit, measurement.unit),
        value=Decimal(measurement.value),
        counting_method=measurement.counting_method,
        origin=measurement.origin,
        scope=measurement.scope,
        uncertainty_note=note,
        evidence_ids=tuple(sorted(measurement.evidence_ids)),
    )


def _touches_request(relationship: Relationship, resource_id: str) -> bool:
    return (
        relationship.source.id == resource_id
        or relationship.target.id == resource_id
        or resource_id in relationship.source_scope.resource_ids
        or resource_id in relationship.target_scope.resource_ids
    )


def _side_matches(
    relationship: Relationship, resource_id: str, side: Literal["source", "target"]
) -> bool:
    endpoint = getattr(relationship, side)
    selected_scope = getattr(relationship, f"{side}_scope")
    return endpoint.id == resource_id or resource_id in selected_scope.resource_ids


def _request_orientations(
    relationship: Relationship, resource_id: str, direction: Direction
) -> tuple[tuple[str, str], ...]:
    orientations: list[tuple[str, str]] = []
    source, target = relationship.source.id, relationship.target.id
    if (
        not relationship.directional or direction in {"outgoing", "both"}
    ) and _side_matches(relationship, resource_id, "source"):
        orientations.append((source, target))
    if (
        not relationship.directional or direction in {"incoming", "both"}
    ) and _side_matches(relationship, resource_id, "target"):
        orientations.append((target, source))
    return tuple(orientations)


def _oriented_edges(
    relationship: Relationship, direction: Direction
) -> tuple[tuple[str, str], ...]:
    source, target = relationship.source.id, relationship.target.id
    if not relationship.directional or direction == "both":
        return ((source, target), (target, source))
    if direction == "outgoing":
        return ((source, target),)
    return ((target, source),)


def _relationship_cycles(
    relationships: Iterable[Relationship],
) -> tuple[RelationshipPath, ...]:
    grouped: dict[str, list[Relationship]] = defaultdict(list)
    for relationship in relationships:
        if relationship.kind not in {"contains", "part_of", "derived_from"}:
            grouped[relationship.kind].append(relationship)
    cycles: list[RelationshipPath] = []
    for kind in sorted(grouped):
        if not grouped[kind][0].directional:
            found = _find_undirected_cycle(grouped[kind])
            if found is not None:
                cycles.append(
                    RelationshipPath(relationship_ids=found[1], endpoint_ids=found[0])
                )
            continue
        edges: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for relationship in grouped[kind]:
            for source, target in _oriented_edges(relationship, "outgoing"):
                edges[source].append((target, relationship.id))
        found = _find_cycle(edges)
        if found is not None:
            cycles.append(
                RelationshipPath(relationship_ids=found[1], endpoint_ids=found[0])
            )
    return tuple(
        sorted(
            cycles,
            key=lambda item: (
                len(item.relationship_ids),
                item.endpoint_ids,
                item.relationship_ids,
            ),
        )
    )


def _find_undirected_cycle(
    relationships: Iterable[Relationship],
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    adjacency: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for relationship in relationships:
        source, target = relationship.source.id, relationship.target.id
        adjacency[source].append((target, relationship.id))
        adjacency[target].append((source, relationship.id))
    visited: set[str] = set()
    node_stack: list[str] = []
    relationship_stack: list[str] = []

    def visit(
        node: str, parent_relationship: str | None
    ) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
        visited.add(node)
        node_stack.append(node)
        for target, relationship_id in sorted(adjacency[node]):
            if relationship_id == parent_relationship:
                continue
            if target not in visited:
                relationship_stack.append(relationship_id)
                found = visit(target, relationship_id)
                if found is not None:
                    return found
                relationship_stack.pop()
            elif target in node_stack:
                start = node_stack.index(target)
                raw_nodes = [*node_stack[start:], target]
                raw_relationships = [*relationship_stack[start:], relationship_id]
                canonical_nodes = _canonical_cycle(raw_nodes)
                offset = raw_nodes[:-1].index(canonical_nodes[0])
                canonical_relationships = tuple(
                    raw_relationships[offset:] + raw_relationships[:offset]
                )
                return canonical_nodes, canonical_relationships
        node_stack.pop()
        return None

    for node in sorted(adjacency):
        if node not in visited:
            found = visit(node, None)
            if found is not None:
                return found
    return None


def _relation_sort_key(relationship: Relationship) -> tuple[str, ...]:
    return (
        relationship.kind,
        relationship.source.id,
        relationship.target.id,
        relationship.id,
    )


def _scope_relationships(
    relationships: Iterable[Relationship],
    paths_by_scope: dict[tuple[tuple[str, ...], ...], list[_LocatedMeasurement]],
) -> list[tuple[Relationship, list[_LocatedMeasurement], list[_LocatedMeasurement]]]:
    relevant = []
    for relationship in relationships:
        source = paths_by_scope.get(_scope_key(relationship.source_scope), [])
        target = paths_by_scope.get(_scope_key(relationship.target_scope), [])
        if source and target:
            relevant.append((relationship, source, target))
    return relevant


def _unresolved(
    relationship: Relationship,
    reason: Literal[
        "extent_unknown",
        "partial_overlap_unquantified",
        "overlap_unit_incompatible",
        "overlap_counting_method_incompatible",
        "overlap_scope_incompatible",
        "conflicting_overlap_measurements",
    ],
) -> UnresolvedOverlap:
    return UnresolvedOverlap(
        relationship_id=relationship.id,
        source_scope=relationship.source_scope,
        target_scope=relationship.target_scope,
        reason=reason,
        evidence_ids=_relationship_evidence_ids(relationship),
    )


def _states_disjoint(relationship: Relationship) -> bool:
    texts: list[str] = []
    if isinstance(relationship.note, KnownValue):
        texts.append(relationship.note.value)
    for evidence in relationship.evidence:
        if isinstance(evidence.note, KnownValue):
            texts.append(evidence.note.value)
        if isinstance(evidence.quotation, KnownValue):
            texts.append(evidence.quotation.value)
    return any("disjoint" in text.casefold() for text in texts)


class CatalogQuery(HistGermModel):
    """Frozen read-only facade over a validated catalog."""

    catalog: Catalog

    def relationships(self, request: RelationshipRequest) -> RelationshipResult:
        _validate_acyclic(self.catalog.relationships)
        filtered = [
            relationship
            for relationship in self.catalog.relationships
            if request.kinds is None or relationship.kind in request.kinds
        ]
        direct = [
            relationship
            for relationship in filtered
            if _touches_request(relationship, request.resource_id)
            and _request_orientations(
                relationship, request.resource_id, request.direction
            )
        ]
        selected: dict[str, Relationship] = {
            relationship.id: relationship for relationship in direct
        }
        paths: list[RelationshipPath] = []

        orientations: list[tuple[str, str, Relationship]] = []
        for relationship in sorted(direct, key=_relation_sort_key):
            for source, target in _request_orientations(
                relationship, request.resource_id, request.direction
            ):
                orientations.append((source, target, relationship))
                paths.append(
                    RelationshipPath(
                        relationship_ids=(relationship.id,),
                        endpoint_ids=(source, target),
                    )
                )

        if request.transitive:
            adjacency: dict[str, list[tuple[str, Relationship]]] = defaultdict(list)
            for relationship in filtered:
                for source, target in _oriented_edges(relationship, request.direction):
                    adjacency[source].append((target, relationship))
            queue: deque[tuple[str, tuple[str, ...], tuple[str, ...]]] = deque(
                (target, (source, target), (relationship.id,))
                for source, target, relationship in orientations
            )
            seen_paths: set[tuple[str, ...]] = set()
            while queue:
                node, endpoints, relationship_ids = queue.popleft()
                for target, relationship in sorted(
                    adjacency.get(node, []),
                    key=lambda item: (
                        item[1].kind,
                        item[0],
                        item[1].id,
                    ),
                ):
                    if target in endpoints:
                        continue
                    new_relationships = (*relationship_ids, relationship.id)
                    new_endpoints = (*endpoints, target)
                    if (
                        len(new_relationships) > 1
                        and new_relationships not in seen_paths
                    ):
                        seen_paths.add(new_relationships)
                        selected[relationship.id] = relationship
                        paths.append(
                            RelationshipPath(
                                relationship_ids=new_relationships,
                                endpoint_ids=new_endpoints,
                            )
                        )
                    queue.append((target, new_endpoints, new_relationships))

        snapshots = tuple(
            _snapshot(relationship)
            for relationship in sorted(selected.values(), key=_relation_sort_key)
        )
        ordered_paths = tuple(
            sorted(
                paths,
                key=lambda item: (
                    len(item.relationship_ids),
                    item.endpoint_ids,
                    item.relationship_ids,
                ),
            )
        )
        return RelationshipResult(
            relationships=snapshots,
            paths=ordered_paths,
            cycles=_relationship_cycles(filtered),
            warnings=(),
        )

    def size_summary(self, request: SizeSummaryRequest) -> SizeSummary:
        _validate_acyclic(self.catalog.relationships)
        candidates = [
            item
            for item in _located_measurements(self.catalog)
            if _scope_matches_selection(item.measurement.scope, request.selection)
        ]
        exclusions: list[SizeExclusion] = []
        selected: list[_LocatedMeasurement] = []
        requested_method = (
            _normalize_method(request.counting_method)
            if request.counting_method is not None
            else None
        )
        for item in candidates:
            measurement = item.measurement
            if measurement.unit != request.unit:
                exclusions.append(
                    SizeExclusion(
                        measurement_path=item.path,
                        reason="incompatible_unit",
                        relationship_ids=(),
                        canonical_measurement_path=None,
                    )
                )
            elif (
                requested_method is not None
                and _normalize_method(measurement.counting_method) != requested_method
            ):
                exclusions.append(
                    SizeExclusion(
                        measurement_path=item.path,
                        reason="incompatible_counting_method",
                        relationship_ids=(),
                        canonical_measurement_path=None,
                    )
                )
            else:
                selected.append(item)
        if (
            request.counting_method is not None
            and not selected
            and any(item.measurement.unit == request.unit for item in candidates)
        ):
            raise IncompatibleMeasurementError(request.unit, request.counting_method)

        groups: dict[
            tuple[tuple[tuple[str, ...], ...], str], list[_LocatedMeasurement]
        ] = defaultdict(list)
        for item in selected:
            groups[
                (
                    _scope_key(item.measurement.scope),
                    _normalize_method(item.measurement.counting_method),
                )
            ].append(item)
        for items in groups.values():
            if len(items) > 1:
                raise AmbiguousMeasurementError(
                    items[0].measurement.scope, (item.path for item in items)
                )

        methods = {
            _normalize_method(item.measurement.counting_method) for item in selected
        }
        result_method = (
            request.counting_method
            if request.counting_method is not None
            else (
                selected[0].measurement.counting_method
                if len(methods) == 1 and selected
                else None
            )
        )
        paths_by_scope: dict[tuple[tuple[str, ...], ...], list[_LocatedMeasurement]] = (
            defaultdict(list)
        )
        for item in selected:
            paths_by_scope[_scope_key(item.measurement.scope)].append(item)
        relevant = _scope_relationships(self.catalog.relationships, paths_by_scope)

        if not request.deduplicate:
            branch_assumptions = ["overlap deduplication disabled"]
            uncertain = any(
                item.measurement.uncertainty_note.status != "not_applicable"
                for item in selected
            )
            has_overlap = any(
                relationship.kind in {"overlaps", "duplicates", "contains", "part_of"}
                for relationship, _, _ in relevant
            )
            computable = len(methods) <= 1
            total = (
                sum(
                    (Decimal(item.measurement.value) for item in selected),
                    Decimal(),
                )
                if computable
                else None
            )
            exactness: Exactness = (
                "not_computable"
                if not computable
                else "non_exact"
                if has_overlap or uncertain
                else "exact"
            )
            if total is not None and exactness == "non_exact":
                branch_assumptions.append(
                    "total is an unadjusted subtotal, not a deduplicated total"
                )
            return self._summary(
                request=request,
                selected=selected,
                counted=selected,
                exclusions=exclusions,
                adjustments=[],
                assumptions=branch_assumptions,
                unresolved=[],
                total=total,
                exactness=exactness,
                counting_method=result_method,
            )

        counted: dict[str, _LocatedMeasurement] = {item.path: item for item in selected}

        duplicate_edges = [
            item
            for item in relevant
            if item[0].kind == "duplicates" or item[0].overlap_extent == "exact"
        ]
        duplicate_adjacency: dict[str, list[tuple[str, Relationship]]] = defaultdict(
            list
        )
        scope_item = {_scope_key(item.measurement.scope): item for item in selected}
        for relationship, source_items, target_items in duplicate_edges:
            source = source_items[0]
            target = target_items[0]
            if _normalize_method(
                source.measurement.counting_method
            ) != _normalize_method(target.measurement.counting_method):
                raise InvalidOverlapError(
                    relationship.id, "exact duplicate counting methods are incompatible"
                )
            if source.measurement.value != target.measurement.value:
                raise InvalidOverlapError(
                    relationship.id, "exact duplicate measurements have unequal values"
                )
            duplicate_adjacency[source.path].append((target.path, relationship))
            duplicate_adjacency[target.path].append((source.path, relationship))

        visited: set[str] = set()
        for start in sorted(duplicate_adjacency):
            if start in visited:
                continue
            queue = deque([start])
            members: set[str] = set()
            relationship_ids: set[str] = set()
            canonical_paths: set[str] = set()
            while queue:
                path = queue.popleft()
                if path in members:
                    continue
                members.add(path)
                visited.add(path)
                for neighbor, relationship in duplicate_adjacency[path]:
                    relationship_ids.add(relationship.id)
                    if isinstance(relationship.canonical_scope, KnownValue):
                        canonical = scope_item.get(
                            _scope_key(relationship.canonical_scope.value)
                        )
                        if canonical is not None:
                            canonical_paths.add(canonical.path)
                    queue.append(neighbor)
            if len(canonical_paths) > 1:
                relationship_id = min(relationship_ids)
                raise InvalidOverlapError(
                    relationship_id, "duplicate group has conflicting canonical scopes"
                )
            canonical_path = (
                next(iter(canonical_paths)) if canonical_paths else min(members)
            )
            for path in sorted(members - {canonical_path}):
                counted.pop(path, None)
                exclusions.append(
                    SizeExclusion(
                        measurement_path=path,
                        reason="exact_duplicate",
                        relationship_ids=tuple(sorted(relationship_ids)),
                        canonical_measurement_path=canonical_path,
                    )
                )

        containment_graph: dict[
            tuple[tuple[str, ...], ...],
            list[tuple[tuple[tuple[str, ...], ...], Relationship]],
        ] = defaultdict(list)
        for relationship in self.catalog.relationships:
            if relationship.kind not in {"contains", "part_of"}:
                continue
            if relationship.kind == "contains":
                parent_scope = relationship.source_scope
                child_scope = relationship.target_scope
            else:
                parent_scope = relationship.target_scope
                child_scope = relationship.source_scope
            containment_graph[_scope_key(parent_scope)].append(
                (_scope_key(child_scope), relationship)
            )

        for parent_path in sorted(tuple(counted)):
            if parent_path not in counted:
                continue
            parent = counted[parent_path]
            queue_scopes: deque[tuple[tuple[tuple[str, ...], ...], tuple[str, ...]]] = (
                deque([(_scope_key(parent.measurement.scope), ())])
            )
            seen_scopes: set[tuple[tuple[str, ...], ...]] = set()
            while queue_scopes:
                current_scope_key, containment_ids = queue_scopes.popleft()
                if current_scope_key in seen_scopes:
                    continue
                seen_scopes.add(current_scope_key)
                for child_scope_key, relationship in sorted(
                    containment_graph.get(current_scope_key, []),
                    key=lambda item: (item[0], item[1].id),
                ):
                    path_ids = (*containment_ids, relationship.id)
                    child = scope_item.get(child_scope_key)
                    if child is not None and child.path in counted:
                        counted.pop(child.path)
                        exclusions.append(
                            SizeExclusion(
                                measurement_path=child.path,
                                reason="contained_scope",
                                relationship_ids=path_ids,
                                canonical_measurement_path=parent_path,
                            )
                        )
                    queue_scopes.append((child_scope_key, path_ids))

        unresolved: list[UnresolvedOverlap] = []
        adjustments: list[SizeAdjustment] = []
        partial = [
            item
            for item in relevant
            if item[0].kind == "overlaps"
            and item[0].overlap_extent in {"partial", "unknown"}
        ]
        active_partial = [
            item
            for item in partial
            if item[1][0].path in counted and item[2][0].path in counted
        ]
        degrees: dict[str, int] = defaultdict(int)
        for _, source_items, target_items in active_partial:
            degrees[source_items[0].path] += 1
            degrees[target_items[0].path] += 1
        multi_scope = any(degree > 1 for degree in degrees.values())
        disjoint_contract = all(
            _states_disjoint(relationship) for relationship, _, _ in active_partial
        )

        for relationship, source_items, target_items in sorted(
            active_partial, key=lambda item: _relation_sort_key(item[0])
        ):
            source = source_items[0]
            target = target_items[0]
            source_method = _normalize_method(source.measurement.counting_method)
            target_method = _normalize_method(target.measurement.counting_method)
            methods_compatible = source_method == target_method
            if not methods_compatible:
                unresolved.append(
                    _unresolved(relationship, "overlap_counting_method_incompatible")
                )
            if relationship.overlap_extent == "unknown":
                unresolved.append(_unresolved(relationship, "extent_unknown"))
                continue
            if not isinstance(relationship.overlap_measurement, KnownValue):
                unresolved.append(
                    _unresolved(relationship, "partial_overlap_unquantified")
                )
                continue
            overlap = relationship.overlap_measurement.value
            if overlap.unit != request.unit:
                unresolved.append(
                    _unresolved(relationship, "overlap_unit_incompatible")
                )
                continue
            overlap_method = _normalize_method(overlap.counting_method)
            if not methods_compatible or overlap_method != source_method:
                if methods_compatible:
                    unresolved.append(
                        _unresolved(
                            relationship, "overlap_counting_method_incompatible"
                        )
                    )
                continue
            if (
                overlap.source_scope != source.measurement.scope
                or overlap.target_scope != target.measurement.scope
                or overlap.source_version_id != source.measurement.version_id
                or overlap.target_version_id != target.measurement.version_id
            ):
                unresolved.append(
                    _unresolved(relationship, "overlap_scope_incompatible")
                )
                continue
            if multi_scope and not disjoint_contract:
                unresolved.append(
                    _unresolved(relationship, "conflicting_overlap_measurements")
                )
                continue
            if overlap.value > min(source.measurement.value, target.measurement.value):
                raise InvalidOverlapError(
                    relationship.id, "overlap exceeds an affected measurement"
                )
            relationship_index = self.catalog.relationships.index(relationship)
            adjustments.append(
                SizeAdjustment(
                    relationship_id=relationship.id,
                    overlap_measurement_path=(
                        f"/relationships/{relationship_index}/overlap_measurement/value"
                    ),
                    subtracted_value=Decimal(overlap.value),
                    unit=request.unit,
                    counting_method=overlap.counting_method,
                    source_measurement_paths=tuple(sorted((source.path, target.path))),
                )
            )

        assumptions: list[str] = []
        if multi_scope and disjoint_contract:
            assumptions.append(
                "partial overlap measurements are explicitly documented as disjoint"
            )
        uncertainty = any(
            item.measurement.uncertainty_note.status != "not_applicable"
            for item in counted.values()
        )
        if uncertainty:
            assumptions.append("one or more counted measurements are uncertain")

        method_incompatible = (
            len(
                {
                    _normalize_method(item.measurement.counting_method)
                    for item in counted.values()
                }
            )
            > 1
        )
        not_computable = method_incompatible or bool(unresolved)
        subtotal = sum(
            (Decimal(item.measurement.value) for item in counted.values()),
            Decimal(),
        ) - sum(
            (adjustment.subtracted_value for adjustment in adjustments),
            Decimal(),
        )
        if subtotal < 0:
            relationship_id = (
                adjustments[-1].relationship_id if adjustments else "rel-unknown"
            )
            raise InvalidOverlapError(
                relationship_id, "adjusted total would be negative"
            )
        total = None if not_computable else subtotal
        exactness = (
            "not_computable"
            if not_computable
            else "non_exact"
            if uncertainty
            else "exact"
        )
        if total is not None and exactness == "non_exact":
            assumptions.append("total is a transparent uncertain subtotal")

        return self._summary(
            request=request,
            selected=selected,
            counted=counted.values(),
            exclusions=exclusions,
            adjustments=adjustments,
            assumptions=assumptions,
            unresolved=unresolved,
            total=total,
            exactness=exactness,
            counting_method=result_method,
        )

    @staticmethod
    def _summary(
        *,
        request: SizeSummaryRequest,
        selected: Iterable[_LocatedMeasurement],
        counted: Iterable[_LocatedMeasurement],
        exclusions: Iterable[SizeExclusion],
        adjustments: Iterable[SizeAdjustment],
        assumptions: Iterable[str],
        unresolved: Iterable[UnresolvedOverlap],
        total: Decimal | None,
        exactness: Exactness,
        counting_method: str | None,
    ) -> SizeSummary:
        return SizeSummary(
            request=request,
            selected_measurements=tuple(
                _measurement_ref(item)
                for item in sorted(selected, key=lambda x: x.path)
            ),
            counted_measurement_paths=tuple(sorted(item.path for item in counted)),
            exclusions=tuple(
                sorted(
                    exclusions,
                    key=lambda item: (
                        item.measurement_path,
                        item.reason,
                        item.relationship_ids,
                    ),
                )
            ),
            adjustments=tuple(
                sorted(
                    adjustments,
                    key=lambda item: (
                        item.relationship_id,
                        item.overlap_measurement_path,
                    ),
                )
            ),
            assumptions=tuple(sorted(set(assumptions))),
            unresolved_overlaps=tuple(
                sorted(
                    unresolved,
                    key=lambda item: (item.relationship_id, item.reason),
                )
            ),
            total=total,
            exactness=exactness,
            unit=request.unit,
            counting_method=counting_method,
        )


__all__ = [
    "AmbiguousMeasurementError",
    "CatalogQuery",
    "HistGermQueryError",
    "IncompatibleMeasurementError",
    "InvalidOverlapError",
    "InvalidQueryError",
    "MeasurementRef",
    "RelationshipCycleError",
    "RelationshipPath",
    "RelationshipRequest",
    "RelationshipResult",
    "RelationshipSnapshot",
    "SizeAdjustment",
    "SizeExclusion",
    "SizeSummary",
    "SizeSummaryRequest",
    "UnresolvedOverlap",
]
