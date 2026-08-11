from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from histgerm.loading import load_catalog
from histgerm.models.catalog import Catalog
from histgerm.serialization import (
    canonical_json,
    canonical_json_bytes,
    canonical_yaml,
    canonical_yaml_bytes,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "loading"


def _catalog() -> Catalog:
    catalog, _ = load_catalog(FIXTURES / "safe")
    return catalog


def test_canonical_json_is_utf8_compact_sorted_lf_and_round_trips() -> None:
    catalog = _catalog()
    first = canonical_json_bytes(catalog)
    second = canonical_json(catalog).encode()

    assert first == second
    assert first.endswith(b"\n") and not first.endswith(b"\n\n")
    assert b"\r" not in first
    assert not first.startswith(b"\xef\xbb\xbf")
    assert b'"generated_on":"2026-08-11"' in first
    assert Catalog.model_validate_json(first) == catalog
    assert canonical_json_bytes(Catalog.model_validate_json(first)) == first
    assert list(json.loads(first)) == sorted(json.loads(first))


def test_canonical_yaml_has_model_order_unicode_lf_no_aliases_and_round_trips() -> None:
    catalog = _catalog()
    first = canonical_yaml_bytes(catalog)
    second = canonical_yaml(catalog).encode()
    text = first.decode("utf-8")

    assert first == second
    assert first.endswith(b"\n") and not first.endswith(b"\n\n")
    assert b"\r" not in first
    assert not first.startswith(b"\xef\xbb\xbf")
    assert not any(marker in text for marker in ("&id", "*id", "!!python", "!<"))
    assert text.startswith("record_type: catalog\nschema_version:")
    assert Catalog.model_validate(yaml.safe_load(text)) == catalog
    assert canonical_yaml_bytes(Catalog.model_validate(yaml.safe_load(text))) == first


def test_repeated_values_never_generate_yaml_anchors() -> None:
    shared: dict[str, Any] = {"status": "unknown"}
    rendered = canonical_yaml({"first": shared, "second": shared})
    assert "&id" not in rendered
    assert "*id" not in rendered


def test_catalog_id_collections_are_canonicalized_without_mutating_model() -> None:
    data = _catalog().model_dump(mode="python")
    data["registries"] = {
        "example": {
            "schema_version": "1.0.0",
            "terms": [
                {
                    "id": "beta",
                    "canonical_label": "Beta",
                    "description": {"status": "unknown"},
                },
                {
                    "id": "alpha",
                    "canonical_label": "Alpha",
                    "description": {"status": "unknown"},
                },
            ],
        }
    }
    catalog = Catalog.model_validate(data)
    assert [term.id for term in catalog.registries.root["example"].terms] == [
        "beta",
        "alpha",
    ]
    serialized = json.loads(canonical_json(catalog))
    assert [term["id"] for term in serialized["registries"]["example"]["terms"]] == [
        "alpha",
        "beta",
    ]
