from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from histgerm.packaging import load_verified_bundled_catalog
from histgerm.query import (
    CatalogQuery,
    ManifestExportOptions,
    ManifestSelection,
    ManifestSelectionError,
    QueryFilter,
    ReviewRequiredError,
    TrainingDataManifest,
    manifest_json,
)


def selection(**ids: tuple[str, ...]) -> ManifestSelection:
    return ManifestSelection(filters=QueryFilter(), **ids)


def test_digest_is_stable_across_creation_timestamps_and_round_trips() -> None:
    query = CatalogQuery(catalog=load_verified_bundled_catalog())
    options = ManifestExportOptions(include_review_required=True)
    first = query.export_manifest(
        selection(distribution_ids=("dist-rem-2-1-zenodo",)),
        options=options,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    second = query.export_manifest(
        selection(distribution_ids=("dist-rem-2-1-zenodo",)),
        options=options,
        created_at=datetime(2026, 8, 11, tzinfo=UTC),
    )

    assert first.created_at != second.created_at
    assert first.semantic_digest == second.semantic_digest
    encoded = manifest_json(first)
    assert encoded.endswith(b"\n")
    assert TrainingDataManifest.model_validate_json(encoded) == first


def test_selection_change_changes_digest() -> None:
    query = CatalogQuery(catalog=load_verified_bundled_catalog())
    options = ManifestExportOptions(include_review_required=True)
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)

    first = query.export_manifest(
        selection(distribution_ids=("dist-rem-2-1-zenodo",)),
        options=options,
        created_at=timestamp,
    )
    second = query.export_manifest(
        selection(distribution_ids=("dist-mwb-online",)),
        options=options,
        created_at=timestamp,
    )

    assert first.semantic_digest != second.semantic_digest


def test_missing_stable_identity_fails_actionably() -> None:
    catalog = load_verified_bundled_catalog()
    resource = catalog.resources[0]
    unstable_version = resource.versions[0].model_construct(
        **{**resource.versions[0].__dict__, "id": ""}
    )
    unstable_resource = resource.model_copy(update={"versions": [unstable_version]})
    unstable_catalog = catalog.model_copy(
        update={"resources": [unstable_resource, *catalog.resources[1:]]}
    )

    with pytest.raises(ManifestSelectionError, match="stable resource or version ID"):
        CatalogQuery(catalog=unstable_catalog).export_manifest(
            selection(resource_ids=(resource.id,)),
            options=ManifestExportOptions(include_review_required=True),
        )

    version = resource.versions[0]
    distribution = version.distributions[0]
    unstable_distribution = distribution.model_construct(
        **{**distribution.__dict__, "id": ""}
    )
    changed_version = version.model_copy(
        update={"distributions": [unstable_distribution, *version.distributions[1:]]}
    )
    changed_resource = resource.model_copy(update={"versions": [changed_version]})
    changed_catalog = catalog.model_copy(
        update={"resources": [changed_resource, *catalog.resources[1:]]}
    )
    with pytest.raises(ManifestSelectionError, match="without a stable ID"):
        CatalogQuery(catalog=changed_catalog).export_manifest(
            selection(resource_ids=(resource.id,)),
            options=ManifestExportOptions(include_review_required=True),
        )


def test_unclear_permission_requires_opt_in_and_emits_warning() -> None:
    query = CatalogQuery(catalog=load_verified_bundled_catalog())
    selected = selection(distribution_ids=("dist-rem-2-1-zenodo",))

    with pytest.raises(ReviewRequiredError, match="include_review_required=True"):
        query.export_manifest(selected)

    manifest = query.export_manifest(
        selected,
        options=ManifestExportOptions(include_review_required=True),
    )
    assert "REVIEW_REQUIRED_MODEL_TRAINING_PERMISSION" in manifest.warnings
    assert manifest.entries[0].model_training == "unclear"


def test_prohibited_permission_is_always_rejected() -> None:
    catalog = load_verified_bundled_catalog()
    resource = next(item for item in catalog.resources if item.id == "res-rem")
    version = resource.versions[0]
    distribution = version.distributions[0]
    access = distribution.access.model_copy(update={"model_training": "prohibited"})
    prohibited = distribution.model_copy(update={"access": access})
    changed_version = version.model_copy(update={"distributions": [prohibited]})
    changed_resource = resource.model_copy(update={"versions": [changed_version]})
    changed_catalog = catalog.model_copy(
        update={
            "resources": [
                changed_resource if item.id == resource.id else item
                for item in catalog.resources
            ]
        }
    )

    with pytest.raises(ManifestSelectionError, match="explicitly prohibits"):
        CatalogQuery(catalog=changed_catalog).export_manifest(
            selection(distribution_ids=(distribution.id,)),
            options=ManifestExportOptions(include_review_required=True),
        )


def test_manifest_is_external_only_and_deterministically_ordered() -> None:
    query = CatalogQuery(catalog=load_verified_bundled_catalog())
    manifest = query.export_manifest(
        selection(),
        options=ManifestExportOptions(include_review_required=True),
    )
    document = json.loads(manifest_json(manifest))
    encoded = json.dumps(document)

    entry_keys = [
        (entry["resource_id"], entry["version_id"], entry["distribution_id"])
        for entry in document["entries"]
    ]
    assert entry_keys == sorted(entry_keys)
    assert all(
        reference.startswith(("https://", "http://"))
        for entry in document["entries"]
        for reference in entry["external_references"]
    )
    for forbidden in ("file:", "data:", "local_path", '"payload"', '"content"'):
        assert forbidden not in encoded


def test_identical_manifest_requests_produce_identical_canonical_bytes() -> None:
    query = CatalogQuery(catalog=load_verified_bundled_catalog())
    selected = selection(distribution_ids=("dist-rem-2-1-zenodo",))
    options = ManifestExportOptions(include_review_required=True)
    timestamp = datetime(2026, 8, 11, 12, 30, tzinfo=UTC)

    first = query.export_manifest(selected, options=options, created_at=timestamp)
    second = query.export_manifest(selected, options=options, created_at=timestamp)

    assert first == second
    assert manifest_json(first) == manifest_json(second)
