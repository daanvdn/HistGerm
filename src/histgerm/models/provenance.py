"""Evidence models and deterministic provenance completeness checks."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    field_serializer,
    model_validator,
)

from histgerm.models.common import (
    ExtensionData,
    HistGermModel,
    HttpUrlValue,
    JsonPointer,
    JsonValue,
    KnowledgeValue,
    NonEmptyStr,
    StableId,
    VocabularyId,
)

_EVIDENCE_ID_PATTERN = re.compile(r"^evidence-[a-z0-9]+(?:-[a-z0-9]+)*$")
_PUBLICATION_ID_PATTERN = re.compile(r"^pub-[a-z0-9]+(?:-[a-z0-9]+)*$")
_JSON_POINTER_PATTERN = re.compile(r"^(?:|/(?:[^~]|~[01])*)$")

_PROVENANCE_FIELDS = frozenset({"evidence", "claims"})
_STRUCTURAL_DISCRIMINATORS = frozenset({"entity_type", "record_type"})
_ROOT_STRUCTURAL_POINTERS = frozenset(
    {"/generated_on", "/inventory_release", "/schema_version"}
)
_OWN_ENTITY_ID_POINTER_PATTERNS = (
    re.compile(r"^/id$"),
    re.compile(r"^/versions/\d+/id$"),
    re.compile(
        r"^/versions/\d+/(?:annotations|components|distributions|documents)/\d+/id$"
    ),
    re.compile(r"^/corpus/value/text_layers/\d+/id$"),
)
_STRUCTURAL_REFERENCE_POINTER_PATTERNS = (re.compile(r"^/(?:.+/)?evidence_ids/\d+$"),)
_KNOWLEDGE_STATES = frozenset(
    {"known", "unknown", "not_applicable", "not_publicly_available"}
)


def _validate_evidence_id(value: StableId) -> StableId:
    if not _EVIDENCE_ID_PATTERN.fullmatch(value):
        raise ValueError("must use the 'evidence-' prefix")
    return value


def _validate_publication_id(value: StableId) -> StableId:
    if not _PUBLICATION_ID_PATTERN.fullmatch(value):
        raise ValueError("must use the 'pub-' prefix")
    return value


EvidenceId = Annotated[StableId, AfterValidator(_validate_evidence_id)]
PublicationId = Annotated[StableId, AfterValidator(_validate_publication_id)]


class EvidenceItem(HistGermModel):
    """Metadata for one successfully inspected supporting source."""

    id: EvidenceId
    source_url: HttpUrlValue
    accessed_on: date
    source_kind: VocabularyId
    quotation: KnowledgeValue[NonEmptyStr]
    note: KnowledgeValue[NonEmptyStr]
    publication_id: KnowledgeValue[PublicationId]
    archived_url: KnowledgeValue[HttpUrlValue]


def _claim_references_to_frozenset(value: Any) -> Any:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return value
    references = list(value)
    if len(references) != len(set(references)):
        raise ValueError("claim evidence references must be duplicate-free")
    return frozenset(references)


NonEmptyEvidenceReferences = Annotated[
    frozenset[EvidenceId],
    BeforeValidator(_claim_references_to_frozenset),
    Field(min_length=1),
]
ClaimMap = dict[JsonPointer, NonEmptyEvidenceReferences]


class ProvenanceIssue(HistGermModel):
    """One deterministic, actionable provenance validation failure."""

    record_id: NonEmptyStr
    pointer: str
    error_code: Literal[
        "container_pointer",
        "dangling_evidence",
        "dangling_pointer",
        "duplicate_evidence_id",
        "duplicate_evidence_reference",
        "exempt_pointer",
        "invalid_pointer",
        "missing_claim",
        "provenance_self_reference",
    ]
    message: NonEmptyStr


class ProvenanceReport(HistGermModel):
    """Complete deterministic result for one provenance-bearing record."""

    record_id: NonEmptyStr
    errors: tuple[ProvenanceIssue, ...]

    @property
    def is_complete(self) -> bool:
        return not self.errors


class _Resolved(HistGermModel):
    status: Literal["resolved"]
    value: JsonValue


class _Unresolved(HistGermModel):
    status: Literal["unresolved"]


type _Resolution = _Resolved | _Unresolved


def _escape_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _decode_pointer_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _join_pointer(pointer: str, token: str) -> str:
    return f"{pointer}/{_escape_pointer_token(token)}"


def resolve_json_pointer(document: JsonValue, pointer: str) -> _Resolution:
    """Resolve an RFC 6901 pointer without coercion or exception-based control."""

    if not _JSON_POINTER_PATTERN.fullmatch(pointer):
        return _Unresolved(status="unresolved")
    current: JsonValue = document
    if pointer == "":
        return _Resolved(status="resolved", value=current)

    for encoded_token in pointer[1:].split("/"):
        token = _decode_pointer_token(encoded_token)
        if isinstance(current, dict):
            if token not in current:
                return _Unresolved(status="unresolved")
            current = current[token]
        elif isinstance(current, list):
            if (
                not token
                or (len(token) > 1 and token.startswith("0"))
                or not token.isascii()
                or not token.isdecimal()
            ):
                return _Unresolved(status="unresolved")
            index = int(token)
            if index >= len(current):
                return _Unresolved(status="unresolved")
            current = current[index]
        else:
            return _Unresolved(status="unresolved")
    return _Resolved(status="resolved", value=current)


def _is_knowledge_wrapper(value: JsonValue) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("status"), str)
        and value["status"] in _KNOWLEDGE_STATES
        and set(value).issubset({"status", "value"})
    )


def _matches_pointer_policy(
    pointer: str, patterns: tuple[re.Pattern[str], ...]
) -> bool:
    return any(pattern.fullmatch(pointer) for pattern in patterns)


def _is_exempt_scalar(pointer: str) -> bool:
    if pointer.startswith("/extensions/"):
        return False
    token = _decode_pointer_token(pointer.rsplit("/", maxsplit=1)[-1])
    return (
        pointer in _ROOT_STRUCTURAL_POINTERS
        or token in _STRUCTURAL_DISCRIMINATORS
        or token == "record_reviewed_on"
        or _matches_pointer_policy(pointer, _OWN_ENTITY_ID_POINTER_PATTERNS)
        or _matches_pointer_policy(pointer, _STRUCTURAL_REFERENCE_POINTER_PATTERNS)
    )


def _required_leaf_pointers(value: JsonValue, pointer: str = "") -> set[str]:
    required: set[str] = set()
    if isinstance(value, dict):
        if _is_knowledge_wrapper(value):
            status = value["status"]
            if status == "known":
                required.update(
                    _required_leaf_pointers(
                        value["value"], _join_pointer(pointer, "value")
                    )
                )
            elif status == "not_publicly_available":
                required.add(_join_pointer(pointer, "status"))
            return required

        for key, child in value.items():
            if pointer == "" and key in _PROVENANCE_FIELDS:
                continue
            required.update(_required_leaf_pointers(child, _join_pointer(pointer, key)))
        return required

    if isinstance(value, list):
        for index, child in enumerate(value):
            required.update(
                _required_leaf_pointers(child, _join_pointer(pointer, str(index)))
            )
        return required

    if not _is_exempt_scalar(pointer):
        required.add(pointer)
    return required


def required_provenance_pointers(record: ProvenancedRecord) -> frozenset[str]:
    """Return every exact canonical leaf pointer that requires evidence."""

    document = record.model_dump(mode="json")
    return frozenset(_required_leaf_pointers(document))


def _record_identifier(document: Mapping[str, JsonValue]) -> str:
    identifier = document.get("id")
    if isinstance(identifier, str) and identifier:
        return identifier
    record_type = document.get("record_type")
    if isinstance(record_type, str) and record_type:
        return record_type
    return "<unidentified-record>"


def _issue(
    record_id: str, pointer: str, error_code: Any, message: str
) -> ProvenanceIssue:
    return ProvenanceIssue(
        record_id=record_id,
        pointer=pointer,
        error_code=error_code,
        message=message,
    )


def _claims_from_document(
    document: Mapping[str, JsonValue],
) -> list[tuple[str, list[str]]]:
    claims = document.get("claims", {})
    if not isinstance(claims, dict):
        return []
    result: list[tuple[str, list[str]]] = []
    for pointer, references in claims.items():
        if not isinstance(pointer, str) or not isinstance(references, list):
            continue
        result.append((pointer, [item for item in references if isinstance(item, str)]))
    return result


def provenance_completeness_report(
    record: ProvenancedRecord,
) -> ProvenanceReport:
    """Report all safe independent provenance failures in canonical order."""

    document = record.model_dump(mode="json")
    record_id = _record_identifier(document)
    evidence_ids = [item.id for item in record.evidence]
    evidence_id_set = set(evidence_ids)
    issues: list[ProvenanceIssue] = []

    seen_evidence: set[str] = set()
    for evidence_id in evidence_ids:
        if evidence_id in seen_evidence:
            issues.append(
                _issue(
                    record_id,
                    "/evidence",
                    "duplicate_evidence_id",
                    f"Evidence ID {evidence_id!r} is duplicated; assign each "
                    "evidence item a unique stable ID.",
                )
            )
        seen_evidence.add(evidence_id)

    claims = _claims_from_document(document)
    claim_pointers = {pointer for pointer, _ in claims}
    for pointer, references in claims:
        if not _JSON_POINTER_PATTERN.fullmatch(pointer):
            issues.append(
                _issue(
                    record_id,
                    pointer,
                    "invalid_pointer",
                    "Use an absolute RFC 6901 JSON Pointer with '~0' and '~1' "
                    "escaping.",
                )
            )
            continue

        first_token = (
            _decode_pointer_token(pointer[1:].split("/", maxsplit=1)[0])
            if pointer
            else ""
        )
        if first_token in _PROVENANCE_FIELDS:
            issues.append(
                _issue(
                    record_id,
                    pointer,
                    "provenance_self_reference",
                    "Claims cannot point into /evidence or /claims; point to "
                    "the supported factual leaf.",
                )
            )
            continue

        resolution = resolve_json_pointer(document, pointer)
        if isinstance(resolution, _Unresolved):
            issues.append(
                _issue(
                    record_id,
                    pointer,
                    "dangling_pointer",
                    "The claim pointer does not resolve in canonical "
                    "serialization; update it to the exact existing leaf.",
                )
            )
            continue
        if isinstance(resolution.value, (dict, list)):
            issues.append(
                _issue(
                    record_id,
                    pointer,
                    "container_pointer",
                    "The claim resolves to a container; point to each exact "
                    "factual scalar leaf instead.",
                )
            )
            continue
        if pointer not in _required_leaf_pointers(document):
            issues.append(
                _issue(
                    record_id,
                    pointer,
                    "exempt_pointer",
                    "The claim points only to an exempt structural value; "
                    "remove the claim or point it to a factual leaf.",
                )
            )

        seen_references: set[str] = set()
        for evidence_id in references:
            if evidence_id in seen_references:
                issues.append(
                    _issue(
                        record_id,
                        pointer,
                        "duplicate_evidence_reference",
                        f"Evidence ID {evidence_id!r} is repeated; keep one "
                        "reference per claim.",
                    )
                )
            elif evidence_id not in evidence_id_set:
                issues.append(
                    _issue(
                        record_id,
                        pointer,
                        "dangling_evidence",
                        f"Evidence ID {evidence_id!r} is not in this record; "
                        "add the evidence item or remove the reference.",
                    )
                )
            seen_references.add(evidence_id)

    for pointer in sorted(_required_leaf_pointers(document) - claim_pointers):
        issues.append(
            _issue(
                record_id,
                pointer,
                "missing_claim",
                "Add an exact claim for this factual leaf citing at least one "
                "local evidence item.",
            )
        )

    ordered = tuple(
        sorted(
            issues,
            key=lambda item: (
                item.pointer,
                item.error_code,
                item.message,
            ),
        )
    )
    return ProvenanceReport(record_id=record_id, errors=ordered)


class ProvenancedRecord(HistGermModel):
    """Base class for records with local evidence and exact claims."""

    evidence: list[EvidenceItem] = Field(default_factory=list)
    claims: ClaimMap = Field(default_factory=dict)
    extensions: ExtensionData = Field(default_factory=dict)

    @field_serializer("claims", when_used="json")
    def serialize_claims(self, claims: ClaimMap) -> dict[JsonPointer, list[EvidenceId]]:
        return {
            pointer: sorted(references)
            for pointer, references in sorted(claims.items())
        }

    @model_validator(mode="after")
    def validate_provenance_structure(self) -> ProvenancedRecord:
        evidence_ids = [item.id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence IDs must be unique within a record")

        report = provenance_completeness_report(self)
        structural_errors = [
            error for error in report.errors if error.error_code != "missing_claim"
        ]
        if structural_errors:
            details = "; ".join(
                f"{error.pointer or '<root>'}: {error.error_code}: {error.message}"
                for error in structural_errors
            )
            raise ValueError(f"invalid provenance claims: {details}")
        return self


__all__ = [
    "ClaimMap",
    "EvidenceItem",
    "ProvenanceIssue",
    "ProvenanceReport",
    "ProvenancedRecord",
    "provenance_completeness_report",
    "required_provenance_pointers",
    "resolve_json_pointer",
]
