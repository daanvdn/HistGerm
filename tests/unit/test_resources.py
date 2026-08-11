from __future__ import annotations

import re
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from histgerm.models.annotation import AnnotationLayer
from histgerm.models.corpus import SizeMeasurement
from histgerm.models.provenance import provenance_completeness_report
from histgerm.models.resource import Resource
from histgerm.models.tool import DatasetUse, EvaluationMetric

UNKNOWN = {"status": "unknown"}
NOT_APPLICABLE = {"status": "not_applicable"}
REPOSITORY_ROOT = Path(__file__).parents[2]


def known(value: Any) -> dict[str, Any]:
    return {"status": "known", "value": value}


def version(identifier: str = "ver-example-1") -> dict[str, Any]:
    return {
        "id": identifier,
        "version_label": known("1"),
        "release_date": UNKNOWN,
        "superseded": UNKNOWN,
        "changelog_url": UNKNOWN,
        "language_stage_ids": known(["mhg"]),
        "chronology": UNKNOWN,
        "components": [],
        "documents": [],
        "distributions": [],
        "annotations": [],
        "size_measurements": [],
        "extensions": {},
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


def unknown_license() -> dict[str, Any]:
    return {
        "status": "unknown",
        "license_id": UNKNOWN,
        "name": UNKNOWN,
        "url": UNKNOWN,
        "scopes": UNKNOWN,
        "note": UNKNOWN,
    }


def tool_profile() -> dict[str, Any]:
    return {
        "supported_tasks": known(["lemma"]),
        "input_formats": UNKNOWN,
        "output_formats": UNKNOWN,
        "language_stage_ids": known(["ohg", "mhg"]),
        "implementation_languages": UNKNOWN,
        "frameworks": UNKNOWN,
        "model_architecture": UNKNOWN,
        "training_data": UNKNOWN,
        "evaluation_data": UNKNOWN,
        "reported_metrics": UNKNOWN,
        "installation_url": UNKNOWN,
        "usage_url": UNKNOWN,
        "package_name": UNKNOWN,
        "cli": UNKNOWN,
        "api": NOT_APPLICABLE,
        "hugging_face": NOT_APPLICABLE,
        "software_license": unknown_license(),
        "model_license": {
            "status": "not_applicable",
            "license_id": NOT_APPLICABLE,
            "name": NOT_APPLICABLE,
            "url": NOT_APPLICABLE,
            "scopes": NOT_APPLICABLE,
            "note": NOT_APPLICABLE,
        },
        "maintenance_status": UNKNOWN,
    }


def dictionary_profile() -> dict[str, Any]:
    return {
        "lexical_coverage": UNKNOWN,
        "source_language_stage_ids": known(["mhg"]),
        "target_language_ids": UNKNOWN,
        "search_interface": UNKNOWN,
        "api": UNKNOWN,
        "download_formats": UNKNOWN,
        "machine_readable_availability": "unknown",
        "machine_readable_download_url": UNKNOWN,
        "query_capabilities": UNKNOWN,
        "lexical_features": {
            "headwords": UNKNOWN,
            "lemmas": UNKNOWN,
            "spelling_variants": UNKNOWN,
            "part_of_speech": UNKNOWN,
            "morphology": UNKNOWN,
            "senses": UNKNOWN,
            "etymology": UNKNOWN,
        },
        "citation_structure": UNKNOWN,
        "corpus_occurrence_links": UNKNOWN,
        "transcription_convention": UNKNOWN,
        "normalization_convention": UNKNOWN,
        "supervision_suitability": UNKNOWN,
        "source_publication_ids": [],
        "source_edition": UNKNOWN,
        "access_restrictions": UNKNOWN,
        "reuse_restrictions": UNKNOWN,
    }


def resource(
    categories: list[str],
    *,
    versions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    category_set = set(categories)
    return {
        "record_type": "resource",
        "id": "res-example",
        "canonical_name": "Example",
        "alternative_names": [],
        "categories": categories,
        "description": UNKNOWN,
        "responsible_parties": UNKNOWN,
        "homepage_url": UNKNOWN,
        "repository_url": UNKNOWN,
        "language_stage_ids": known(["ohg", "mhg"]),
        "chronology": UNKNOWN,
        "geography": UNKNOWN,
        "maintenance_status": UNKNOWN,
        "publication_ids": [],
        "versions": versions or [version()],
        "corpus": known(corpus_profile())
        if "corpus" in category_set
        else NOT_APPLICABLE,
        "tool": (
            known(tool_profile())
            if category_set
            & {"pos_tagger", "morphological_tagger", "lemmatizer", "syntactic_parser"}
            else NOT_APPLICABLE
        ),
        "dictionary": (
            known(dictionary_profile())
            if category_set & {"dictionary", "lexicon"}
            else NOT_APPLICABLE
        ),
        "record_reviewed_on": date(2026, 8, 11),
        "evidence": [],
        "claims": {},
        "extensions": {},
    }


def scope(**changes: Any) -> dict[str, Any]:
    value = {
        "resource_ids": ["res-example"],
        "version_ids": ["ver-example-1"],
        "component_ids": [],
        "document_ids": [],
        "annotation_ids": [],
        "filter": NOT_APPLICABLE,
    }
    value.update(changes)
    return value


def measurement(origin: str) -> dict[str, Any]:
    return {
        "unit": "token",
        "value": 12,
        "version_id": "ver-example-1",
        "scope": scope(),
        "counting_method": "Synthetic token count.",
        "origin": origin,
        "computed_on": (
            NOT_APPLICABLE if origin == "reported" else known(date(2026, 8, 11))
        ),
        "evidence_ids": (["evidence-example"] if origin == "reported" else []),
        "uncertainty_note": UNKNOWN,
    }


def annotation(
    identifier: str, quality: str, selected_scope: dict[str, Any]
) -> dict[str, Any]:
    return {
        "id": identifier,
        "task": "lemma",
        "scheme": UNKNOWN,
        "scope": selected_scope,
        "coverage_measurements": [],
        "alignment_unit": known("token"),
        "production_method": UNKNOWN,
        "quality": quality,
        "annotators": UNKNOWN,
        "guidelines_url": UNKNOWN,
        "inter_annotator_agreement": UNKNOWN,
        "evaluation_results": UNKNOWN,
        "missing_value_convention": UNKNOWN,
        "scheme_mappings": UNKNOWN,
        "extensions": {},
    }


@pytest.mark.parametrize(
    "categories", [["corpus"], ["lemmatizer", "pos_tagger"], ["dictionary"]]
)
def test_corpus_tool_dictionary_resources_round_trip(categories: list[str]) -> None:
    parsed = Resource.model_validate(resource(categories))
    assert Resource.model_validate_json(parsed.model_dump_json()) == parsed


def test_resource_supports_multiple_categories_and_language_stages() -> None:
    parsed = Resource.model_validate(resource(["corpus", "lemmatizer", "lexicon"]))
    assert parsed.categories == frozenset({"corpus", "lemmatizer", "lexicon"})
    assert parsed.language_stage_ids.value == frozenset({"ohg", "mhg"})  # type: ignore[union-attr]


def test_annotation_layers_keep_independent_scopes_and_qualities() -> None:
    data = resource(["corpus"])
    current = data["versions"][0]
    current["components"] = [
        {
            "id": "comp-a",
            "name": "A",
            "description": UNKNOWN,
            "parent_component_id": NOT_APPLICABLE,
            "language_stage_ids": known(["mhg"]),
            "chronology": UNKNOWN,
            "geography": UNKNOWN,
            "genres": UNKNOWN,
            "text_types": UNKNOWN,
            "document_ids": [],
            "size_measurements": [],
            "extensions": {},
        },
        {
            "id": "comp-b",
            "name": "B",
            "description": UNKNOWN,
            "parent_component_id": NOT_APPLICABLE,
            "language_stage_ids": known(["mhg"]),
            "chronology": UNKNOWN,
            "geography": UNKNOWN,
            "genres": UNKNOWN,
            "text_types": UNKNOWN,
            "document_ids": [],
            "size_measurements": [],
            "extensions": {},
        },
    ]
    current["annotations"] = [
        annotation(
            "ann-a",
            "expert_gold",
            scope(component_ids=["comp-a"]),
        ),
        annotation(
            "ann-b",
            "automatically_predicted",
            scope(component_ids=["comp-b"]),
        ),
    ]
    parsed = Resource.model_validate(data)
    assert parsed.versions[0].annotations[0].scope.component_ids == {"comp-a"}
    assert parsed.versions[0].annotations[1].quality == "automatically_predicted"


@pytest.mark.parametrize("origin", ["reported", "locally_computed"])
def test_each_measurement_origin_is_distinct_and_round_trips(origin: str) -> None:
    parsed = SizeMeasurement.model_validate(measurement(origin))
    assert parsed.origin == origin
    assert SizeMeasurement.model_validate_json(parsed.model_dump_json()) == parsed


def test_measurement_origin_invariants_are_enforced() -> None:
    invalid_reported = measurement("reported") | {"evidence_ids": []}
    with pytest.raises(ValidationError, match="requires evidence_ids"):
        SizeMeasurement.model_validate(invalid_reported)
    invalid_computed = measurement("locally_computed") | {"computed_on": NOT_APPLICABLE}
    with pytest.raises(ValidationError, match="known computation date"):
        SizeMeasurement.model_validate(invalid_computed)


@pytest.mark.parametrize(
    ("categories", "profile", "value"),
    [
        (["corpus"], "corpus", NOT_APPLICABLE),
        (["lemmatizer"], "tool", NOT_APPLICABLE),
        (["dictionary"], "dictionary", NOT_APPLICABLE),
        (["corpus"], "tool", known(tool_profile())),
    ],
)
def test_invalid_category_profile_combinations_fail(
    categories: list[str], profile: str, value: dict[str, Any]
) -> None:
    data = resource(categories)
    data[profile] = value
    with pytest.raises(ValidationError, match="profile"):
        Resource.model_validate(data)


def test_empty_categories_and_known_stage_sets_fail() -> None:
    with pytest.raises(ValidationError):
        Resource.model_validate(resource([]))
    data = resource(["corpus"])
    data["language_stage_ids"] = known([])
    with pytest.raises(ValidationError, match="must not be empty"):
        Resource.model_validate(data)


def test_version_component_document_hierarchy_and_scope_are_local() -> None:
    data = resource(["corpus"])
    current = data["versions"][0]
    current["components"] = [
        {
            "id": "comp-main",
            "name": "Main",
            "description": UNKNOWN,
            "parent_component_id": NOT_APPLICABLE,
            "language_stage_ids": known(["mhg"]),
            "chronology": UNKNOWN,
            "geography": UNKNOWN,
            "genres": UNKNOWN,
            "text_types": UNKNOWN,
            "document_ids": ["doc-one"],
            "size_measurements": [],
            "extensions": {},
        }
    ]
    current["documents"] = [
        {
            "id": "doc-one",
            "title": UNKNOWN,
            "component_ids": ["comp-main"],
            "work_ids": [],
            "witness_ids": [],
            "edition_witness_ids": [],
            "external_identifiers": [],
            "authorship": UNKNOWN,
            "language_stage_ids": known(["mhg"]),
            "chronology": UNKNOWN,
            "geography": UNKNOWN,
            "genres": UNKNOWN,
            "text_types": UNKNOWN,
            "language_mixture": UNKNOWN,
            "stable_segment_identifier_types": UNKNOWN,
            "size_measurements": [],
            "extensions": {},
        }
    ]
    assert Resource.model_validate(data).versions[0].documents[0].id == "doc-one"

    broken = deepcopy(data)
    broken["versions"][0]["documents"][0]["component_ids"] = ["comp-other"]
    with pytest.raises(ValidationError, match="containing version"):
        Resource.model_validate(broken)


def test_annotation_scope_rejects_foreign_version_component_and_bad_id() -> None:
    with pytest.raises(ValidationError, match="'ann-' prefix"):
        AnnotationLayer.model_validate(annotation("doc-wrong", "unknown", scope()))

    data = resource(["corpus"])
    data["versions"][0]["annotations"] = [
        annotation(
            "ann-example",
            "unknown",
            scope(version_ids=["ver-foreign"]),
        )
    ]
    with pytest.raises(ValidationError, match="containing version"):
        Resource.model_validate(data)


def test_strict_dates_and_measurement_ids_are_rejected() -> None:
    data = resource(["corpus"])
    data["versions"][0]["release_date"] = known("2026-08-11")
    with pytest.raises(ValidationError, match="date"):
        Resource.model_validate(data)

    invalid = measurement("locally_computed") | {"version_id": "res-wrong"}
    with pytest.raises(ValidationError, match="'ver-' prefix"):
        SizeMeasurement.model_validate(invalid)


def test_dataset_use_requires_resource_xor_external_identity() -> None:
    base = {"scope": UNKNOWN, "note": UNKNOWN}
    DatasetUse.model_validate(
        base | {"resource_id": known("res-data"), "external_name": UNKNOWN}
    )
    DatasetUse.model_validate(
        base | {"resource_id": UNKNOWN, "external_name": known("External data")}
    )
    with pytest.raises(ValidationError, match="exactly one"):
        DatasetUse.model_validate(
            base
            | {
                "resource_id": known("res-data"),
                "external_name": known("External data"),
            }
        )
    with pytest.raises(ValidationError, match="exactly one"):
        DatasetUse.model_validate(
            base | {"resource_id": UNKNOWN, "external_name": UNKNOWN}
        )


def test_tool_metric_preserves_dataset_identity_scope_and_value() -> None:
    metric = EvaluationMetric.model_validate(
        {
            "name": "Accuracy",
            "value": known(0.91),
            "scale": known("proportion"),
            "task": known("lemma"),
            "dataset": known(
                {
                    "resource_id": UNKNOWN,
                    "external_name": known("Synthetic evaluation set"),
                    "scope": UNKNOWN,
                    "note": UNKNOWN,
                }
            ),
            "scope": known(scope()),
            "note": UNKNOWN,
        }
    )
    assert metric.value.value == 0.91  # type: ignore[union-attr]
    assert EvaluationMetric.model_validate_json(metric.model_dump_json()) == metric


def test_dictionary_machine_readable_availability_requires_real_access() -> None:
    data = resource(["dictionary"])
    profile = data["dictionary"]["value"]
    profile["machine_readable_availability"] = "available"
    with pytest.raises(ValidationError, match="download URL or API"):
        Resource.model_validate(data)

    profile["api"] = known(
        {
            "base_url": "https://example.invalid/api",
            "documentation_url": UNKNOWN,
            "capabilities": known(["lookup"]),
        }
    )
    assert Resource.model_validate(data).dictionary.status == "known"


def test_dictionary_citations_occurrences_and_suitability_are_independent() -> None:
    data = resource(["dictionary"])
    profile = data["dictionary"]["value"]
    profile["citation_structure"] = known(
        {
            "works": known(True),
            "editions": known(True),
            "pages": UNKNOWN,
            "lines": UNKNOWN,
            "passages": known(True),
            "note": UNKNOWN,
        }
    )
    profile["corpus_occurrence_links"] = known(
        {
            "available": known(True),
            "resource_ids": ["res-corpus"],
            "link_unit": known("passage"),
        }
    )
    profile["supervision_suitability"] = known(
        [
            {
                "task": "lemmatization",
                "status": "review_required",
                "scope": scope(),
                "reasons": known(["Linked lemmas are present."]),
                "limitations": known(["Permission remains unclear."]),
                "quality": UNKNOWN,
                "model_training_permission": "unclear",
                "evidence_ids": ["evidence-example"],
            }
        ]
    )
    parsed = Resource.model_validate(data)
    dictionary = parsed.dictionary.value  # type: ignore[union-attr]
    assert dictionary.citation_structure.value.passages.value is True  # type: ignore[union-attr]
    assert dictionary.corpus_occurrence_links.value.resource_ids == {  # type: ignore[union-attr]
        "res-corpus"
    }


def test_required_resource_and_profile_fields_cannot_be_omitted() -> None:
    data = resource(["corpus"])
    del data["record_reviewed_on"]
    with pytest.raises(ValidationError, match="record_reviewed_on"):
        Resource.model_validate(data)

    data = resource(["corpus"])
    del data["corpus"]["value"]["text_layers"]
    with pytest.raises(ValidationError, match="text_layers"):
        Resource.model_validate(data)


@pytest.mark.parametrize("section", ["12.1 Corpus", "12.2 NLP tool", "12.3 Dictionary"])
def test_complete_approved_design_yaml_round_trips(section: str) -> None:
    design = (REPOSITORY_ROOT / "docs" / "data-model.md").read_text(encoding="utf-8")
    section_text = design.split(f"### {section}", maxsplit=1)[1]
    match = re.search(r"```yaml\n(.*?)\n```", section_text, re.DOTALL)
    assert match is not None

    parsed = Resource.model_validate(yaml.safe_load(match.group(1)))
    assert Resource.model_validate_json(parsed.model_dump_json()) == parsed


@pytest.mark.parametrize(
    "relative_path",
    [
        Path("inventory/resources/corpora/rem.yaml"),
        Path("inventory/resources/tools/rnntagger.yaml"),
        Path("inventory/resources/dictionaries/mwb.yaml"),
    ],
)
def test_representative_resource_records_map_and_round_trip(
    relative_path: Path,
) -> None:
    data = yaml.safe_load((REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8"))
    parsed = Resource.model_validate(data)
    assert Resource.model_validate_json(parsed.model_dump_json()) == parsed

    report = provenance_completeness_report(parsed)
    assert {issue.error_code for issue in report.errors} <= {"missing_claim"}
