"""Canonical, deterministic serialization for HistGerm models."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel

from histgerm.models.catalog import Catalog


class _AnchorFreeDumper(yaml.SafeDumper):  # type: ignore[misc]
    def ignore_aliases(self, data: Any) -> bool:
        return True


def _ordered_catalog(document: dict[str, Any]) -> dict[str, Any]:
    for field in (
        "resources",
        "works",
        "witnesses",
        "publications",
        "relationships",
    ):
        document[field] = sorted(document[field], key=lambda item: item["id"])
    for definition in document["registries"].values():
        definition["terms"] = sorted(definition["terms"], key=lambda item: item["id"])
    return document


def _json_value(value: BaseModel | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        dumped = value.model_dump(mode="json")
    else:
        dumped = dict(value)
    if isinstance(value, Catalog):
        _ordered_catalog(dumped)
    return dumped


def _yaml_compatible(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _yaml_compatible(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {str(key): _yaml_compatible(child) for key, child in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_yaml_compatible(child) for child in value), key=str)
    if isinstance(value, tuple):
        return [_yaml_compatible(child) for child in value]
    if isinstance(value, list):
        return [_yaml_compatible(child) for child in value]
    if isinstance(value, date):
        return value
    return value


def canonical_json_bytes(value: BaseModel | Mapping[str, Any]) -> bytes:
    """Return compact canonical JSON as UTF-8, LF, and one trailing newline."""

    document = _json_value(value)
    text = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return f"{text}\n".encode()


def canonical_json(value: BaseModel | Mapping[str, Any]) -> str:
    """Return canonical JSON text."""

    return canonical_json_bytes(value).decode("utf-8")


def canonical_yaml_bytes(value: BaseModel | Mapping[str, Any]) -> bytes:
    """Return stable block YAML without aliases, anchors, or explicit tags."""

    document = _yaml_compatible(value)
    if isinstance(value, Catalog):
        document = _ordered_catalog(document)
    text: str = yaml.dump(
        document,
        Dumper=_AnchorFreeDumper,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=10_000,
        line_break="\n",
    )
    return text.replace("\r\n", "\n").encode("utf-8")


def canonical_yaml(value: BaseModel | Mapping[str, Any]) -> str:
    """Return canonical YAML text."""

    return canonical_yaml_bytes(value).decode("utf-8")


__all__ = [
    "canonical_json",
    "canonical_json_bytes",
    "canonical_yaml",
    "canonical_yaml_bytes",
]
