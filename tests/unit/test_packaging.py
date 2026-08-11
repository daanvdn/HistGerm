from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from histgerm.packaging import (
    InventoryManifestError,
    InventoryValidationError,
    build_inventory,
    check_inventory,
    generate_inventory,
    load_verified_bundled_catalog,
    verify_generated_inventory,
)

ROOT = Path(__file__).parents[2]
INVENTORY = ROOT / "inventory"
BUNDLED = ROOT / "src" / "histgerm" / "resources" / "inventory"


def test_generation_is_byte_stable_and_manifest_matches_source() -> None:
    first = generate_inventory(INVENTORY)
    second = generate_inventory(INVENTORY)

    assert first.snapshot == second.snapshot
    assert first.manifest == second.manifest
    manifest = json.loads(first.manifest)
    assert manifest["snapshot_sha256"]
    assert manifest["record_counts"]["total"] == 5
    assert [item["path"] for item in manifest["authoring_files"]] == sorted(
        item["path"] for item in manifest["authoring_files"]
    )
    assert verify_generated_inventory(first.snapshot, first.manifest) == first.catalog


def test_build_and_drift_check_detect_missing_extra_and_changed_files(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "generated"
    build_inventory(INVENTORY, destination)
    assert check_inventory(INVENTORY, destination).is_current

    (destination / "snapshot.json").write_bytes(b"{}\n")
    (destination / "extra.json").write_bytes(b"{}\n")
    (destination / "manifest.json").unlink()
    drift = check_inventory(INVENTORY, destination)
    assert drift.changes == (
        "missing:manifest.json",
        "extra:extra.json",
        "changed:snapshot.json",
    )


def test_invalid_authoring_source_refuses_to_replace_previous_snapshot(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "generated"
    destination.mkdir()
    previous = b'{"previous":true}\n'
    (destination / "snapshot.json").write_bytes(previous)
    invalid = ROOT / "tests" / "fixtures" / "loading" / "safe"

    with pytest.raises(InventoryValidationError, match="validation failed"):
        build_inventory(invalid, destination)

    assert (destination / "snapshot.json").read_bytes() == previous


def test_bundled_catalog_loads_without_current_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    catalog = load_verified_bundled_catalog()
    assert catalog.inventory_release == "2026.08.11.1"
    assert len(catalog.resources) == 3


@pytest.mark.parametrize(
    "forbidden",
    [
        "data:text/plain;base64,SGVsbG8=",
        "file:///home/user/private.bin",
        "C:\\Users\\user\\private.bin",
        "A" * 300,
    ],
)
def test_payload_policy_refuses_embedded_data_and_local_paths(
    tmp_path: Path, forbidden: str
) -> None:
    source = tmp_path / "inventory"
    shutil.copytree(INVENTORY, source)
    catalog = source / "catalog.yaml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8").replace(
            "extensions: {}\n",
            "extensions:\n  example.org:\n    artifact_reference: "
            + json.dumps(forbidden)
            + "\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(InventoryValidationError, match="forbidden"):
        generate_inventory(source)


def test_manifest_digest_tampering_is_rejected() -> None:
    generated = generate_inventory(INVENTORY)
    with pytest.raises(InventoryManifestError, match="SHA-256"):
        verify_generated_inventory(b"{}\n", generated.manifest)
