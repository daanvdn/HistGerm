from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from histgerm.loading import HistGermLoadingError, load_catalog, load_yaml_bytes
from histgerm.models.catalog import Catalog
from histgerm.models.common import KnowledgeValue
from histgerm.packaging import generate_inventory, load_verified_bundled_catalog
from histgerm.validation import validate_catalog

ROOT = Path(__file__).parents[2]
INVENTORY = ROOT / "inventory"
UNSAFE = ROOT / "tests" / "fixtures" / "loading" / "unsafe"


def _statuses(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        status = value.get("status")
        if isinstance(status, str):
            found.add(status)
        for child in value.values():
            found.update(_statuses(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_statuses(child))
    return found


def test_authoritative_inventory_covers_valid_record_shapes_and_knowledge_states() -> (
    None
):
    catalog, source_paths = load_catalog(INVENTORY)
    report = validate_catalog(catalog, source_paths=source_paths)

    assert report.is_valid, report.errors
    resources = {resource.id: resource for resource in catalog.resources}
    assert resources["res-rem"].categories == {"corpus"}
    assert resources["res-mwb"].categories == {"dictionary"}
    assert resources["res-rnntagger"].categories == {"lemmatizer", "pos_tagger"}
    stages = resources["res-rnntagger"].language_stage_ids
    assert stages.status == "known"
    assert stages.value == {"mhg", "enhg"}
    assert {"known", "unknown", "not_applicable"} <= _statuses(
        catalog.model_dump(mode="json")
    )


@pytest.mark.parametrize(
    "value",
    [
        {"status": "known", "value": "fixture"},
        {"status": "unknown"},
        {"status": "not_applicable"},
        {"status": "not_publicly_available"},
    ],
)
def test_every_knowledge_state_round_trips(value: dict[str, str]) -> None:
    adapter: TypeAdapter[KnowledgeValue[str]] = TypeAdapter(KnowledgeValue[str])
    parsed = adapter.validate_python(value)
    assert adapter.validate_json(adapter.dump_json(parsed)) == parsed


def test_authoring_and_packaged_catalogs_are_exactly_equivalent() -> None:
    generated = generate_inventory(INVENTORY)
    bundled = load_verified_bundled_catalog()

    assert generated.catalog == bundled
    assert Catalog.model_validate_json(generated.snapshot) == bundled
    assert generate_inventory(INVENTORY).files == generated.files


@pytest.mark.parametrize(
    ("fixture", "code"),
    [
        ("duplicate-key.yaml", "duplicate_key"),
        ("anchor.yaml", "unsafe_yaml"),
        ("alias.yaml", "unsafe_yaml"),
        ("merge-key.yaml", "unsafe_yaml"),
        ("custom-tag.yaml", "unsafe_yaml"),
        ("multi-document.yaml", "unsafe_yaml"),
        ("non-string-key.yaml", "invalid_mapping_key"),
    ],
)
def test_malformed_yaml_reports_file_line_and_code(fixture: str, code: str) -> None:
    path = UNSAFE / fixture
    with pytest.raises(HistGermLoadingError) as caught:
        load_yaml_bytes(path.read_bytes(), source_path=path.as_posix())

    diagnostic = caught.value.diagnostic
    assert diagnostic.code == code
    assert diagnostic.path.endswith(fixture)
    assert diagnostic.location
    assert f"{diagnostic.path}:{diagnostic.location}" in str(caught.value)


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("missing_evidence", "dangling_evidence"),
        ("broken_pointer", "dangling_pointer"),
    ],
)
def test_invalid_provenance_is_rejected_with_exact_claim_path(
    case: str, expected: str
) -> None:
    data = load_verified_bundled_catalog().model_dump(mode="python")
    resource = data["resources"][0]
    if case == "missing_evidence":
        resource["claims"]["/canonical_name"] = frozenset({"evidence-missing"})
    else:
        evidence_id = resource["evidence"][0]["id"]
        resource["claims"]["/does/not/exist"] = frozenset({evidence_id})

    with pytest.raises(ValidationError) as caught:
        Catalog.model_validate(data)

    message = str(caught.value)
    assert "resources.0" in message
    assert expected in message
    assert (
        "/canonical_name" in message
        if case == "missing_evidence"
        else "/does/not/exist" in message
    )


def test_stale_references_duplicate_ids_and_invalid_vocab_are_path_rich() -> None:
    original = load_verified_bundled_catalog().model_dump(mode="python")

    stale = deepcopy(original)
    stale["resources"][0]["publication_ids"] = frozenset({"pub-missing"})
    stale_report = validate_catalog(Catalog.model_validate(stale))
    stale_error = next(
        item for item in stale_report.errors if item.code == "dangling_reference"
    )
    assert stale_error.pointer == "/resources/0/publication_ids"
    assert "pub-missing" in stale_error.message

    duplicate = deepcopy(original)
    duplicate["publications"].append(deepcopy(duplicate["publications"][0]))
    duplicate_report = validate_catalog(Catalog.model_validate(duplicate))
    duplicate_error = next(
        item for item in duplicate_report.errors if item.code == "duplicate_id"
    )
    assert duplicate_error.path == "<catalog>"
    assert duplicate_error.pointer.startswith("/publications/")

    invalid_vocab: dict[str, Any] = deepcopy(original)
    language_stages = invalid_vocab["vocabularies"]["language_stage"]["ids"]
    invalid_vocab["vocabularies"]["language_stage"]["ids"] = frozenset(
        set(language_stages) - {"mhg"}
    )
    vocab_report = validate_catalog(Catalog.model_validate(invalid_vocab))
    vocab_error = next(
        item
        for item in vocab_report.errors
        if item.code == "unregistered_vocabulary_value"
    )
    assert vocab_error.pointer.startswith("/resources/")
    assert "'mhg'" in vocab_error.message
