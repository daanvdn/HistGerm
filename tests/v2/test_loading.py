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


def test_synthetic_corpus_is_discoverable_and_loads_as_mapping() -> None:
    resources = discover_bundled_yaml()

    assert resources == (
        "corpora/rem.yaml",
        "corpora/synthetic-corpus.yaml",
        "dictionaries/mwb.yaml",
        "tools/rnntagger.yaml",
    )
    payload = load_bundled_yaml(resources[1])
    assert payload["id"] == "corpus-synthetic-demo"
    assert payload["versions"][0]["texts"][1]["title"] == (
        "Synthetic Later Sermon Witness"
    )


@pytest.mark.parametrize(
    "data",
    [
        b"---\nid: first\n---\nid: second\n",
        b"id: [unterminated\n",
        b"id: first\nid: second\n",
        b"defaults: &defaults\n  value: one\ncopy: *defaults\n",
        b"tagged: !custom value\n",
    ],
)
def test_restricted_yaml_rejects_malformed_or_unsafe_documents(
    data: bytes,
) -> None:
    with pytest.raises(UnsafeYamlError):
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


def test_source_checkout_resource_is_a_real_file() -> None:
    data_home = Path(__file__).parents[2] / "src" / "histgerm" / "data"
    assert (data_home / "corpora" / "synthetic-corpus.yaml").is_file()
