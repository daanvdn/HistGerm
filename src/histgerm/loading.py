"""Safe, deterministic loading for HistGerm authoring data and snapshots."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import TypeAdapter, ValidationError
from yaml.nodes import MappingNode, ScalarNode  # type: ignore[import-untyped]
from yaml.tokens import (  # type: ignore[import-untyped]
    AliasToken,
    AnchorToken,
    TagToken,
)

from histgerm.models.catalog import (
    Catalog,
    InventoryRecord,
    OpenRegistryDefinition,
)

_INVENTORY_ADAPTER: TypeAdapter[InventoryRecord] = TypeAdapter(InventoryRecord)
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
    if mark is None:
        return ""
    return f"{mark.line + 1}:{mark.column + 1}"


def _diagnostic(
    code: str,
    path: str,
    message: str,
    mark: yaml.error.Mark | None = None,
) -> LoadingDiagnostic:
    return LoadingDiagnostic(
        code=code,
        path=path,
        location=_location(mark),
        message=message,
    )


class _RestrictedLoader(yaml.SafeLoader):  # type: ignore[misc]
    source_path: str

    def construct_mapping(
        self, node: MappingNode, deep: bool = False
    ) -> dict[str, Any]:
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


def load_yaml_bytes(data: bytes, *, source_path: str = "<memory>") -> Any:
    """Load one restricted YAML document from UTF-8 bytes."""

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
        return document
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


def load_yaml_file(path: Path) -> Any:
    """Load one restricted YAML file."""

    return load_yaml_bytes(path.read_bytes(), source_path=path.as_posix())


def discover_inventory_files(root: Path) -> tuple[Path, ...]:
    """Return authoring YAML files in normalized relative-path order."""

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
                    "authoring file must use .yaml or .yml",
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


def _model_error(path: str, error: ValidationError) -> InventoryModelError:
    first = error.errors(include_url=False)[0]
    location = "/".join(str(part) for part in first["loc"])
    return InventoryModelError(
        LoadingDiagnostic(
            code="model_validation",
            path=path,
            location=location,
            message=str(first["msg"]),
        )
    )


def _compose_authoring(root: Path) -> tuple[Catalog, dict[str, str]]:
    files = discover_inventory_files(root)
    boundary = root.resolve() if root.is_dir() else root.parent.resolve()
    catalog_data: dict[str, Any] | None = None
    catalog_path = ""
    vocabularies: dict[str, dict[str, Any]] = {}
    registries: dict[str, dict[str, Any]] = {}
    collections: dict[str, list[Any]] = {
        "resources": [],
        "works": [],
        "witnesses": [],
        "publications": [],
        "relationships": [],
    }
    sources: dict[str, str] = {}
    collection_for_type = {
        "resource": "resources",
        "work": "works",
        "witness": "witnesses",
        "publication": "publications",
        "relationship": "relationships",
    }

    for path in files:
        relative = path.relative_to(boundary).as_posix()
        document = load_yaml_bytes(path.read_bytes(), source_path=relative)
        if not isinstance(document, Mapping):
            raise InventoryCompositionError(
                _diagnostic(
                    "invalid_document",
                    relative,
                    "inventory document must be a mapping",
                )
            )
        if document.get("record_type") == "catalog":
            if catalog_data is not None:
                raise InventoryCompositionError(
                    _diagnostic(
                        "duplicate_catalog",
                        relative,
                        f"catalog already declared by {catalog_path}",
                    )
                )
            catalog_data = dict(document)
            catalog_path = relative
            continue
        if "vocabulary" in document:
            header = document["vocabulary"]
            terms = document.get("terms")
            if not isinstance(header, Mapping) or not isinstance(terms, list):
                raise InventoryCompositionError(
                    _diagnostic(
                        "invalid_vocabulary",
                        relative,
                        "vocabulary and terms must be a mapping and list",
                    )
                )
            identifier = header.get("id")
            term_ids = [
                term.get("id") if isinstance(term, Mapping) else None for term in terms
            ]
            if not isinstance(identifier, str) or not all(
                isinstance(term_id, str) for term_id in term_ids
            ):
                raise InventoryCompositionError(
                    _diagnostic(
                        "invalid_vocabulary",
                        relative,
                        "vocabulary and term IDs must be strings",
                    )
                )
            if identifier in vocabularies:
                raise InventoryCompositionError(
                    _diagnostic(
                        "duplicate_vocabulary",
                        relative,
                        f"duplicate vocabulary {identifier!r}",
                    )
                )
            vocabularies[identifier] = {
                "schema_version": document.get("schema_version"),
                "ids": term_ids,
            }
            continue
        if "registry" in document:
            header = document["registry"]
            if not isinstance(header, Mapping) or not isinstance(header.get("id"), str):
                raise InventoryCompositionError(
                    _diagnostic(
                        "invalid_registry",
                        relative,
                        "registry must declare a string id",
                    )
                )
            identifier = str(header["id"])
            if identifier in registries:
                raise InventoryCompositionError(
                    _diagnostic(
                        "duplicate_registry",
                        relative,
                        f"duplicate registry {identifier!r}",
                    )
                )
            definition = {
                "schema_version": document.get("schema_version"),
                "terms": document.get("terms", []),
            }
            try:
                registries[identifier] = OpenRegistryDefinition.model_validate(
                    definition
                ).model_dump(mode="python")
            except ValidationError as error:
                raise _model_error(relative, error) from error
            continue

        record_type_value = document.get("record_type")
        record_type = record_type_value if isinstance(record_type_value, str) else ""
        collection = collection_for_type.get(record_type)
        if collection is None:
            raise InventoryCompositionError(
                _diagnostic(
                    "unknown_document",
                    relative,
                    "document is not a catalog, vocabulary, registry, or record",
                )
            )
        try:
            record = _INVENTORY_ADAPTER.validate_python(document)
        except ValidationError as error:
            raise _model_error(relative, error) from error
        collections[collection].append(record)
        sources[str(record.id)] = relative

    if catalog_data is None:
        raise InventoryCompositionError(
            _diagnostic(
                "missing_catalog",
                boundary.as_posix(),
                "inventory must contain exactly one catalog document",
            )
        )

    base_vocabularies = catalog_data.get("vocabularies", {})
    base_registries = catalog_data.get("registries", {})
    if not isinstance(base_vocabularies, Mapping) or not isinstance(
        base_registries, Mapping
    ):
        raise InventoryCompositionError(
            _diagnostic(
                "invalid_catalog",
                catalog_path,
                "catalog vocabularies and registries must be mappings",
            )
        )
    merged_vocabularies = dict(base_vocabularies)
    merged_registries = dict(base_registries)
    for name, definition in vocabularies.items():
        if name in merged_vocabularies:
            raise InventoryCompositionError(
                _diagnostic(
                    "duplicate_vocabulary",
                    catalog_path,
                    f"duplicate vocabulary {name!r}",
                )
            )
        merged_vocabularies[name] = definition
    for name, definition in registries.items():
        if name in merged_registries:
            raise InventoryCompositionError(
                _diagnostic(
                    "duplicate_registry",
                    catalog_path,
                    f"duplicate registry {name!r}",
                )
            )
        merged_registries[name] = definition

    composed = dict(catalog_data)
    composed["vocabularies"] = merged_vocabularies
    composed["registries"] = merged_registries
    for field, records in collections.items():
        existing = composed.get(field, [])
        if not isinstance(existing, list):
            raise InventoryCompositionError(
                _diagnostic(
                    "invalid_catalog",
                    catalog_path,
                    f"catalog field {field!r} must be a list",
                )
            )
        combined = [*existing, *records]
        composed[field] = sorted(
            combined,
            key=lambda item: str(
                item.get("id") if isinstance(item, Mapping) else item.id
            ),
        )
    try:
        return Catalog.model_validate(composed), sources
    except ValidationError as error:
        raise _model_error(catalog_path, error) from error


def _load_json_bytes(data: bytes, path: str) -> Catalog:
    text = _decode_utf8(data, path)
    try:
        return Catalog.model_validate_json(text)
    except ValidationError as error:
        raise _model_error(path, error) from error


def load_catalog(path: Path) -> tuple[Catalog, dict[str, str]]:
    """Load an explicit authoring inventory directory or canonical JSON snapshot."""

    if path.is_file() and path.suffix.casefold() == ".json":
        return _load_json_bytes(path.read_bytes(), path.as_posix()), {}
    return _compose_authoring(path)


def load_bundled_catalog(
    *,
    package: str = "histgerm.resources",
    resource: str = "inventory/snapshot.json",
) -> Catalog:
    """Load the package snapshot strictly within the declared resource boundary."""

    parts = resource.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise InventoryDiscoveryError(
            _diagnostic(
                "invalid_resource_path",
                f"{package}:{resource}",
                "package resource path must remain within its declared boundary",
            )
        )
    target = resources.files(package).joinpath(*parts)
    return _load_json_bytes(target.read_bytes(), f"{package}:{resource}")


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
    "discover_inventory_files",
    "load_bundled_catalog",
    "load_catalog",
    "load_yaml_bytes",
    "load_yaml_file",
]
