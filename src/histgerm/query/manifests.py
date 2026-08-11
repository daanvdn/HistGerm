"""Deterministic, external-reference-only training-data manifests."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import field_serializer, field_validator

from histgerm.models.access import AvailabilityState, PermissionState
from histgerm.models.common import HistGermModel, KnownValue, StableId
from histgerm.models.resource import Resource, ResourceVersion
from histgerm.query.catalog import CatalogQuery as _CatalogQuery
from histgerm.query.catalog import _matches
from histgerm.query.filters import QueryFilter
from histgerm.serialization import canonical_json_bytes

MANIFEST_SCHEMA_VERSION = "1.0"
REVIEW_REQUIRED_WARNING = "REVIEW_REQUIRED_MODEL_TRAINING_PERMISSION"
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "authorization",
        "credential",
        "key",
        "password",
        "signature",
        "sig",
        "token",
        "x-amz-credential",
        "x-amz-signature",
    }
)


class ManifestSelectionError(ValueError):
    """The requested selection cannot be represented reproducibly."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"manifest selection is not reproducible: {reason}")


class ReviewRequiredError(ValueError):
    """Unclear permission requires an explicit caller decision."""

    def __init__(self, distribution_ids: tuple[str, ...]) -> None:
        super().__init__(
            "selection contains unclear model-training permission: "
            + ", ".join(distribution_ids)
            + "; pass include_review_required=True to include it"
        )


class ExternalReferenceError(ValueError):
    """A selected distribution has no safe external reference."""

    def __init__(self, entry_id: str) -> None:
        super().__init__(
            f"manifest entry {entry_id!r} has no permitted external reference"
        )


class ManifestSelection(HistGermModel):
    """Exact immutable filters and identifiers requested by the caller."""

    filters: QueryFilter
    resource_ids: tuple[StableId, ...] = ()
    version_ids: tuple[StableId, ...] = ()
    distribution_ids: tuple[StableId, ...] = ()
    component_ids: tuple[StableId, ...] = ()
    document_ids: tuple[StableId, ...] = ()
    annotation_ids: tuple[StableId, ...] = ()

    @field_validator(
        "resource_ids",
        "version_ids",
        "distribution_ids",
        "component_ids",
        "document_ids",
        "annotation_ids",
        mode="before",
    )
    @classmethod
    def canonicalize_ids(cls, value: object) -> object:
        if isinstance(value, (set, frozenset, list, tuple)):
            return tuple(sorted(set(value)))
        return value


class ManifestExportOptions(HistGermModel):
    include_review_required: bool = False


class ManifestEntry(HistGermModel):
    resource_id: StableId
    version_id: StableId
    distribution_id: StableId
    component_ids: tuple[StableId, ...]
    document_ids: tuple[StableId, ...]
    annotation_ids: tuple[StableId, ...]
    external_references: tuple[str, ...]
    availability: AvailabilityState
    automated_access: PermissionState
    model_training: PermissionState
    original_redistribution: PermissionState
    processed_redistribution: PermissionState
    trained_weights_publication: PermissionState
    evidence_ids: tuple[StableId, ...]
    warnings: tuple[str, ...]


class TrainingDataManifest(HistGermModel):
    manifest_schema_version: str
    histgerm_schema_version: str
    histgerm_package_version: str
    inventory_revision: str
    created_at: datetime
    selection: ManifestSelection
    entries: tuple[ManifestEntry, ...]
    provenance_evidence_ids: tuple[StableId, ...]
    warnings: tuple[str, ...]
    semantic_digest_algorithm: Literal["sha256"]
    semantic_digest: str

    @field_serializer("created_at", when_used="json")
    def serialize_created_at(self, value: datetime) -> str:
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _package_version() -> str:
    try:
        return metadata.version("histgerm")
    except metadata.PackageNotFoundError as error:
        raise ManifestSelectionError(
            "the histgerm package version is unavailable"
        ) from error


def _git_sha() -> str | None:
    package_root = Path(__file__).resolve().parents[3]
    try:
        result = subprocess.run(
            ["git", "-C", str(package_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = result.stdout.strip().lower()
    if len(sha) == 40 and all(character in "0123456789abcdef" for character in sha):
        return sha
    return None


def _inventory_revision(catalog: Any) -> str:
    snapshot_digest = hashlib.sha256(canonical_json_bytes(catalog)).hexdigest()
    git_sha = _git_sha()
    identity = (
        f"git:{git_sha}"
        if git_sha is not None
        else f"release:{catalog.inventory_release}"
    )
    return f"{identity};snapshot:sha256:{snapshot_digest}"


def _external_references(distribution: Any) -> tuple[str, ...]:
    if not isinstance(distribution.access_urls, KnownValue):
        return ()
    accepted: set[str] = set()
    for item in distribution.access_urls.value:
        url = str(item.url)
        parsed = urlsplit(url)
        query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query)}
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or query_keys & _SENSITIVE_QUERY_KEYS
        ):
            continue
        accepted.add(url)
    return tuple(sorted(accepted))


def _evidence_ids(
    resource: Resource,
    version_index: int,
    distribution_index: int,
    component_ids: tuple[str, ...],
    document_ids: tuple[str, ...],
    annotation_ids: tuple[str, ...],
) -> tuple[StableId, ...]:
    prefixes = {
        f"/versions/{version_index}/distributions/{distribution_index}",
    }
    version = resource.versions[version_index]
    for collection, identifiers in (
        ("components", component_ids),
        ("documents", document_ids),
        ("annotations", annotation_ids),
    ):
        positions = {
            item.id: index for index, item in enumerate(getattr(version, collection))
        }
        prefixes.update(
            f"/versions/{version_index}/{collection}/{positions[identifier]}"
            for identifier in identifiers
        )
    return tuple(
        sorted(
            {
                evidence_id
                for pointer, references in resource.claims.items()
                if any(
                    pointer == prefix or pointer.startswith(f"{prefix}/")
                    for prefix in prefixes
                )
                for evidence_id in references
            }
        )
    )


def _scope_ids(
    requested: tuple[str, ...],
    available: set[str],
    declared: frozenset[str],
    *,
    label: str,
    distribution_id: str,
) -> tuple[str, ...]:
    unknown = sorted(set(requested) - available)
    if unknown:
        raise ManifestSelectionError(
            f"{label} IDs do not resolve in the selected version: {', '.join(unknown)}"
        )
    selected = set(requested) if requested else (set(declared) or available)
    outside = sorted(selected - set(declared)) if declared else []
    if outside:
        raise ManifestSelectionError(
            f"{label} IDs are outside distribution {distribution_id!r} scope: "
            + ", ".join(outside)
        )
    return tuple(sorted(selected))


def _version_matches(
    query: _CatalogQuery,
    resource: Resource,
    version: Any,
    filters: QueryFilter,
) -> bool:
    if filters.language_stages and not _matches(
        query._resource_stages(resource, version), filters.language_stages
    ):
        return False
    if filters.annotation_types and not any(
        _matches(frozenset({annotation.task}), filters.annotation_types)
        for annotation in version.annotations
    ):
        return False
    return not filters.annotation_qualities or any(
        _matches(frozenset({annotation.quality}), filters.annotation_qualities)
        for annotation in version.annotations
    )


def _entry_warnings(
    catalog: Any, resource_id: str, distribution: Any
) -> tuple[str, ...]:
    warnings: set[str] = set()
    requirement = distribution.access.authentication_or_agreement
    if requirement not in {"none", "not_applicable"}:
        warnings.add(f"ACCESS_REQUIREMENT:{requirement}")
    for relationship in catalog.relationships:
        if resource_id not in {relationship.source.id, relationship.target.id}:
            continue
        extent = relationship.overlap_extent
        if extent in {"partial", "unknown"}:
            warnings.add(f"UNRESOLVED_OVERLAP:{relationship.id}:{extent}")
    if distribution.access.model_training == "unclear":
        warnings.add(REVIEW_REQUIRED_WARNING)
    return tuple(sorted(warnings))


def _semantic_digest(document: dict[str, Any]) -> str:
    semantic = {
        key: value
        for key, value in document.items()
        if key not in {"created_at", "semantic_digest"}
    }
    return hashlib.sha256(canonical_json_bytes(semantic)).hexdigest()


def manifest_json(manifest: TrainingDataManifest) -> bytes:
    """Serialize a manifest as canonical JSON and verify its semantic digest."""

    document = manifest.model_dump(mode="json")
    expected = _semantic_digest(document)
    if manifest.semantic_digest != expected:
        raise ManifestSelectionError("semantic digest does not match manifest content")
    return canonical_json_bytes(document)


class CatalogQuery(_CatalogQuery):
    """Catalog query API extended with reproducible manifest export."""

    def export_manifest(
        self,
        selection: ManifestSelection,
        *,
        options: ManifestExportOptions | None = None,
        created_at: datetime | None = None,
    ) -> TrainingDataManifest:
        options = options or ManifestExportOptions()
        allowed_resources = {
            match.resource_id for match in self.resources(selection.filters)
        }
        entries: list[ManifestEntry] = []
        unclear: list[str] = []
        found: dict[str, set[str]] = {
            "resource": set(),
            "version": set(),
            "distribution": set(),
        }
        for resource in self.catalog.resources:
            if resource.id not in allowed_resources:
                continue
            if selection.resource_ids and resource.id not in selection.resource_ids:
                continue
            found["resource"].add(resource.id)
            for version_index, version in enumerate(resource.versions):
                if selection.version_ids and version.id not in selection.version_ids:
                    continue
                if not _version_matches(self, resource, version, selection.filters):
                    continue
                found["version"].add(version.id)
                self._append_version_entries(
                    entries,
                    unclear,
                    found["distribution"],
                    resource,
                    version,
                    version_index,
                    selection,
                )
        requested = {
            "resource": set(selection.resource_ids),
            "version": set(selection.version_ids),
            "distribution": set(selection.distribution_ids),
        }
        for label in ("resource", "version", "distribution"):
            missing = sorted(requested[label] - found[label])
            if missing:
                raise ManifestSelectionError(
                    f"requested {label} IDs did not resolve with all filters: "
                    + ", ".join(missing)
                )
        if unclear and not options.include_review_required:
            raise ReviewRequiredError(tuple(sorted(unclear)))
        timestamp = created_at or datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ManifestSelectionError("created_at must be timezone-aware")
        timestamp = timestamp.astimezone(UTC).replace(microsecond=0)
        ordered_entries = tuple(
            sorted(
                entries,
                key=lambda entry: (
                    entry.resource_id,
                    entry.version_id,
                    entry.distribution_id,
                ),
            )
        )
        warnings = tuple(
            sorted({warning for entry in ordered_entries for warning in entry.warnings})
        )
        evidence_ids = tuple(
            sorted(
                {
                    evidence_id
                    for entry in ordered_entries
                    for evidence_id in entry.evidence_ids
                }
            )
        )
        values: dict[str, Any] = {
            "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
            "histgerm_schema_version": str(self.catalog.schema_version),
            "histgerm_package_version": _package_version(),
            "inventory_revision": _inventory_revision(self.catalog),
            "created_at": timestamp,
            "selection": selection,
            "entries": ordered_entries,
            "provenance_evidence_ids": evidence_ids,
            "warnings": warnings,
            "semantic_digest_algorithm": "sha256",
            "semantic_digest": "",
        }
        provisional = TrainingDataManifest(**values)
        values["semantic_digest"] = _semantic_digest(
            provisional.model_dump(mode="json")
        )
        return TrainingDataManifest(**values)

    def _append_version_entries(
        self,
        entries: list[ManifestEntry],
        unclear: list[str],
        found_distributions: set[str],
        resource: Resource,
        version: ResourceVersion,
        version_index: int,
        selection: ManifestSelection,
    ) -> None:
        if not resource.id or not version.id:
            raise ManifestSelectionError(
                "selected resource/version is missing a stable resource or version ID"
            )
        components = {item.id for item in version.components}
        documents = {item.id for item in version.documents}
        annotations = {item.id for item in version.annotations}
        for distribution_index, distribution in enumerate(version.distributions):
            if selection.distribution_ids and (
                distribution.id not in selection.distribution_ids
            ):
                continue
            if not self._distribution_satisfies(distribution, selection.filters):
                continue
            if not distribution.id:
                raise ManifestSelectionError(
                    f"version {version.id!r} has a distribution without a stable ID"
                )
            found_distributions.add(distribution.id)
            permission = distribution.access.model_training
            if permission == "prohibited":
                raise ManifestSelectionError(
                    f"distribution {distribution.id!r} explicitly prohibits "
                    "model training"
                )
            if permission == "not_applicable":
                raise ManifestSelectionError(
                    f"distribution {distribution.id!r} has model-training permission "
                    "marked not_applicable"
                )
            if permission == "unclear":
                unclear.append(distribution.id)
            component_ids = _scope_ids(
                selection.component_ids,
                components,
                distribution.scope.component_ids,
                label="component",
                distribution_id=distribution.id,
            )
            document_ids = _scope_ids(
                selection.document_ids,
                documents,
                distribution.scope.document_ids,
                label="document",
                distribution_id=distribution.id,
            )
            annotation_ids = _scope_ids(
                selection.annotation_ids,
                annotations,
                distribution.scope.annotation_ids,
                label="annotation",
                distribution_id=distribution.id,
            )
            references = _external_references(distribution)
            if not references:
                raise ExternalReferenceError(distribution.id)
            warnings = _entry_warnings(self.catalog, resource.id, distribution)
            evidence_ids = _evidence_ids(
                resource,
                version_index,
                distribution_index,
                component_ids,
                document_ids,
                annotation_ids,
            )
            entries.append(
                ManifestEntry(
                    resource_id=resource.id,
                    version_id=version.id,
                    distribution_id=distribution.id,
                    component_ids=component_ids,
                    document_ids=document_ids,
                    annotation_ids=annotation_ids,
                    external_references=references,
                    availability=distribution.availability,
                    automated_access=distribution.access.automated_access,
                    model_training=permission,
                    original_redistribution=(
                        distribution.access.original_redistribution
                    ),
                    processed_redistribution=(
                        distribution.access.processed_redistribution
                    ),
                    trained_weights_publication=(
                        distribution.access.trained_weights_publication
                    ),
                    evidence_ids=evidence_ids,
                    warnings=warnings,
                )
            )


__all__ = [
    "CatalogQuery",
    "ExternalReferenceError",
    "MANIFEST_SCHEMA_VERSION",
    "ManifestEntry",
    "ManifestExportOptions",
    "ManifestSelection",
    "ManifestSelectionError",
    "REVIEW_REQUIRED_WARNING",
    "ReviewRequiredError",
    "TrainingDataManifest",
    "manifest_json",
]
