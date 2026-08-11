from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

from histgerm.models.catalog import Catalog
from histgerm.models.resource import Resource, ResourceVersion
from histgerm.validation import validate_catalog

UNKNOWN = {"status": "unknown"}
NOT_APPLICABLE = {"status": "not_applicable"}
VOCABULARIES = {
    "access_requirement",
    "alignment_unit",
    "annotation_production_method",
    "annotation_quality",
    "annotation_task",
    "auxiliary_task",
    "availability",
    "certainty",
    "data_format",
    "dating_method",
    "distribution_kind",
    "editorial_intervention",
    "entity_reference_kind",
    "evidence_source_kind",
    "knowledge_state",
    "language_stage",
    "license_scope",
    "license_status",
    "lifecycle_status",
    "maintenance_status",
    "measurement_origin",
    "name_type",
    "overlap_extent",
    "party_type",
    "permission_state",
    "relationship_kind",
    "resource_category",
    "responsible_party_role",
    "size_unit",
    "suitability_decision",
    "text_layer_kind",
    "transcription_method",
    "unicode_normalization",
    "url_purpose",
    "witness_kind",
}
OPEN_REGISTRIES = {
    "dialects",
    "genres",
    "languages",
    "licenses",
    "regions",
    "text_types",
}


def evidence(identifier: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "source_url": "https://example.invalid/source",
        "accessed_on": date(2026, 8, 11),
        "source_kind": "synthetic_fixture",
        "quotation": UNKNOWN,
        "note": UNKNOWN,
        "publication_id": UNKNOWN,
        "archived_url": UNKNOWN,
    }


def corpus_profile() -> dict[str, Any]:
    return {
        "transcription_methods": UNKNOWN,
        "editorial_interventions": UNKNOWN,
        "unicode_normalization": UNKNOWN,
        "character_repertoire": UNKNOWN,
        "tokenization_convention": UNKNOWN,
        "sentence_segmentation_convention": UNKNOWN,
        "text_layers": UNKNOWN,
        "stable_document_identifiers": UNKNOWN,
        "stable_token_identifiers": UNKNOWN,
        "corpus_schema": UNKNOWN,
        "access_restrictions": UNKNOWN,
    }


def resource(identifier: str = "res-example") -> dict[str, Any]:
    evidence_id = f"evidence-{identifier.removeprefix('res-')}"
    return {
        "id": identifier,
        "canonical_name": identifier,
        "categories": ["corpus"],
        "description": UNKNOWN,
        "responsible_parties": UNKNOWN,
        "homepage_url": UNKNOWN,
        "repository_url": UNKNOWN,
        "language_stage_ids": UNKNOWN,
        "chronology": UNKNOWN,
        "geography": UNKNOWN,
        "maintenance_status": UNKNOWN,
        "versions": [],
        "corpus": {"status": "known", "value": corpus_profile()},
        "tool": NOT_APPLICABLE,
        "dictionary": NOT_APPLICABLE,
        "record_reviewed_on": date(2026, 8, 11),
        "evidence": [evidence(evidence_id)],
        "claims": {
            "/canonical_name": [evidence_id],
            "/categories/0": [evidence_id],
        },
    }


def catalog_data(*resources: dict[str, Any]) -> dict[str, Any]:
    vocabularies = {
        name: {"schema_version": "1.0.0", "ids": ["fixture"]} for name in VOCABULARIES
    }
    vocabularies.update(
        {
            "resource_category": {
                "schema_version": "1.0.0",
                "ids": ["corpus"],
            },
            "evidence_source_kind": {
                "schema_version": "1.0.0",
                "ids": ["synthetic_fixture"],
            },
            "knowledge_state": {
                "schema_version": "1.0.0",
                "ids": ["known", "unknown", "not_applicable"],
            },
        }
    )
    return {
        "schema_version": "1.0.0",
        "inventory_release": "fixture",
        "generated_on": date(2026, 8, 11),
        "vocabularies": vocabularies,
        "registries": {
            name: {"schema_version": "1.0.0", "terms": []} for name in OPEN_REGISTRIES
        },
        "resources": list(resources),
        "notes": UNKNOWN,
    }


def valid_catalog(*resources: dict[str, Any]) -> Catalog:
    return Catalog.model_validate(catalog_data(*resources))


def test_empty_and_synthetic_catalog_validate_deterministically() -> None:
    report = validate_catalog(valid_catalog(resource()))
    assert report.is_valid
    assert report.errors == ()
    assert (
        Catalog.model_validate_json(valid_catalog().model_dump_json())
        == valid_catalog()
    )


def test_global_ids_include_nested_evidence_and_aggregate_errors() -> None:
    first = resource("res-one")
    second = resource("res-two")
    second["evidence"][0]["id"] = "evidence-one"
    second["claims"] = {"/canonical_name": ["evidence-one"]}
    second["publication_ids"] = ["pub-missing"]
    report = validate_catalog(valid_catalog(first, second))
    assert {error.code for error in report.errors} >= {
        "duplicate_id",
        "dangling_reference",
    }
    assert list(report.errors) == sorted(
        report.errors,
        key=lambda item: (item.path, item.model_path, item.code, item.message),
    )


def test_registered_closed_and_open_values_are_required() -> None:
    data = catalog_data(resource())
    data["vocabularies"]["resource_category"]["ids"] = ["dictionary"]
    report = validate_catalog(Catalog.model_validate(data))
    assert "unregistered_vocabulary_value" in {error.code for error in report.errors}

    open_data = resource()
    open_data["geography"] = {
        "status": "known",
        "value": {
            "region_ids": {"status": "known", "value": ["rheinland"]},
            "dialect_ids": UNKNOWN,
            "certainty": UNKNOWN,
            "note": UNKNOWN,
        },
    }
    open_data["claims"]["/geography/value/region_ids/value/0"] = ["evidence-example"]
    reparsed = valid_catalog(open_data)
    assert "unregistered_open_registry_value" in {
        error.code for error in validate_catalog(reparsed).errors
    }


def test_complete_registries_and_normalized_open_aliases_are_enforced() -> None:
    data = catalog_data()
    del data["vocabularies"]["certainty"]
    del data["registries"]["licenses"]
    report = validate_catalog(Catalog.model_validate(data))
    assert {error.code for error in report.errors} >= {
        "missing_vocabulary",
        "missing_open_registry",
    }

    data = catalog_data()
    data["registries"]["regions"]["terms"] = [
        {
            "id": "rheinland",
            "canonical_label": "Rheinland",
            "aliases": [" RHEINLAND "],
            "description": UNKNOWN,
            "evidence": [evidence("evidence-rheinland")],
            "claims": {
                "/canonical_label": ["evidence-rheinland"],
                "/aliases/0": ["evidence-rheinland"],
            },
        }
    ]
    report = validate_catalog(Catalog.model_validate(data))
    assert any(error.code == "duplicate_registry_name" for error in report.errors)


def test_resource_alias_normalization_is_unique() -> None:
    data = resource()
    data["alternative_names"] = [
        {"text": " RES-EXAMPLE ", "language": UNKNOWN, "name_type": "short"}
    ]
    parsed = valid_catalog(data)
    report = validate_catalog(parsed)
    assert any(error.code == "duplicate_resource_name" for error in report.errors)


def test_scope_hierarchy_and_relationship_endpoints_are_path_rich() -> None:
    left = Resource.model_validate(resource("res-left"))
    right = Resource.model_validate(resource("res-right"))
    left_version = {
        "id": "ver-left",
        "version_label": UNKNOWN,
        "release_date": UNKNOWN,
        "superseded": UNKNOWN,
        "changelog_url": UNKNOWN,
        "language_stage_ids": UNKNOWN,
        "chronology": UNKNOWN,
    }
    right_version = left_version | {"id": "ver-right"}
    left = left.model_copy(
        update={"versions": [ResourceVersion.model_validate(left_version)]}
    )
    right = right.model_copy(
        update={"versions": [ResourceVersion.model_validate(right_version)]}
    )
    data = catalog_data()
    data["resources"] = [
        left.model_dump(mode="python"),
        right.model_dump(mode="python"),
    ]
    data["relationships"] = [
        {
            "id": "rel-scope",
            "source": {"entity_type": "version", "id": "ver-left"},
            "target": {"entity_type": "version", "id": "ver-right"},
            "kind": "derived_from",
            "directional": True,
            "source_scope": {
                "resource_ids": ["res-right"],
                "version_ids": ["ver-left"],
                "filter": UNKNOWN,
            },
            "target_scope": {
                "version_ids": ["ver-right"],
                "filter": UNKNOWN,
            },
            "overlap_extent": "unknown",
            "overlap_measurement": NOT_APPLICABLE,
            "certainty": UNKNOWN,
            "note": UNKNOWN,
            "duplicate_group_id": NOT_APPLICABLE,
            "canonical_scope": NOT_APPLICABLE,
        }
    ]
    parsed = Catalog.model_validate(data)
    report = validate_catalog(parsed, source_paths={"rel-scope": "relations.json"})
    codes = {error.code for error in report.errors}
    assert "incoherent_scope" in codes
    assert any(
        error.path == "relations.json"
        and error.model_path.startswith("/relationships/0")
        for error in report.errors
    )


def test_derivation_cycles_and_dangling_endpoints_fail() -> None:
    first = resource("res-a")
    second = resource("res-b")
    relationships = []
    for identifier, source, target in (
        ("rel-a", "res-a", "res-b"),
        ("rel-b", "res-b", "res-a"),
        ("rel-c", "res-a", "res-missing"),
    ):
        relationships.append(
            {
                "id": identifier,
                "source": {"entity_type": "resource", "id": source},
                "target": {"entity_type": "resource", "id": target},
                "kind": "derived_from",
                "directional": True,
                "source_scope": {"resource_ids": [source], "filter": UNKNOWN},
                "target_scope": {"resource_ids": [target], "filter": UNKNOWN},
                "overlap_extent": "unknown",
                "overlap_measurement": NOT_APPLICABLE,
                "certainty": UNKNOWN,
                "note": UNKNOWN,
                "duplicate_group_id": NOT_APPLICABLE,
                "canonical_scope": NOT_APPLICABLE,
            }
        )
    data = catalog_data(first, second)
    data["relationships"] = relationships
    report = validate_catalog(Catalog.model_validate(data))
    assert {error.code for error in report.errors} >= {
        "relationship_cycle",
        "dangling_reference",
    }


def test_provenance_completeness_is_integrated_without_hiding_other_errors() -> None:
    data = resource()
    data["claims"] = {}
    data["publication_ids"] = ["pub-missing"]
    report = validate_catalog(valid_catalog(data))
    assert {error.code for error in report.errors} >= {
        "provenance_missing_claim",
        "dangling_reference",
    }


def test_payload_paths_and_keys_are_rejected_even_at_catalog_root() -> None:
    catalog = valid_catalog()
    unsafe = catalog.model_copy(
        update={
            "extensions": {
                "org.example.fixture": {
                    "resource_payload": "C:\\datasets\\third-party.bin"
                }
            }
        }
    )
    codes = {error.code for error in validate_catalog(unsafe).errors}
    assert {"forbidden_payload_key", "forbidden_local_path"} <= codes


def test_json_cli_valid_zero_invalid_one_and_missing_yaml_boundary_two(
    tmp_path: Path,
) -> None:
    valid_path = tmp_path / "valid.json"
    invalid_path = tmp_path / "invalid.json"
    valid_path.write_text(valid_catalog().model_dump_json(), encoding="utf-8")
    invalid = resource()
    invalid["claims"] = {}
    invalid_path.write_text(valid_catalog(invalid).model_dump_json(), encoding="utf-8")

    valid_run = subprocess.run(
        [
            sys.executable,
            "-m",
            "histgerm.validation",
            "inventory",
            str(valid_path),
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    invalid_run = subprocess.run(
        [
            sys.executable,
            "-m",
            "histgerm.validation",
            "inventory",
            str(invalid_path),
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    missing_boundary = subprocess.run(
        [
            sys.executable,
            "-m",
            "histgerm.validation",
            "inventory",
            str(tmp_path),
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    valid_envelope = json.loads(valid_run.stdout)
    invalid_envelope = json.loads(invalid_run.stdout)
    boundary_envelope = json.loads(missing_boundary.stdout)
    assert valid_run.returncode == 0
    assert valid_envelope["protocol"] == "histgerm.command.v1"
    assert invalid_run.returncode == 1
    assert invalid_envelope["errors"][0]["model_path"]
    assert missing_boundary.returncode == 2
    assert boundary_envelope["status"] == "error"
    assert boundary_envelope["errors"] == [
        {
            "code": "input_error",
            "severity": "error",
            "path": str(tmp_path),
            "model_path": "",
            "pointer": "",
            "message": (
                f"{tmp_path.as_posix()}:: "
                "inventory must contain exactly one catalog document"
            ),
        }
    ]
