"""Restricted YAML loading for authored and bundled HistGerm V2 data."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from yaml.nodes import MappingNode, ScalarNode  # type: ignore[import-untyped]
from yaml.tokens import (  # type: ignore[import-untyped]
    AliasToken,
    AnchorToken,
    TagToken,
)

_UTF8_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True, slots=True)
class LoadingDiagnostic:
    """Stable details for one loading failure."""

    code: str
    path: str
    location: str
    message: str


class HistGermLoadingError(OSError):
    """Base class for expected inventory loading failures."""

    def __init__(self, diagnostic: LoadingDiagnostic) -> None:
        """Store the structured diagnostic and initialize the error message."""

        self.diagnostic = diagnostic
        super().__init__(
            f"{diagnostic.path}:{diagnostic.location}: {diagnostic.message}"
        )


class InventoryDiscoveryError(HistGermLoadingError):
    """The requested inventory boundary cannot be discovered safely."""


class InventoryEncodingError(HistGermLoadingError):
    """An inventory document is not permitted UTF-8 text."""


class UnsafeYamlError(HistGermLoadingError):
    """A YAML document uses a prohibited feature."""


class DuplicateKeyError(UnsafeYamlError):
    """A YAML mapping contains a duplicate key."""


class InvalidMappingKeyError(UnsafeYamlError):
    """A YAML mapping key is not a valid JSON object key."""


class InventoryCompositionError(HistGermLoadingError):
    """Safe documents cannot be composed into one catalog."""


class InventoryModelError(HistGermLoadingError):
    """A safely parsed document does not satisfy its Pydantic model."""


def _location(mark: yaml.error.Mark | None) -> str:
    """Return a stable one-based line and column for a YAML mark."""

    if mark is None:
        return ""
    return f"{mark.line + 1}:{mark.column + 1}"


def _diagnostic(
    code: str,
    path: str,
    message: str,
    mark: yaml.error.Mark | None = None,
) -> LoadingDiagnostic:
    """Build a loading diagnostic from a YAML parser failure."""

    return LoadingDiagnostic(
        code=code,
        path=path,
        location=_location(mark),
        message=message,
    )


class _RestrictedLoader(yaml.SafeLoader):  # type: ignore[misc]
    """PyYAML safe loader that rejects unsafe mapping constructs."""

    source_path: str

    def construct_mapping(
        self, node: MappingNode, deep: bool = False
    ) -> dict[str, Any]:
        """Construct a mapping while enforcing unique, plain string keys."""

        mapping: dict[str, Any] = {}
        for key_node, value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise UnsafeYamlError(
                    _diagnostic(
                        "merge_key",
                        self.source_path,
                        "YAML merge keys are not permitted",
                        key_node.start_mark,
                    )
                )
            if (
                not isinstance(key_node, ScalarNode)
                or key_node.tag != "tag:yaml.org,2002:str"
            ):
                raise InvalidMappingKeyError(
                    _diagnostic(
                        "invalid_mapping_key",
                        self.source_path,
                        "mapping keys must be strings",
                        key_node.start_mark,
                    )
                )
            key = self.construct_scalar(key_node)
            if (
                not key
                or key != key.strip()
                or any(ord(character) < 0x20 for character in key)
            ):
                raise InvalidMappingKeyError(
                    _diagnostic(
                        "invalid_mapping_key",
                        self.source_path,
                        f"invalid mapping key {key!r}",
                        key_node.start_mark,
                    )
                )
            if key in mapping:
                raise DuplicateKeyError(
                    _diagnostic(
                        "duplicate_key",
                        self.source_path,
                        f"duplicate mapping key {key!r}",
                        key_node.start_mark,
                    )
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _decode_utf8(data: bytes, path: str) -> str:
    """Decode BOM-free UTF-8 bytes or raise a structured loading error."""

    if data.startswith(_UTF8_BOM):
        raise InventoryEncodingError(
            _diagnostic("utf8_bom", path, "UTF-8 BOM is not permitted")
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InventoryEncodingError(
            LoadingDiagnostic(
                code="invalid_utf8",
                path=path,
                location=str(error.start),
                message="document is not valid UTF-8",
            )
        ) from error


def load_yaml_mapping_bytes(
    data: bytes, *, source_path: str = "<memory>"
) -> dict[str, Any]:
    """Load one restricted UTF-8 YAML document whose root is a mapping."""

    text = _decode_utf8(data, source_path)
    try:
        tokens = list(yaml.scan(text, Loader=yaml.SafeLoader))
    except yaml.MarkedYAMLError as error:
        raise UnsafeYamlError(
            _diagnostic(
                "invalid_yaml",
                source_path,
                error.problem or "invalid YAML",
                error.problem_mark,
            )
        ) from error
    for token in tokens:
        if isinstance(token, AnchorToken):
            feature = "anchors"
        elif isinstance(token, AliasToken):
            feature = "aliases"
        elif isinstance(token, TagToken):
            feature = "explicit tags"
        else:
            continue
        raise UnsafeYamlError(
            _diagnostic(
                "unsafe_yaml",
                source_path,
                f"YAML {feature} are not permitted",
                token.start_mark,
            )
        )

    loader = _RestrictedLoader(text)
    loader.source_path = source_path
    try:
        document = loader.get_single_data()
        if document is None:
            raise InventoryCompositionError(
                _diagnostic(
                    "empty_document", source_path, "inventory document is empty"
                )
            )
        if not isinstance(document, Mapping):
            raise InventoryCompositionError(
                _diagnostic(
                    "non_mapping_document",
                    source_path,
                    "YAML document root must be a mapping",
                )
            )
        return dict(document)
    except HistGermLoadingError:
        raise
    except yaml.MarkedYAMLError as error:
        message = error.problem or "invalid or unsafe YAML"
        if "merge" in message.casefold():
            message = "YAML merge keys are not permitted"
        raise UnsafeYamlError(
            _diagnostic("unsafe_yaml", source_path, message, error.problem_mark)
        ) from error
    finally:
        loader.dispose()


def _resource_parts(path: str, *, label: str) -> tuple[str, ...]:
    """Split and validate a relative package-resource path."""

    parts = tuple(path.split("/"))
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise InventoryDiscoveryError(
            _diagnostic(
                "invalid_resource_path",
                label,
                "package resource path must remain within its declared boundary",
            )
        )
    return parts


def _discover_yaml_resources(
    root: Traversable, *, prefix: tuple[str, ...] = ()
) -> list[str]:
    """Recursively discover YAML resources in deterministic path order."""

    discovered: list[str] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        relative = (*prefix, entry.name)
        if entry.is_dir():
            discovered.extend(_discover_yaml_resources(entry, prefix=relative))
        elif entry.is_file() and Path(entry.name).suffix.casefold() in {
            ".yaml",
            ".yml",
        }:
            discovered.append("/".join(relative))
    return discovered


def discover_bundled_yaml(
    *, package: str = "histgerm", directory: str = "data"
) -> tuple[str, ...]:
    """Discover authored YAML paths beneath a package data directory."""

    label = f"{package}:{directory}"
    root = resources.files(package).joinpath(*_resource_parts(directory, label=label))
    if not root.is_dir():
        raise InventoryDiscoveryError(
            _diagnostic(
                "missing_bundled_data",
                label,
                "bundled data directory does not exist",
            )
        )
    return tuple(_discover_yaml_resources(root))


def load_bundled_yaml(
    resource: str,
    *,
    package: str = "histgerm",
    directory: str = "data",
) -> dict[str, Any]:
    """Load one authored YAML mapping from the package data boundary."""

    label = f"{package}:{directory}/{resource}"
    resource_parts = _resource_parts(resource, label=label)
    if Path(resource_parts[-1]).suffix.casefold() not in {".yaml", ".yml"}:
        raise InventoryDiscoveryError(
            _diagnostic(
                "unsupported_resource",
                label,
                "bundled resource must use .yaml or .yml",
            )
        )
    root = resources.files(package).joinpath(
        *_resource_parts(directory, label=f"{package}:{directory}")
    )
    target = root.joinpath(*resource_parts)
    if not target.is_file():
        raise InventoryDiscoveryError(
            _diagnostic(
                "missing_bundled_resource",
                label,
                "bundled YAML resource does not exist",
            )
        )
    return load_yaml_mapping_bytes(target.read_bytes(), source_path=label)


def discover_inventory_files(root: Path) -> tuple[Path, ...]:
    """Return authored YAML files in normalized relative-path order."""

    if root.is_symlink():
        raise InventoryDiscoveryError(
            _diagnostic(
                "symlink_boundary",
                root.as_posix(),
                "inventory boundary may not be a symlink",
            )
        )
    root = root.resolve()
    if not root.exists():
        raise InventoryDiscoveryError(
            _diagnostic(
                "missing_inventory", root.as_posix(), "inventory path does not exist"
            )
        )
    if root.is_file():
        if root.suffix.casefold() not in {".yaml", ".yml"}:
            raise InventoryDiscoveryError(
                _diagnostic(
                    "unsupported_file",
                    root.as_posix(),
                    "data file must use .yaml or .yml",
                )
            )
        return (root,)

    discovered: list[Path] = []
    for directory, directory_names, file_names in os.walk(
        root, topdown=True, followlinks=False
    ):
        directory_path = Path(directory)
        directory_names[:] = sorted(
            name for name in directory_names if not (directory_path / name).is_symlink()
        )
        for name in file_names:
            path = directory_path / name
            if path.suffix.casefold() in {".yaml", ".yml"} and not path.is_symlink():
                discovered.append(path)
    return tuple(sorted(discovered, key=lambda path: path.relative_to(root).as_posix()))


__all__ = [
    "DuplicateKeyError",
    "HistGermLoadingError",
    "InvalidMappingKeyError",
    "InventoryCompositionError",
    "InventoryDiscoveryError",
    "InventoryEncodingError",
    "InventoryModelError",
    "LoadingDiagnostic",
    "UnsafeYamlError",
    "discover_bundled_yaml",
    "discover_inventory_files",
    "load_bundled_yaml",
    "load_yaml_mapping_bytes",
]
