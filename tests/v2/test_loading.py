from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from histgerm.loading import (
    InventoryCompositionError,
    InventoryDiscoveryError,
    InventoryEncodingError,
    UnsafeYamlError,
    discover_bundled_yaml,
    load_bundled_yaml,
    load_yaml_mapping_bytes,
)


def test_exactly_three_authored_yaml_records_are_discoverable() -> None:
    resources = discover_bundled_yaml()

    assert resources == (
        "corpora/rem.yaml",
        "dictionaries/mwb.yaml",
        "tools/rnntagger.yaml",
    )
    assert [load_bundled_yaml(path)["id"] for path in resources] == [
        "res-rem",
        "res-mwb",
        "res-rnntagger",
    ]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"---\nid: first\n---\nid: second\n", "another document"),
        (b"id: [unterminated\n", "expected"),
        (b"id: first\nid: second\n", "duplicate mapping key"),
        (b"defaults: &defaults\n  value: one\n", "anchors"),
        (b"copy: *defaults\n", "aliases"),
        (b"tagged: !custom value\n", "explicit tags"),
        (b"item:\n  <<: {value: one}\n", "merge keys"),
        (b"? [list]\n: value\n", "mapping keys must be strings"),
    ],
)
def test_restricted_yaml_rejects_unsafe_structure(data: bytes, message: str) -> None:
    with pytest.raises(UnsafeYamlError, match=message):
        load_yaml_mapping_bytes(data, source_path="fixture.yaml")


@pytest.mark.parametrize("data", [b"- item\n", b"plain scalar\n", b"null\n"])
def test_yaml_document_root_must_be_a_nonempty_mapping(data: bytes) -> None:
    with pytest.raises(InventoryCompositionError):
        load_yaml_mapping_bytes(data, source_path="fixture.yaml")


@pytest.mark.parametrize("data", [b"\xef\xbb\xbfid: example\n", b"id: \xff\n"])
def test_yaml_is_restricted_to_bomless_utf8(data: bytes) -> None:
    with pytest.raises(InventoryEncodingError):
        load_yaml_mapping_bytes(data, source_path="fixture.yaml")


class _Traversable:
    def __init__(
        self,
        name: str,
        *,
        data: bytes | None = None,
        children: tuple[_Traversable, ...] = (),
    ) -> None:
        self.name = name
        self._data = data
        self._children = children

    def iterdir(self) -> Any:
        return iter(self._children)

    def is_dir(self) -> bool:
        return self._data is None

    def is_file(self) -> bool:
        return self._data is not None

    def joinpath(self, *descendants: str) -> _Traversable:
        current = self
        for descendant in descendants:
            current = next(
                child for child in current._children if child.name == descendant
            )
        return current

    def read_bytes(self) -> bytes:
        if self._data is None:
            raise IsADirectoryError(self.name)
        return self._data


def test_discovery_and_loading_use_package_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = _Traversable("example.yaml", data=b"id: package-example\n")
    ignored = _Traversable("notes.txt", data=b"not YAML")
    data = _Traversable(
        "data",
        children=(_Traversable("tools", children=(ignored, corpus)),),
    )
    package = _Traversable("fixture", children=(data,))
    called: list[str] = []

    def fake_files(name: str) -> _Traversable:
        called.append(name)
        return package

    monkeypatch.setattr("histgerm.loading.resources.files", fake_files)
    assert discover_bundled_yaml(package="fixture") == ("tools/example.yaml",)
    assert load_bundled_yaml("tools/example.yaml", package="fixture") == {
        "id": "package-example"
    }
    assert called == ["fixture", "fixture"]


@pytest.mark.parametrize(
    "resource", ["../escape.yaml", "/absolute.yaml", "record.json"]
)
def test_bundled_resource_paths_stay_within_data_boundary(resource: str) -> None:
    with pytest.raises(InventoryDiscoveryError):
        load_bundled_yaml(resource)


def test_source_checkout_contains_only_three_authored_yaml_files() -> None:
    data_home = Path(__file__).parents[2] / "src" / "histgerm" / "data"
    paths = sorted(
        path.relative_to(data_home).as_posix() for path in data_home.rglob("*")
    )
    assert [path for path in paths if path.endswith((".yaml", ".yml"))] == [
        "corpora/rem.yaml",
        "dictionaries/mwb.yaml",
        "tools/rnntagger.yaml",
    ]
