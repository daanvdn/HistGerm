from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from histgerm.models.entities import Authorship, Publication, Witness, Work
from histgerm.models.relationships import OverlapMeasurement, Relationship

UNKNOWN = {"status": "unknown"}
NOT_APPLICABLE = {"status": "not_applicable"}


def party(name: str = "Example Author") -> dict[str, Any]:
    return {
        "name": name,
        "party_type": "person",
        "role": "author",
        "identifier": UNKNOWN,
    }


def authorship() -> dict[str, Any]:
    return {
        "party": party(),
        "attribution_type": "traditional",
        "certainty": {"status": "known", "value": "probable"},
        "note": UNKNOWN,
    }


def work_data(identifier: str = "work-example") -> dict[str, Any]:
    return {
        "id": identifier,
        "canonical_name": "Example Work",
        "authorship": {"status": "known", "value": [authorship()]},
        "chronology": UNKNOWN,
        "language_stage_ids": {
            "status": "known",
            "value": ["mhg", "ohg", "mhg"],
        },
        "genres": UNKNOWN,
        "description": {"status": "known", "value": "A normalized work."},
    }


def witness_data(identifier: str, work_ids: list[str]) -> dict[str, Any]:
    return {
        "id": identifier,
        "canonical_name": f"Witness {identifier}",
        "witness_type": "manuscript",
        "work_ids": work_ids,
        "chronology": UNKNOWN,
        "geography": UNKNOWN,
        "holding_institution": UNKNOWN,
        "description": UNKNOWN,
    }


def scope(version_id: str) -> dict[str, Any]:
    return {"version_ids": [version_id], "filter": UNKNOWN}


def measurement(
    *,
    source_version_id: str = "ver-a",
    target_version_id: str = "ver-b",
    source_scope: dict[str, Any] | None = None,
    target_scope: dict[str, Any] | None = None,
    origin: str = "reported",
    computed_on: dict[str, Any] = NOT_APPLICABLE,
    evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "unit": "token",
        "value": 12,
        "counting_method": "Whitespace-delimited tokens",
        "source_version_id": source_version_id,
        "target_version_id": target_version_id,
        "source_scope": source_scope or scope(source_version_id),
        "target_scope": target_scope or scope(target_version_id),
        "origin": origin,
        "computed_on": computed_on,
        "evidence_ids": evidence_ids or ["evidence-overlap"],
        "uncertainty_note": UNKNOWN,
    }


def relationship_data(
    *,
    kind: str = "overlaps",
    directional: bool = False,
    extent: str = "partial",
    source: tuple[str, str] = ("version", "ver-a"),
    target: tuple[str, str] = ("version", "ver-b"),
    source_scope: dict[str, Any] | None = None,
    target_scope: dict[str, Any] | None = None,
    overlap_measurement: dict[str, Any] | None = None,
    duplicate_group_id: dict[str, Any] = NOT_APPLICABLE,
    canonical_scope: dict[str, Any] = NOT_APPLICABLE,
) -> dict[str, Any]:
    left_scope = source_scope or scope("ver-a")
    right_scope = target_scope or scope("ver-b")
    return {
        "id": "rel-example",
        "source": {"entity_type": source[0], "id": source[1]},
        "target": {"entity_type": target[0], "id": target[1]},
        "kind": kind,
        "directional": directional,
        "source_scope": left_scope,
        "target_scope": right_scope,
        "overlap_extent": extent,
        "overlap_measurement": (
            {"status": "known", "value": overlap_measurement}
            if overlap_measurement is not None
            else NOT_APPLICABLE
        ),
        "certainty": {"status": "known", "value": "certain"},
        "note": UNKNOWN,
        "duplicate_group_id": duplicate_group_id,
        "canonical_scope": canonical_scope,
    }


def test_authorship_and_normalized_entities_round_trip() -> None:
    attribution = Authorship.model_validate(authorship())
    assert Authorship.model_validate_json(attribution.model_dump_json()) == attribution

    work = Work.model_validate(work_data())
    assert work.language_stage_ids.value == frozenset({"mhg", "ohg"})  # type: ignore[union-attr]
    assert Work.model_validate_json(work.model_dump_json()) == work

    publication = Publication.model_validate(
        {
            "id": "pub-example",
            "title": "Example Publication",
            "authors": {"status": "known", "value": [party()]},
            "publication_date": {
                "status": "known",
                "value": date(2025, 1, 2),
            },
            "publication_type": "journal_article",
            "url": {
                "status": "known",
                "value": "https://example.invalid/article",
            },
            "citation": {"status": "known", "value": "Example citation."},
        }
    )
    assert Publication.model_validate_json(publication.model_dump_json()) == publication


def test_recurring_work_is_reused_by_stable_id_across_witnesses() -> None:
    Work.model_validate(work_data("work-shared"))
    first = Witness.model_validate(witness_data("wit-first", ["work-shared"]))
    second = Witness.model_validate(witness_data("wit-second", ["work-shared"]))
    assert first.work_ids == second.work_ids == frozenset({"work-shared"})
    assert Witness.model_validate_json(first.model_dump_json()) == first


@pytest.mark.parametrize(
    ("model", "data", "message"),
    [
        (Work, work_data("wit-wrong"), "work id"),
        (
            Witness,
            witness_data("work-wrong", ["work-example"]),
            "witness id",
        ),
        (
            Witness,
            witness_data("wit-example", ["wit-wrong"]),
            "work_ids",
        ),
    ],
)
def test_entities_enforce_local_identifier_prefixes(
    model: type[Work] | type[Witness],
    data: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        model.model_validate(data)


def test_overlap_measurement_round_trips_and_canonicalizes_evidence() -> None:
    parsed = OverlapMeasurement.model_validate(
        measurement(evidence_ids=["evidence-z", "evidence-a", "evidence-z"])
    )
    assert parsed.evidence_ids == frozenset({"evidence-a", "evidence-z"})
    assert OverlapMeasurement.model_validate_json(parsed.model_dump_json()) == parsed


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"unit": "page"}, "size unit"),
        ({"value": 0}, "greater than 0"),
        ({"source_version_id": "res-a"}, "version IDs"),
        ({"evidence_ids": ["pub-wrong"]}, "evidence_ids"),
        ({"computed_on": UNKNOWN}, "not_applicable"),
        ({"evidence_ids": []}, "evidence ID"),
        (
            {
                "origin": "locally_computed",
                "computed_on": UNKNOWN,
                "evidence_ids": [],
            },
            "known computation date",
        ),
    ],
)
def test_overlap_measurement_rejects_invalid_units_values_ids_and_origin(
    change: dict[str, Any], message: str
) -> None:
    data = measurement()
    data.update(change)
    with pytest.raises(ValidationError, match=message):
        OverlapMeasurement.model_validate(data)


def test_locally_computed_overlap_requires_and_accepts_known_date() -> None:
    parsed = OverlapMeasurement.model_validate(
        measurement(
            origin="locally_computed",
            computed_on={"status": "known", "value": date(2026, 8, 11)},
            evidence_ids=[],
        )
    )
    assert parsed.origin == "locally_computed"


@pytest.mark.parametrize(
    ("kind", "directional", "extent"),
    [
        ("derived_from", True, "unknown"),
        ("contains", True, "contains"),
        ("part_of", True, "contains"),
        ("overlaps", False, "unknown"),
        ("supersedes", True, "unknown"),
        ("trained_on", True, "unknown"),
        ("evaluated_on", True, "unknown"),
        ("annotates", True, "unknown"),
    ],
)
def test_relationship_kind_direction_and_extent_valid_combinations(
    kind: str, directional: bool, extent: str
) -> None:
    parsed = Relationship.model_validate(
        relationship_data(
            kind=kind,
            directional=directional,
            extent=extent,
        )
    )
    assert Relationship.model_validate_json(parsed.model_dump_json()) == parsed


def test_quantified_partial_overlap_validates_both_sides_and_round_trips() -> None:
    data = relationship_data(overlap_measurement=measurement())
    parsed = Relationship.model_validate(data)
    assert Relationship.model_validate_json(parsed.model_dump_json()) == parsed


def test_exact_duplicates_require_group_and_support_canonical_scope() -> None:
    canonical = scope("ver-a")
    parsed = Relationship.model_validate(
        relationship_data(
            kind="duplicates",
            extent="exact",
            duplicate_group_id={"status": "known", "value": "duplicate-group-a"},
            canonical_scope={"status": "known", "value": canonical},
        )
    )
    assert parsed.canonical_scope.value == parsed.source_scope  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (
            relationship_data(target=("version", "ver-a")),
            "source and target",
        ),
        (
            relationship_data(directional=True),
            "must be symmetric",
        ),
        (
            relationship_data(kind="contains", directional=True, extent="partial"),
            "requires overlap_extent 'contains'",
        ),
        (
            relationship_data(kind="duplicates", extent="partial"),
            "requires overlap_extent 'exact'",
        ),
        (
            relationship_data(
                kind="duplicates",
                extent="exact",
                duplicate_group_id=UNKNOWN,
            ),
            "known duplicate_group_id",
        ),
        (
            relationship_data(
                kind="derived_from",
                directional=True,
                extent="unknown",
                duplicate_group_id={"status": "known", "value": "group"},
            ),
            "not_applicable",
        ),
        (
            relationship_data(
                source=("version", "ver-z"),
                target=("version", "ver-a"),
            ),
            "source.id < target.id",
        ),
        (
            relationship_data(
                kind="same_work",
                extent="unknown",
                source=("document", "doc-a"),
                target=("document", "doc-b"),
                source_scope={"document_ids": ["doc-a"], "filter": UNKNOWN},
                target_scope={"document_ids": ["doc-b"], "filter": UNKNOWN},
            ),
            "entity type work",
        ),
    ],
)
def test_relationship_rejects_self_direction_extent_group_order_and_types(
    data: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        Relationship.model_validate(data)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            {"source_scope": scope("ver-other")},
            "source_scope must equal",
        ),
        (
            {"source_version_id": "ver-other"},
            "source_version_id must match",
        ),
    ],
)
def test_quantified_overlap_requires_compatible_scope_and_endpoint_versions(
    change: dict[str, Any], message: str
) -> None:
    quantified = measurement()
    quantified.update(change)
    with pytest.raises(ValidationError, match=message):
        Relationship.model_validate(relationship_data(overlap_measurement=quantified))


def test_canonical_scope_must_be_one_side_and_only_for_exact_deduplication() -> None:
    with pytest.raises(ValidationError, match="equal source_scope or target_scope"):
        Relationship.model_validate(
            relationship_data(
                extent="exact",
                canonical_scope={
                    "status": "known",
                    "value": scope("ver-other"),
                },
            )
        )

    with pytest.raises(ValidationError, match="must be not_applicable"):
        Relationship.model_validate(
            relationship_data(
                canonical_scope={
                    "status": "known",
                    "value": scope("ver-a"),
                }
            )
        )
