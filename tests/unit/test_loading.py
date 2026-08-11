from __future__ import annotations

import json
from pathlib import Path

import pytest

from histgerm.loading import (
    DuplicateKeyError,
    InvalidMappingKeyError,
    InventoryDiscoveryError,
    InventoryEncodingError,
    UnsafeYamlError,
    discover_inventory_files,
    load_bundled_catalog,
    load_catalog,
    load_yaml_bytes,
)
from histgerm.serialization import canonical_json_bytes

FIXTURES = Path(__file__).parents[1] / "fixtures" / "loading"


def test_discovery_and_composition_are_deterministic_and_cwd_independent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    inventory = FIXTURES / "safe"
    first_files = discover_inventory_files(inventory)
    assert [path.relative_to(inventory).as_posix() for path in first_files] == [
        "catalog.yaml",
        "vocabularies/example.yaml",
    ]

    first, first_sources = load_catalog(inventory)
    monkeypatch.chdir(tmp_path)
    second, second_sources = load_catalog(inventory)

    assert first == second
    assert first_sources == second_sources == {}
    assert first.vocabularies.root["example_terms"].ids == {"alpha", "beta"}
    assert canonical_json_bytes(first) == canonical_json_bytes(second)


def test_explicit_json_snapshot_loads_without_authoring_discovery(
    tmp_path: Path,
) -> None:
    catalog, _ = load_catalog(FIXTURES / "safe")
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_bytes(canonical_json_bytes(catalog))
    loaded, sources = load_catalog(snapshot)
    assert loaded == catalog
    assert sources == {}


class _Resource:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def joinpath(self, *_parts: str) -> _Resource:
        return self

    def read_bytes(self) -> bytes:
        return self._data


def test_bundled_loading_uses_only_the_package_resource_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog, _ = load_catalog(FIXTURES / "safe")
    called: list[str] = []

    def fake_files(package: str) -> _Resource:
        called.append(package)
        return _Resource(canonical_json_bytes(catalog))

    monkeypatch.setattr("histgerm.loading.resources.files", fake_files)
    assert load_bundled_catalog(package="fixture.package") == catalog
    assert called == ["fixture.package"]

    with pytest.raises(InventoryDiscoveryError):
        load_bundled_catalog(package="fixture.package", resource="../snapshot.json")


@pytest.mark.parametrize(
    ("name", "exception"),
    [
        ("alias.yaml", UnsafeYamlError),
        ("anchor.yaml", UnsafeYamlError),
        ("custom-tag.yaml", UnsafeYamlError),
        ("duplicate-key.yaml", DuplicateKeyError),
        ("merge-key.yaml", UnsafeYamlError),
        ("multi-document.yaml", UnsafeYamlError),
        ("non-string-key.yaml", InvalidMappingKeyError),
        ("python-object.yaml", UnsafeYamlError),
    ],
)
def test_unsafe_yaml_is_rejected_with_path_rich_diagnostics(
    name: str, exception: type[UnsafeYamlError]
) -> None:
    path = FIXTURES / "unsafe" / name
    with pytest.raises(exception) as captured:
        load_yaml_bytes(path.read_bytes(), source_path=f"unsafe/{name}")
    assert captured.value.diagnostic.path == f"unsafe/{name}"
    assert captured.value.diagnostic.code
    assert "unsafe/" in str(captured.value)


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"\xef\xbb\xbfvalue: one\n", "utf8_bom"),
        (b"value: \xff\n", "invalid_utf8"),
    ],
)
def test_bom_and_non_utf8_are_rejected(data: bytes, code: str) -> None:
    with pytest.raises(InventoryEncodingError) as captured:
        load_yaml_bytes(data, source_path="bad.yaml")
    assert captured.value.diagnostic.code == code


def test_json_snapshot_rejects_bom(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({}).encode())
    with pytest.raises(InventoryEncodingError):
        load_catalog(path)
