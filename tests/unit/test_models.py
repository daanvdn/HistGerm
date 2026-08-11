from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from histgerm.models.common import (
    DateRange,
    EntityReference,
    ExtensionData,
    ExternalIdentifier,
    GeographicCoverage,
    HistGermModel,
    HttpUrlValue,
    JsonPointer,
    JsonValue,
    KnowledgeValue,
    KnownValue,
    LocalizedName,
    NonEmptyStr,
    ResponsibleParty,
    SelectionScope,
    StableId,
    UnknownValue,
    VocabularyId,
)

UNKNOWN = {"status": "unknown"}


@pytest.mark.parametrize(
    "value",
    [
        "res-example",
        "ver-example-1",
        "dist-example",
        "comp-example",
        "work-example",
        "wit-example",
        "doc-example",
        "ann-example",
        "rel-example",
        "pub-example",
        "evidence-example",
    ],
)
def test_stable_id_accepts_every_approved_prefix(value: str) -> None:
    assert TypeAdapter(StableId).validate_python(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "RES-example",
        "res-Example",
        " res-example",
        "res-example ",
        "example",
        "other-example",
        "res-",
        "res-two--parts",
        "res_two",
    ],
)
def test_stable_id_rejects_unstable_values(value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(StableId).validate_python(value)


@pytest.mark.parametrize("value", ["language_stage", "mhg", "x1_value"])
def test_vocabulary_and_registry_ids_are_snake_case(value: str) -> None:
    assert TypeAdapter(VocabularyId).validate_python(value) == value


@pytest.mark.parametrize(
    "value", ["Language_stage", "language-stage", " language_stage", "", "_x"]
)
def test_vocabulary_id_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(VocabularyId).validate_python(value)


def test_non_empty_string_is_stripped_but_not_coerced() -> None:
    adapter = TypeAdapter(NonEmptyStr)
    assert adapter.validate_python("  value  ") == "value"
    with pytest.raises(ValidationError):
        adapter.validate_python(" \t ")
    with pytest.raises(ValidationError):
        adapter.validate_python(4)


@pytest.mark.parametrize(
    "value",
    [
        "https://example.org",
        "http://example.org:8080/path?q=value#fragment",
        "https://127.0.0.1/resource",
    ],
)
def test_http_url_accepts_absolute_web_urls_without_normalizing(value: str) -> None:
    assert TypeAdapter(HttpUrlValue).validate_python(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "example.org",
        "/relative",
        "ftp://example.org/file",
        "file:///local/path",
        "https:///missing-host",
        "https://example.org/a b",
        " https://example.org",
    ],
)
def test_http_url_rejects_invalid_or_unsafe_urls(value: str) -> None:
    with pytest.raises(ValidationError, match="URL|url|http"):
        TypeAdapter(HttpUrlValue).validate_python(value)


@pytest.mark.parametrize("value", ["", "/", "/name", "/a~1b", "/m~0n", "/items/0/name"])
def test_json_pointer_accepts_rfc_6901_syntax(value: str) -> None:
    assert TypeAdapter(JsonPointer).validate_python(value) == value


@pytest.mark.parametrize("value", ["name", "#/name", "/bad~", "/bad~2escape"])
def test_json_pointer_rejects_non_absolute_or_bad_escapes(value: str) -> None:
    with pytest.raises(ValidationError, match="RFC 6901"):
        TypeAdapter(JsonPointer).validate_python(value)


@pytest.mark.parametrize(
    "value",
    [
        {"status": "known", "value": "fact"},
        {"status": "unknown"},
        {"status": "not_applicable"},
        {"status": "not_publicly_available"},
    ],
)
def test_knowledge_value_accepts_all_valid_states(value: dict[str, Any]) -> None:
    adapter: TypeAdapter[KnowledgeValue[NonEmptyStr]] = TypeAdapter(
        KnowledgeValue[NonEmptyStr]
    )
    parsed = adapter.validate_python(value)
    assert adapter.validate_json(adapter.dump_json(parsed)) == parsed


@pytest.mark.parametrize(
    "value",
    [
        {"status": "known"},
        {"status": "known", "value": ""},
        {"status": "known", "value": []},
        {"status": "unknown", "value": "forbidden"},
        {"status": "not_applicable", "value": "forbidden"},
        {"status": "not_publicly_available", "value": "forbidden"},
        {"status": "other"},
        {},
    ],
)
def test_knowledge_value_rejects_every_invalid_state_combination(
    value: dict[str, Any],
) -> None:
    adapter: TypeAdapter[KnowledgeValue[list[NonEmptyStr]]] = TypeAdapter(
        KnowledgeValue[list[NonEmptyStr]]
    )
    with pytest.raises(ValidationError):
        adapter.validate_python(value)


def test_common_models_reject_extra_keys_and_are_frozen() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        LocalizedName.model_validate(
            {
                "text": "Name",
                "language": UNKNOWN,
                "name_type": "official",
                "unexpected": True,
            }
        )
    name = LocalizedName(
        text="Name",
        language=UnknownValue(status="unknown"),
        name_type="official",
    )
    with pytest.raises(ValidationError, match="frozen"):
        name.text = "Changed"  # type: ignore[misc]


def test_names_parties_and_external_ids_round_trip() -> None:
    party = ResponsibleParty(
        name="Example Institute",
        party_type="organization",
        role="maintainer",
        identifier=KnownValue[ExternalIdentifier](
            status="known",
            value=ExternalIdentifier(
                scheme="ROR",
                value="https://ror.org/012345678",
                resolver_url=KnownValue[str](
                    status="known",
                    value="https://ror.org/012345678",
                ),
            ),
        ),
    )
    assert ResponsibleParty.model_validate_json(party.model_dump_json()) == party

    external = party.identifier
    assert isinstance(external, KnownValue)
    assert isinstance(external.value, ExternalIdentifier)


def _date_range(earliest: int | None, latest: int | None) -> DateRange:
    unknown = UnknownValue(status="unknown")
    return DateRange(
        earliest_year=(
            KnownValue[int](status="known", value=earliest)
            if earliest is not None
            else unknown
        ),
        latest_year=(
            KnownValue[int](status="known", value=latest)
            if latest is not None
            else unknown
        ),
        label=unknown,
        dating_method=unknown,
        certainty=KnownValue[str](status="known", value="probable"),
    )


def test_date_range_accepts_ordered_signed_years_and_unknown_endpoint() -> None:
    earliest = _date_range(-750, 1050).earliest_year
    latest = _date_range(None, 1050).latest_year
    assert isinstance(earliest, KnownValue)
    assert isinstance(latest, KnownValue)
    assert earliest.value == -750
    assert latest.value == 1050


def test_date_range_rejects_reversed_known_endpoints() -> None:
    with pytest.raises(ValidationError, match="earliest_year"):
        _date_range(1200, 1100)


def test_geography_accepts_deduplicated_yaml_sequences_and_round_trips() -> None:
    geography = GeographicCoverage.model_validate(
        {
            "region_ids": {
                "status": "known",
                "value": ["upper_rhine", "bavaria", "upper_rhine"],
            },
            "dialect_ids": UNKNOWN,
            "certainty": {"status": "known", "value": "probable"},
            "note": UNKNOWN,
        }
    )
    assert isinstance(geography.region_ids, KnownValue)
    assert geography.region_ids.value == frozenset({"bavaria", "upper_rhine"})
    dumped = geography.model_dump_json()
    assert json.loads(dumped)["region_ids"]["value"] == ["bavaria", "upper_rhine"]
    assert GeographicCoverage.model_validate_json(dumped) == geography


@pytest.mark.parametrize(
    ("entity_type", "identifier"),
    [
        ("resource", "res-example"),
        ("version", "ver-example"),
        ("distribution", "dist-example"),
        ("component", "comp-example"),
        ("work", "work-example"),
        ("witness", "wit-example"),
        ("document", "doc-example"),
        ("annotation", "ann-example"),
        ("publication", "pub-example"),
    ],
)
def test_entity_reference_accepts_matching_prefix(
    entity_type: str, identifier: str
) -> None:
    reference = EntityReference.model_validate(
        {"entity_type": entity_type, "id": identifier}
    )
    assert reference.id == identifier


def test_entity_reference_rejects_wrong_prefix() -> None:
    with pytest.raises(ValidationError, match="must use the 'work-' prefix"):
        EntityReference(entity_type="work", id="res-example")


def test_selection_scope_requires_ids_or_known_filter_and_checks_prefixes() -> None:
    scope = SelectionScope.model_validate(
        {
            "resource_ids": ["res-b", "res-a", "res-a"],
            "filter": UNKNOWN,
        }
    )
    assert scope.resource_ids == frozenset({"res-a", "res-b"})
    assert json.loads(scope.model_dump_json())["resource_ids"] == [
        "res-a",
        "res-b",
    ]
    assert SelectionScope.model_validate_json(scope.model_dump_json()) == scope

    filtered = SelectionScope.model_validate(
        {"filter": {"status": "known", "value": {"language_stage": "mhg"}}}
    )
    assert isinstance(filtered.filter, KnownValue)

    unknown = UnknownValue(status="unknown")
    with pytest.raises(ValidationError, match="at least one selected ID"):
        SelectionScope(filter=unknown)
    with pytest.raises(ValidationError, match="resource_ids entries"):
        SelectionScope(resource_ids=frozenset({"ver-wrong"}), filter=unknown)
    empty_filter: dict[str, JsonValue] = {}
    with pytest.raises(ValidationError):
        SelectionScope(
            filter=KnownValue[dict[str, JsonValue]](
                status="known",
                value=empty_filter,
            )
        )


def test_extensions_accept_namespaced_json_and_round_trip() -> None:
    value = {
        "org.example.experimental": {
            "confidence_score": 0.75,
            "flags": [True, False, None],
            "details": {" revision label ": "  preserved verbatim  "},
        }
    }
    adapter: TypeAdapter[ExtensionData] = TypeAdapter(
        ExtensionData, config=HistGermModel.model_config
    )
    parsed = adapter.validate_python(value)
    assert parsed == value
    assert adapter.validate_json(adapter.dump_json(parsed)) == parsed


@pytest.mark.parametrize(
    "value",
    [
        {"experimental": {"field": True}},
        {"Org.Example": {"field": True}},
        {"org.example": {"payload": "embedded"}},
        {"org.example": {"nested": {"content": "forbidden"}}},
        {"org.example": {"items": [{"local_path": "C:\\data"}]}},
        {"org.example": {"number": float("nan")}},
        {"org.example": {"value": b"bytes"}},
    ],
)
def test_extensions_reject_bad_namespaces_payload_keys_and_non_json_values(
    value: dict[str, Any],
) -> None:
    adapter: TypeAdapter[ExtensionData] = TypeAdapter(
        ExtensionData, config=HistGermModel.model_config
    )
    with pytest.raises(ValidationError):
        adapter.validate_python(value)


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        4,
        1.25,
        "text",
        [1, "two", False],
        {"nested": [None, 3]},
        {" spaced key ": "  spaced value  "},
    ],
)
def test_json_values_round_trip(value: JsonValue) -> None:
    adapter: TypeAdapter[JsonValue] = TypeAdapter(
        JsonValue, config=HistGermModel.model_config
    )
    parsed = adapter.validate_python(value)
    assert adapter.validate_json(adapter.dump_json(parsed)) == parsed


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), b"x"])
def test_json_values_reject_non_json_values(value: Any) -> None:
    adapter: TypeAdapter[JsonValue] = TypeAdapter(
        JsonValue, config=HistGermModel.model_config
    )
    with pytest.raises(ValidationError):
        adapter.validate_python(value)


# --- P2-ACCESS-MODELS: access agent owned section ---

from histgerm.models.access import (  # noqa: E402
    AccessPolicy,
    ApiInterface,
    ArtifactMetadata,
    AvailabilityState,
    Distribution,
    FormatDescription,
    LicenseDescription,
    PermissionState,
)

NOT_APPLICABLE = {"status": "not_applicable"}
NOT_PUBLIC = {"status": "not_publicly_available"}


def _access_policy(**changes: str) -> AccessPolicy:
    values = {
        "public_description": "available",
        "online_browsing": "unknown",
        "download": "available",
        "api_access": "unknown",
        "request_only": "unknown",
        "authentication_or_agreement": "none",
        "automated_access": "unclear",
        "model_training": "unclear",
        "original_redistribution": "unclear",
        "processed_redistribution": "unclear",
        "trained_weights_publication": "unclear",
    }
    values.update(changes)
    return AccessPolicy.model_validate(values)


def _license(**changes: Any) -> LicenseDescription:
    values: dict[str, Any] = {
        "status": "declared_standard",
        "license_id": {"status": "known", "value": "cc_by_4_0"},
        "name": {"status": "known", "value": "CC BY 4.0"},
        "url": {"status": "known", "value": "https://example.org/license"},
        "scopes": {
            "status": "known",
            "value": ["metadata", "original_data"],
        },
        "note": UNKNOWN,
    }
    values.update(changes)
    return LicenseDescription.model_validate(values)


def _distribution(**changes: Any) -> Distribution:
    values: dict[str, Any] = {
        "id": "dist-example",
        "kind": "project_download",
        "title": {"status": "known", "value": "Example release"},
        "scope": {"resource_ids": ["res-example"], "filter": UNKNOWN},
        "access_urls": {
            "status": "known",
            "value": [
                {
                    "kind": "download",
                    "url": "https://example.org/download",
                }
            ],
        },
        "formats": UNKNOWN,
        "license": _license(),
        "availability": "available",
        "access": _access_policy(),
        "attribution_requirement": UNKNOWN,
        "citation_requirement": UNKNOWN,
        "package": NOT_APPLICABLE,
        "cli": NOT_APPLICABLE,
        "api": NOT_APPLICABLE,
        "hugging_face": NOT_APPLICABLE,
        "artifact": UNKNOWN,
    }
    values.update(changes)
    return Distribution.model_validate(values)


@pytest.mark.parametrize(
    "value",
    [
        "available",
        "partially_available",
        "request_only",
        "authentication_required",
        "temporarily_unavailable",
        "discontinued",
        "inaccessible",
        "not_publicly_available",
        "unknown",
    ],
)
def test_access_availability_states_are_exact(value: str) -> None:
    assert TypeAdapter(AvailabilityState).validate_python(value) == value


@pytest.mark.parametrize(
    "value", ["permitted", "prohibited", "unclear", "not_applicable"]
)
def test_access_permission_states_round_trip_distinctly(value: str) -> None:
    adapter: TypeAdapter[PermissionState] = TypeAdapter(PermissionState)
    parsed = adapter.validate_python(value)
    assert adapter.validate_json(adapter.dump_json(parsed)) == value


@pytest.mark.parametrize(
    ("adapter_type", "value"),
    [
        (AvailabilityState, "not_available"),
        (AvailabilityState, "not_applicable"),
        (PermissionState, "unknown"),
        (PermissionState, "allowed"),
        (PermissionState, True),
    ],
)
def test_access_closed_states_reject_unapproved_values(
    adapter_type: Any, value: Any
) -> None:
    adapter: TypeAdapter[Any] = TypeAdapter(adapter_type)
    with pytest.raises(ValidationError):
        adapter.validate_python(value)


def test_access_policy_keeps_every_permission_separate_and_required() -> None:
    policy = _access_policy(model_training="permitted")
    assert policy.automated_access == "unclear"
    assert policy.model_training == "permitted"
    assert policy.original_redistribution == "unclear"
    dumped = json.loads(policy.model_dump_json())
    assert dumped["model_training"] == "permitted"
    assert dumped["automated_access"] == "unclear"

    values = dumped | {"model_training": None}
    with pytest.raises(ValidationError):
        AccessPolicy.model_validate(values)
    del dumped["model_training"]
    with pytest.raises(ValidationError, match="model_training"):
        AccessPolicy.model_validate(dumped)


def test_access_policy_rejects_authentication_without_requirement() -> None:
    with pytest.raises(ValidationError, match="authentication_required access facets"):
        _access_policy(
            download="authentication_required",
            authentication_or_agreement="unknown",
        )

    policy = _access_policy(
        download="authentication_required",
        authentication_or_agreement="account",
    )
    assert policy.authentication_or_agreement == "account"


def test_access_policy_rejects_request_and_requirement_contradictions() -> None:
    with pytest.raises(ValidationError, match="request-only download"):
        _access_policy(download="request_only", request_only="unknown")
    with pytest.raises(ValidationError, match="directly available download"):
        _access_policy(download="available", request_only="request_only")
    with pytest.raises(ValidationError, match="cannot be not_applicable"):
        _access_policy(authentication_or_agreement="not_applicable")


def test_format_and_api_sets_deduplicate_sort_and_round_trip() -> None:
    description = FormatDescription.model_validate(
        {
            "format_id": "tei_xml",
            "format_version": UNKNOWN,
            "schema_name": UNKNOWN,
            "schema_version": UNKNOWN,
            "schema_url": UNKNOWN,
            "media_type": {"status": "known", "value": "application/xml"},
            "encoding": {"status": "known", "value": "UTF-8"},
            "profile_or_dialect": UNKNOWN,
            "inner_format_ids": {
                "status": "known",
                "value": ["plain_text", "xml", "plain_text"],
            },
        }
    )
    assert json.loads(description.model_dump_json())["inner_format_ids"]["value"] == [
        "plain_text",
        "xml",
    ]
    assert FormatDescription.model_validate_json(description.model_dump_json()) == (
        description
    )

    interface = ApiInterface.model_validate(
        {
            "base_url": "https://api.example.org",
            "documentation_url": UNKNOWN,
            "capabilities": {
                "status": "known",
                "value": ["search", "concordance", "search"],
            },
        }
    )
    assert json.loads(interface.model_dump_json())["capabilities"]["value"] == [
        "concordance",
        "search",
    ]


def test_artifact_requires_positive_size_and_consistent_checksum_pair() -> None:
    artifact = ArtifactMetadata.model_validate(
        {
            "file_name": {"status": "known", "value": "release.zip"},
            "media_type": {"status": "known", "value": "application/zip"},
            "byte_size": {"status": "known", "value": 42},
            "checksum_algorithm": {"status": "known", "value": "SHA-256"},
            "checksum": {"status": "known", "value": "abc123"},
        }
    )
    assert ArtifactMetadata.model_validate_json(artifact.model_dump_json()) == artifact

    with pytest.raises(ValidationError, match="positive integer"):
        ArtifactMetadata.model_validate(
            {
                "file_name": UNKNOWN,
                "media_type": UNKNOWN,
                "byte_size": {"status": "known", "value": 0},
                "checksum_algorithm": UNKNOWN,
                "checksum": UNKNOWN,
            }
        )
    with pytest.raises(ValidationError, match="either both be known"):
        ArtifactMetadata.model_validate(
            {
                "file_name": UNKNOWN,
                "media_type": UNKNOWN,
                "byte_size": UNKNOWN,
                "checksum_algorithm": {"status": "known", "value": "SHA-256"},
                "checksum": UNKNOWN,
            }
        )
    with pytest.raises(ValidationError, match="same knowledge status"):
        ArtifactMetadata.model_validate(
            {
                "file_name": UNKNOWN,
                "media_type": UNKNOWN,
                "byte_size": UNKNOWN,
                "checksum_algorithm": NOT_APPLICABLE,
                "checksum": UNKNOWN,
            }
        )


def test_license_declared_states_require_explicit_identity() -> None:
    assert _license().scopes.value == frozenset(  # type: ignore[union-attr]
        {"metadata", "original_data"}
    )
    custom = _license(
        status="declared_custom",
        license_id=UNKNOWN,
        name={"status": "known", "value": "Project terms"},
    )
    assert custom.status == "declared_custom"

    registry_only = _license(name=UNKNOWN, url=UNKNOWN, scopes=UNKNOWN)
    assert registry_only.license_id.status == "known"
    with pytest.raises(ValidationError, match="known name or URL"):
        _license(
            status="declared_custom",
            license_id=UNKNOWN,
            name=UNKNOWN,
            url=UNKNOWN,
        )
    with pytest.raises(ValidationError, match="known note"):
        _license(status="multiple")


def test_license_declared_standard_accepts_rem_shape() -> None:
    license_description = _license(
        license_id=UNKNOWN,
        name={
            "status": "known",
            "value": "Creative Commons Attribution-ShareAlike 4.0 International",
        },
        url={
            "status": "known",
            "value": "https://creativecommons.org/licenses/by-sa/4.0/",
        },
        scopes=UNKNOWN,
    )

    assert license_description.status == "declared_standard"
    assert license_description.license_id.status == "unknown"
    assert license_description.scopes.status == "unknown"


def test_license_declared_standard_preserves_unknown_registry_and_scopes() -> None:
    license_description = _license(
        license_id=UNKNOWN,
        name={"status": "known", "value": "Named Standard License 2.0"},
        url=UNKNOWN,
        scopes=UNKNOWN,
    )

    assert license_description.name.status == "known"
    assert license_description.url.status == "unknown"
    assert license_description.scopes.status == "unknown"


def test_license_declared_standard_rejects_underidentified_license() -> None:
    with pytest.raises(ValidationError, match="known name or license_id"):
        _license(
            license_id=UNKNOWN,
            name=UNKNOWN,
            url={"status": "known", "value": "https://example.org/license"},
            scopes=UNKNOWN,
        )


def test_license_missing_states_cannot_hide_known_legal_claims() -> None:
    with pytest.raises(ValidationError, match="unknown license cannot have known"):
        _license(status="unknown")

    not_applicable = _license(
        status="not_applicable",
        license_id=NOT_APPLICABLE,
        name=NOT_APPLICABLE,
        url=NOT_APPLICABLE,
        scopes=NOT_APPLICABLE,
        note=NOT_APPLICABLE,
    )
    assert not_applicable.status == "not_applicable"

    with pytest.raises(ValidationError, match="matching detail states"):
        _license(
            status="not_publicly_available",
            license_id=NOT_PUBLIC,
            name=NOT_PUBLIC,
            url=NOT_PUBLIC,
            scopes=UNKNOWN,
            note=NOT_PUBLIC,
        )


def test_license_mixed_scope_requires_explanation_and_exact_scope_values() -> None:
    with pytest.raises(ValidationError, match="mixed license scope"):
        _license(scopes={"status": "known", "value": ["mixed"]})
    with pytest.raises(ValidationError):
        _license(scopes={"status": "known", "value": ["source_code"]})

    mixed = _license(
        scopes={"status": "known", "value": ["mixed"]},
        note={"status": "known", "value": "Data and code use different terms."},
    )
    assert mixed.status == "declared_standard"


def test_distribution_round_trips_with_closed_access_models() -> None:
    distribution = _distribution()
    assert Distribution.model_validate_json(distribution.model_dump_json()) == (
        distribution
    )
    assert distribution.extensions == {}
    with pytest.raises(ValidationError, match="Extra inputs"):
        Distribution.model_validate(
            distribution.model_dump() | {"local_path": "C:\\data"}
        )
    with pytest.raises(ValidationError, match="'dist-' prefix"):
        _distribution(id="res-wrong")


def test_distribution_rejects_not_public_download_and_request_mismatch() -> None:
    with pytest.raises(
        ValidationError, match="cannot have an available download facet"
    ):
        _distribution(availability="not_publicly_available")
    with pytest.raises(ValidationError, match="available request_only facet"):
        _distribution(availability="request_only")
    with pytest.raises(ValidationError, match="specific authentication_or_agreement"):
        _distribution(
            availability="authentication_required",
            access=_access_policy(download="unknown"),
        )

    request_distribution = _distribution(
        availability="request_only",
        access=_access_policy(
            download="request_only",
            request_only="available",
            authentication_or_agreement="negotiated_agreement",
        ),
    )
    assert request_distribution.availability == "request_only"
