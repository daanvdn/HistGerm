"""Strict access, distribution, and permission models."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field, field_serializer, field_validator, model_validator

from histgerm.models.common import (
    ExtensionData,
    HistGermModel,
    HttpUrlValue,
    KnowledgeValue,
    KnownValue,
    NonEmptyStr,
    NotApplicableValue,
    NotPubliclyAvailableValue,
    RegistryId,
    SelectionScope,
    StableId,
    VocabularyId,
)

type AvailabilityState = Literal[
    "available",
    "partially_available",
    "request_only",
    "authentication_required",
    "temporarily_unavailable",
    "discontinued",
    "inaccessible",
    "not_publicly_available",
    "unknown",
]
type AccessRequirement = Literal[
    "none",
    "account",
    "authentication",
    "click_through_agreement",
    "negotiated_agreement",
    "unknown",
    "not_applicable",
]
type PermissionState = Literal[
    "permitted",
    "prohibited",
    "unclear",
    "not_applicable",
]
type LicenseStatus = Literal[
    "declared_standard",
    "declared_custom",
    "multiple",
    "no_license_declared",
    "unknown",
    "not_applicable",
    "not_publicly_available",
]
type LicenseScope = Literal[
    "software",
    "model",
    "original_data",
    "processed_data",
    "annotations",
    "metadata",
    "documentation",
    "website_content",
    "entire_distribution",
    "mixed",
    "unknown",
]

_ACTIVE_AVAILABILITY = frozenset(
    {
        "available",
        "partially_available",
        "request_only",
        "authentication_required",
    }
)
_SPECIFIC_ACCESS_REQUIREMENTS = frozenset(
    {
        "account",
        "authentication",
        "click_through_agreement",
        "negotiated_agreement",
    }
)


def _known(value: object) -> bool:
    return isinstance(value, KnownValue)


def _knowledge_status(value: KnowledgeValue[Any]) -> str:
    return value.status


def _sequence_to_frozenset(value: Any) -> Any:
    if isinstance(value, list):
        return frozenset(value)
    return value


class AccessUrl(HistGermModel):
    kind: VocabularyId
    url: HttpUrlValue


class FormatDescription(HistGermModel):
    format_id: VocabularyId
    format_version: KnowledgeValue[NonEmptyStr]
    schema_name: KnowledgeValue[NonEmptyStr]
    schema_version: KnowledgeValue[NonEmptyStr]
    schema_url: KnowledgeValue[HttpUrlValue]
    media_type: KnowledgeValue[NonEmptyStr]
    encoding: KnowledgeValue[NonEmptyStr]
    profile_or_dialect: KnowledgeValue[NonEmptyStr]
    inner_format_ids: KnowledgeValue[frozenset[VocabularyId]]

    @field_validator("inner_format_ids", mode="before")
    @classmethod
    def accept_yaml_sequence(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and value.get("status") == "known":
            return {**value, "value": _sequence_to_frozenset(value.get("value"))}
        return value

    @field_serializer("inner_format_ids", when_used="json")
    def serialize_inner_formats(
        self, value: KnowledgeValue[frozenset[VocabularyId]]
    ) -> Any:
        if isinstance(value, KnownValue):
            return {"status": "known", "value": sorted(value.value)}
        return {"status": value.status}


class LicenseDescription(HistGermModel):
    status: LicenseStatus
    license_id: KnowledgeValue[RegistryId]
    name: KnowledgeValue[NonEmptyStr]
    url: KnowledgeValue[HttpUrlValue]
    scopes: KnowledgeValue[frozenset[LicenseScope]]
    note: KnowledgeValue[NonEmptyStr]

    @field_validator("scopes", mode="before")
    @classmethod
    def accept_yaml_sequence(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and value.get("status") == "known":
            return {**value, "value": _sequence_to_frozenset(value.get("value"))}
        return value

    @model_validator(mode="after")
    def validate_legal_structure(self) -> LicenseDescription:
        if self.status == "declared_standard":
            if not (_known(self.license_id) or _known(self.name)):
                raise ValueError(
                    "declared_standard license requires a known name or license_id"
                )
        elif self.status == "declared_custom":
            if not (_known(self.name) or _known(self.url)):
                raise ValueError("declared_custom license requires a known name or URL")
            if not _known(self.scopes):
                raise ValueError(
                    "declared_custom license requires known license scopes"
                )
        elif self.status == "multiple":
            if not _known(self.scopes):
                raise ValueError("multiple licenses require known license scopes")
            if not _known(self.note):
                raise ValueError(
                    "multiple licenses require a known note describing the split"
                )
        elif self.status in {"no_license_declared", "unknown"}:
            contradictory = [
                field
                for field in ("license_id", "name", "url", "scopes")
                if _known(getattr(self, field))
            ]
            if contradictory:
                raise ValueError(
                    f"{self.status} license cannot have known "
                    + ", ".join(contradictory)
                )
        elif self.status == "not_applicable":
            self._require_detail_status("not_applicable", NotApplicableValue)
        elif self.status == "not_publicly_available":
            self._require_detail_status(
                "not_publicly_available", NotPubliclyAvailableValue
            )

        if (
            isinstance(self.scopes, KnownValue)
            and "mixed" in self.scopes.value
            and not isinstance(self.note, KnownValue)
        ):
            raise ValueError("mixed license scope requires a known explanatory note")
        return self

    def _require_detail_status(
        self,
        status: str,
        expected_type: type[NotApplicableValue] | type[NotPubliclyAvailableValue],
    ) -> None:
        mismatched = [
            field
            for field in ("license_id", "name", "url", "scopes")
            if not isinstance(getattr(self, field), expected_type)
        ]
        if mismatched:
            raise ValueError(
                f"{status} license requires matching detail states for "
                + ", ".join(mismatched)
            )

    @field_serializer("scopes", when_used="json")
    def serialize_scopes(self, value: KnowledgeValue[frozenset[LicenseScope]]) -> Any:
        if isinstance(value, KnownValue):
            return {"status": "known", "value": sorted(value.value)}
        return {"status": value.status}


class PackageInterface(HistGermModel):
    ecosystem: NonEmptyStr
    name: NonEmptyStr
    version_constraint: KnowledgeValue[NonEmptyStr]
    install_url: KnowledgeValue[HttpUrlValue]


class CliInterface(HistGermModel):
    command: NonEmptyStr
    usage_url: KnowledgeValue[HttpUrlValue]


class ApiInterface(HistGermModel):
    base_url: HttpUrlValue
    documentation_url: KnowledgeValue[HttpUrlValue]
    capabilities: KnowledgeValue[frozenset[NonEmptyStr]]

    @field_validator("capabilities", mode="before")
    @classmethod
    def accept_yaml_sequence(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and value.get("status") == "known":
            return {**value, "value": _sequence_to_frozenset(value.get("value"))}
        return value

    @field_serializer("capabilities", when_used="json")
    def serialize_capabilities(
        self, value: KnowledgeValue[frozenset[NonEmptyStr]]
    ) -> Any:
        if isinstance(value, KnownValue):
            return {"status": "known", "value": sorted(value.value)}
        return {"status": value.status}


class HuggingFaceReference(HistGermModel):
    url: HttpUrlValue
    repository_id: NonEmptyStr


class ArtifactMetadata(HistGermModel):
    file_name: KnowledgeValue[NonEmptyStr]
    media_type: KnowledgeValue[NonEmptyStr]
    byte_size: KnowledgeValue[int]
    checksum_algorithm: KnowledgeValue[NonEmptyStr]
    checksum: KnowledgeValue[NonEmptyStr]

    @model_validator(mode="after")
    def validate_artifact(self) -> ArtifactMetadata:
        if isinstance(self.byte_size, KnownValue) and self.byte_size.value <= 0:
            raise ValueError("known artifact byte_size must be a positive integer")

        algorithm_known = _known(self.checksum_algorithm)
        checksum_known = _known(self.checksum)
        if algorithm_known != checksum_known:
            raise ValueError(
                "checksum_algorithm and checksum must either both be known "
                "or both be missing"
            )
        if not algorithm_known and _knowledge_status(
            self.checksum_algorithm
        ) != _knowledge_status(self.checksum):
            raise ValueError(
                "missing checksum_algorithm and checksum must use the same "
                "knowledge status"
            )
        return self


class AccessPolicy(HistGermModel):
    public_description: AvailabilityState
    online_browsing: AvailabilityState
    download: AvailabilityState
    api_access: AvailabilityState
    request_only: AvailabilityState
    authentication_or_agreement: AccessRequirement
    automated_access: PermissionState
    model_training: PermissionState
    original_redistribution: PermissionState
    processed_redistribution: PermissionState
    trained_weights_publication: PermissionState

    @model_validator(mode="after")
    def validate_access_relationships(self) -> AccessPolicy:
        facets = {
            "public_description": self.public_description,
            "online_browsing": self.online_browsing,
            "download": self.download,
            "api_access": self.api_access,
            "request_only": self.request_only,
        }
        authenticated = sorted(
            name for name, state in facets.items() if state == "authentication_required"
        )
        if authenticated and (
            self.authentication_or_agreement not in _SPECIFIC_ACCESS_REQUIREMENTS
        ):
            raise ValueError(
                "authentication_required access facets require account, "
                "authentication, click_through_agreement, or "
                "negotiated_agreement; affected: " + ", ".join(authenticated)
            )

        if self.authentication_or_agreement == "not_applicable" and any(
            state in _ACTIVE_AVAILABILITY
            for name, state in facets.items()
            if name != "public_description"
        ):
            raise ValueError(
                "authentication_or_agreement cannot be not_applicable when an "
                "access channel is available"
            )

        if self.download == "request_only" and (
            self.request_only not in _ACTIVE_AVAILABILITY
        ):
            raise ValueError(
                "request-only download requires an available request_only channel"
            )
        if self.request_only == "request_only" and self.download in {
            "available",
            "partially_available",
        }:
            raise ValueError(
                "request_only access cannot coexist with directly available download"
            )
        return self


class Distribution(HistGermModel):
    id: StableId
    kind: VocabularyId
    title: KnowledgeValue[NonEmptyStr]
    scope: SelectionScope
    access_urls: KnowledgeValue[list[AccessUrl]]
    formats: KnowledgeValue[list[FormatDescription]]
    license: LicenseDescription
    availability: AvailabilityState
    access: AccessPolicy
    attribution_requirement: KnowledgeValue[NonEmptyStr]
    citation_requirement: KnowledgeValue[NonEmptyStr]
    package: KnowledgeValue[PackageInterface]
    cli: KnowledgeValue[CliInterface]
    api: KnowledgeValue[ApiInterface]
    hugging_face: KnowledgeValue[HuggingFaceReference]
    artifact: KnowledgeValue[ArtifactMetadata]
    extensions: ExtensionData = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def validate_distribution_id(cls, value: StableId) -> StableId:
        if not value.startswith("dist-"):
            raise ValueError("distribution id must use the 'dist-' prefix")
        return value

    @model_validator(mode="after")
    def validate_availability_consistency(self) -> Distribution:
        if (
            self.availability == "not_publicly_available"
            and self.access.download in _ACTIVE_AVAILABILITY
        ):
            raise ValueError(
                "not_publicly_available distribution cannot have an available "
                "download facet"
            )
        if (
            self.availability == "request_only"
            and self.access.request_only not in _ACTIVE_AVAILABILITY
        ):
            raise ValueError(
                "request_only distribution requires an available request_only facet"
            )
        if (
            self.availability == "authentication_required"
            and self.access.authentication_or_agreement
            not in _SPECIFIC_ACCESS_REQUIREMENTS
        ):
            raise ValueError(
                "authentication_required distribution requires a specific "
                "authentication_or_agreement value"
            )
        return self


__all__ = [
    "AccessPolicy",
    "AccessRequirement",
    "AccessUrl",
    "ApiInterface",
    "ArtifactMetadata",
    "AvailabilityState",
    "CliInterface",
    "Distribution",
    "FormatDescription",
    "HuggingFaceReference",
    "LicenseDescription",
    "LicenseScope",
    "LicenseStatus",
    "PackageInterface",
    "PermissionState",
]
