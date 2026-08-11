from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Literal

import pytest
import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from histgerm.models.common import (
    EntityReference,
    ExtensionData,
    KnowledgeValue,
    NonEmptyStr,
    SelectionScope,
)
from histgerm.models.provenance import (
    EvidenceItem,
    ProvenancedRecord,
    provenance_completeness_report,
    required_provenance_pointers,
    resolve_json_pointer,
)
from histgerm.models.resource import Resource

UNKNOWN = {"status": "unknown"}
NOT_APPLICABLE = {"status": "not_applicable"}


class FixtureRecord(ProvenancedRecord):
    record_type: Literal["fixture"] = "fixture"
    id: str
    name: NonEmptyStr
    description: KnowledgeValue[NonEmptyStr]
    unavailable: KnowledgeValue[NonEmptyStr]
    facts: list[dict[str, str]]
    publication_ids: list[str]
    source: EntityReference
    target: EntityReference
    source_scope: SelectionScope
    evidence_ids: list[str]
    extensions: ExtensionData = {}


def evidence(identifier: str = "evidence-source") -> dict[str, object]:
    return {
        "id": identifier,
        "source_url": "https://example.invalid/source",
        "accessed_on": date(2026, 8, 11),
        "source_kind": "synthetic_fixture",
        "quotation": {"status": "known", "value": "Supporting text"},
        "note": UNKNOWN,
        "publication_id": UNKNOWN,
        "archived_url": UNKNOWN,
    }


def record_data() -> dict[str, object]:
    return {
        "id": "res-fixture",
        "name": "A/B ~ name",
        "description": {"status": "known", "value": "Description"},
        "unavailable": UNKNOWN,
        "facts": [{"a/b": "slash"}, {"til~de": "tilde"}],
        "publication_ids": ["pub-fixture"],
        "source": {
            "entity_type": "resource",
            "id": "res-related",
        },
        "target": {
            "entity_type": "publication",
            "id": "pub-related",
        },
        "source_scope": {
            "resource_ids": ["res-related"],
            "version_ids": [],
            "component_ids": [],
            "document_ids": [],
            "annotation_ids": [],
            "filter": NOT_APPLICABLE,
        },
        "evidence_ids": ["evidence-source"],
        "extensions": {"org.example.test": {"score": 0.75}},
        "evidence": [evidence()],
    }


def complete_claims() -> dict[str, list[str]]:
    return {
        "/name": ["evidence-source"],
        "/description/value": ["evidence-source"],
        "/facts/0/a~1b": ["evidence-source"],
        "/facts/1/til~0de": ["evidence-source"],
        "/publication_ids/0": ["evidence-source"],
        "/source/id": ["evidence-source"],
        "/target/id": ["evidence-source"],
        "/source_scope/resource_ids/0": ["evidence-source"],
        "/extensions/org.example.test/score": ["evidence-source"],
    }


def test_evidence_item_validates_fields_and_round_trips() -> None:
    item = EvidenceItem.model_validate(evidence())
    assert EvidenceItem.model_validate_json(item.model_dump_json()) == item

    for field, value in [
        ("id", "res-wrong"),
        ("source_url", "file:///source"),
        ("accessed_on", "not-a-date"),
        ("source_kind", "Not Canonical"),
        ("quotation", {"status": "known", "value": ""}),
        ("publication_id", {"status": "known", "value": "res-wrong"}),
    ]:
        invalid = evidence()
        invalid[field] = value
        with pytest.raises(ValidationError):
            EvidenceItem.model_validate(invalid)


def test_evidence_ids_must_be_unique() -> None:
    data = record_data()
    data["evidence"] = [evidence(), evidence()]
    with pytest.raises(ValidationError, match="evidence IDs must be unique"):
        FixtureRecord.model_validate(data)


def test_claim_references_must_be_non_empty_and_duplicate_free() -> None:
    data = record_data()
    data["claims"] = {"/name": []}
    with pytest.raises(ValidationError):
        FixtureRecord.model_validate(data)

    data["claims"] = {"/name": ["evidence-source", "evidence-source"]}
    with pytest.raises(ValidationError, match="duplicate-free"):
        FixtureRecord.model_validate(data)


def test_exact_pointers_escaping_and_list_indices_are_complete() -> None:
    data = record_data()
    data["claims"] = complete_claims()
    record = FixtureRecord.model_validate(data)
    report = provenance_completeness_report(record)
    assert report.is_complete
    assert required_provenance_pointers(record) == frozenset(complete_claims())

    document = record.model_dump(mode="json")
    slash = resolve_json_pointer(document, "/facts/0/a~1b")
    tilde = resolve_json_pointer(document, "/facts/1/til~0de")
    assert slash.status == "resolved" and slash.value == "slash"
    assert tilde.status == "resolved" and tilde.value == "tilde"
    assert resolve_json_pointer(document, "/facts/01/a~1b").status == "unresolved"


@pytest.mark.parametrize(
    ("pointer", "message"),
    [
        ("name", "RFC 6901"),
        ("/facts/2/a~1b", "does not resolve"),
        ("/facts", "container"),
        ("/evidence/0/source_url", "cannot point"),
        ("/claims", "cannot point"),
        ("/id", "exempt"),
        ("/description/status", "exempt"),
    ],
)
def test_invalid_dangling_container_self_and_exempt_pointers_are_rejected(
    pointer: str, message: str
) -> None:
    data = record_data()
    data["claims"] = {pointer: ["evidence-source"]}
    with pytest.raises(ValidationError, match=message):
        FixtureRecord.model_validate(data)


def test_dangling_evidence_is_rejected_at_exact_pointer() -> None:
    data = record_data()
    data["claims"] = {"/name": ["evidence-missing"]}
    with pytest.raises(
        ValidationError, match="/name: dangling_evidence.*evidence-missing"
    ):
        FixtureRecord.model_validate(data)


def test_unknown_and_not_applicable_are_structurally_exempt() -> None:
    data = record_data()
    data["description"] = UNKNOWN
    data["unavailable"] = NOT_APPLICABLE
    record = FixtureRecord.model_validate(data)
    pointers = required_provenance_pointers(record)
    assert "/description/status" not in pointers
    assert "/unavailable/status" not in pointers


def test_not_publicly_available_requires_status_evidence() -> None:
    data = record_data()
    data["unavailable"] = {"status": "not_publicly_available"}
    record = FixtureRecord.model_validate(data)
    report = provenance_completeness_report(record)
    assert any(
        error.pointer == "/unavailable/status" and error.error_code == "missing_claim"
        for error in report.errors
    )

    data["claims"] = {"/unavailable/status": ["evidence-source"]}
    record = FixtureRecord.model_validate(data)
    assert "/unavailable/status" not in {
        error.pointer for error in provenance_completeness_report(record).errors
    }


def test_own_id_structural_references_and_discriminators_are_exempt() -> None:
    record = FixtureRecord.model_validate(record_data())
    pointers = required_provenance_pointers(record)
    assert "/id" not in pointers
    assert "/record_type" not in pointers
    assert "/evidence_ids/0" not in pointers


def test_publication_relationship_and_scope_references_require_claims() -> None:
    record = FixtureRecord.model_validate(record_data())
    pointers = required_provenance_pointers(record)
    assert "/publication_ids/0" in pointers
    assert "/source/id" in pointers
    assert "/target/id" in pointers
    assert "/source_scope/resource_ids/0" in pointers


def test_evidence_publication_reference_remains_excluded() -> None:
    data = record_data()
    item = evidence()
    item["publication_id"] = {"status": "known", "value": "pub-fixture"}
    data["evidence"] = [item]
    record = FixtureRecord.model_validate(data)
    assert all(
        not pointer.startswith("/evidence/")
        for pointer in required_provenance_pointers(record)
    )


def test_rnntagger_publication_claim_is_accepted() -> None:
    inventory_path = (
        Path(__file__).parents[2]
        / "inventory"
        / "resources"
        / "tools"
        / "rnntagger.yaml"
    )
    data = yaml.safe_load(inventory_path.read_text(encoding="utf-8"))
    record = Resource.model_validate(data)
    pointers = required_provenance_pointers(record)
    assert record.claims["/publication_ids/0"] == frozenset(
        {"evidence-rnntagger-homepage", "evidence-rnntagger-publication"}
    )
    assert "/versions/0/id" not in pointers
    assert "/versions/0/distributions/0/id" not in pointers


def test_known_factual_fields_and_extensions_require_exact_claims() -> None:
    record = FixtureRecord.model_validate(record_data())
    errors = provenance_completeness_report(record).errors
    missing = {error.pointer for error in errors if error.error_code == "missing_claim"}
    assert missing == set(complete_claims())


def test_internal_looking_factual_values_are_not_exempted_by_content() -> None:
    data = record_data()
    data["description"] = {"status": "known", "value": "res-external-fact"}
    data["extensions"] = {"org.example.test": {"resource_id": "res-extension-fact"}}
    record = FixtureRecord.model_validate(data)
    pointers = required_provenance_pointers(record)
    assert "/description/value" in pointers
    assert "/extensions/org.example.test/resource_id" in pointers


def test_report_returns_multiple_errors_in_deterministic_order() -> None:
    data = record_data()
    data["claims"] = {"/name": ["evidence-missing"]}
    with pytest.raises(ValidationError, match="dangling_evidence"):
        FixtureRecord.model_validate(data)

    record = FixtureRecord.model_validate(record_data())
    report = provenance_completeness_report(record)
    assert len(report.errors) == len(complete_claims())
    assert list(report.errors) == sorted(
        report.errors,
        key=lambda item: (item.pointer, item.error_code, item.message),
    )
    assert all(error.record_id == "res-fixture" for error in report.errors)
    assert all("Add an exact claim" in error.message for error in report.errors)


def test_provenanced_record_round_trip_is_canonical() -> None:
    data = record_data()
    data["claims"] = {
        pointer: ["evidence-source"] for pointer in reversed(complete_claims())
    }
    record = FixtureRecord.model_validate(data)
    dumped = record.model_dump_json()
    assert FixtureRecord.model_validate_json(dumped) == record
    assert list(json.loads(dumped)["claims"]) == sorted(complete_claims())
