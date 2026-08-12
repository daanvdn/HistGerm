"""Bounded, transient model-led candidate elicitation."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from histgerm.models import BaseResource, LanguageStage

from .models import CandidateEntry, ResourceCategory

_MAX_NAME_LENGTH = 160
_URL_MARKER = re.compile(r"(?:[a-z][a-z0-9+.-]*://|www\.)", re.IGNORECASE)
_NON_NAME_CHARACTER = re.compile(r"[^\w]+", re.UNICODE)
_FOCUSES: dict[ResourceCategory, tuple[str, ...]] = {
    "corpus": (
        "text corpora, text collections, datasets, and language data",
        "annotated corpora, treebanks, editions, and historical text collections",
        "institutional or project-based corpora and named corpus collections",
    ),
    "tool": (
        "tagging, part-of-speech annotation, and morphology",
        "lemmatization, normalization, and historical spelling normalization",
        "parsing, segmentation, language models, and NLP pipelines",
    ),
    "dictionary": (
        "historical dictionaries, lexicons, and vocabularies",
        "digital, searchable, and machine-readable dictionaries",
        "institutional dictionary projects and named lexical resources",
    ),
}


class ModelCall(Protocol):
    """Call an injected model and return its raw JSON text."""

    def __call__(self, prompt: str, /) -> str:
        """Return one model response."""
        ...


class ElicitationError(ValueError):
    """Base error for bounded elicitation failures."""


class ElicitationOutputError(ElicitationError):
    """Report invalid or policy-incompatible model output."""


class ElicitationLimitError(ElicitationError):
    """Report input or prompt state that exceeds configured bounds."""


@dataclass(frozen=True, slots=True)
class ElicitationConfig:
    """Hard bounds for one transient elicitation run."""

    max_iterations: int = 4
    exclusion_group_size: int = 12
    max_exclusion_names: int = 240
    max_prompt_chars: int = 24_000
    max_output_chars: int = 32_000
    max_candidates_per_response: int = 25
    max_aliases_per_candidate: int = 10

    def __post_init__(self) -> None:
        bounds = (
            ("max_iterations", self.max_iterations, 1, 10),
            ("exclusion_group_size", self.exclusion_group_size, 1, 25),
            ("max_exclusion_names", self.max_exclusion_names, 1, 500),
            ("max_prompt_chars", self.max_prompt_chars, 1_000, 50_000),
            ("max_output_chars", self.max_output_chars, 100, 100_000),
            ("max_candidates_per_response", self.max_candidates_per_response, 1, 50),
            ("max_aliases_per_candidate", self.max_aliases_per_candidate, 0, 20),
        )
        for name, value, minimum, maximum in bounds:
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")


@dataclass(frozen=True, slots=True)
class ElicitedLead:
    """One untrusted name-only lead retained for external verification."""

    name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ElicitationPrompt:
    """One transient prompt and its deterministic position."""

    iteration: int
    kind: Literal["broad", "follow_up"]
    text: str


@dataclass(frozen=True, slots=True)
class ElicitationResult:
    """Transient coordinator input that never substitutes for external search."""

    leads: tuple[ElicitedLead, ...]
    prompts: tuple[ElicitationPrompt, ...]
    requires_external_search: Literal[True] = True


class _CandidateOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=_MAX_NAME_LENGTH)
    aliases: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _validate_name(value)

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, values: list[str]) -> list[str]:
        return [_validate_name(value) for value in values]


class _ResponseOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    candidates: list[_CandidateOutput]


@dataclass(slots=True)
class _LeadAccumulator:
    name: str
    aliases: list[str]

    def normalized_values(self) -> set[str]:
        return {_normalize_name(self.name), *(_normalize_name(a) for a in self.aliases)}


def elicit_candidates(
    model_call: ModelCall,
    *,
    category: ResourceCategory,
    stage: LanguageStage,
    trusted_records: Sequence[BaseResource],
    ledger_candidates: Sequence[CandidateEntry],
    config: ElicitationConfig | None = None,
) -> ElicitationResult:
    """Elicit deduplicated, name-only leads before required external searching.

    Model responses are transient and untrusted. The result deliberately has no
    URL, evidence, rationale, or persistence field.
    """

    if config is None:
        config = ElicitationConfig()
    known_names = _known_names(trusted_records, ledger_candidates)
    known_normalized = {_normalize_name(name) for name in known_names}
    leads: list[_LeadAccumulator] = []
    prompts: list[ElicitationPrompt] = []

    for iteration in range(config.max_iterations):
        if iteration == 0:
            kind: Literal["broad", "follow_up"] = "broad"
            prompt_text = _broad_prompt(category, stage)
        else:
            kind = "follow_up"
            exclusions = _unique_names(
                [
                    *known_names,
                    *(value for lead in leads for value in [lead.name, *lead.aliases]),
                ]
            )
            prompt_text = _follow_up_prompt(
                category,
                stage,
                _FOCUSES[category][(iteration - 1) % len(_FOCUSES[category])],
                exclusions,
                config,
            )
        _check_prompt_bound(prompt_text, config)
        prompts.append(
            ElicitationPrompt(
                iteration=iteration + 1,
                kind=kind,
                text=prompt_text,
            )
        )
        output = _parse_output(model_call(prompt_text), config)
        new_distinct = _add_output_leads(
            output,
            known_normalized=known_normalized,
            leads=leads,
        )
        if iteration > 0 and new_distinct == 0:
            break

    return ElicitationResult(
        leads=tuple(ElicitedLead(lead.name, tuple(lead.aliases)) for lead in leads),
        prompts=tuple(prompts),
    )


def _validate_name(value: str) -> str:
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError(
            "candidate names and aliases must not contain control characters"
        )
    if _URL_MARKER.search(value):
        raise ValueError("candidate names and aliases must not contain URLs")
    if not _normalize_name(value):
        raise ValueError("candidate names and aliases must contain letters or numbers")
    return value


def _normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(part for part in _NON_NAME_CHARACTER.split(normalized) if part)


def _unique_names(values: Sequence[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _normalize_name(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(value)
    return unique


def _known_names(
    trusted_records: Sequence[BaseResource],
    ledger_candidates: Sequence[CandidateEntry],
) -> list[str]:
    inventory_names = [
        value
        for record in trusted_records
        for value in [record.name, *(record.aliases or [])]
    ]
    return _unique_names(
        [*inventory_names, *(candidate.name for candidate in ledger_candidates)]
    )


def _broad_prompt(category: ResourceCategory, stage: LanguageStage) -> str:
    return (
        "Generate plausible resource leads from model knowledge before web search.\n"
        f"Category: {category}\n"
        f"Historical German stage: {stage.value}\n"
        "Return only JSON matching "
        '{"candidates":[{"name":"canonical or project name","aliases":["alias"]}]}.\n'
        "Include names and aliases only. Do not include rationale, URLs, evidence, "
        "dates, versions, licenses, or stage claims. An empty candidates list is valid."
    )


def _follow_up_prompt(
    category: ResourceCategory,
    stage: LanguageStage,
    focus: str,
    exclusions: Sequence[str],
    config: ElicitationConfig,
) -> str:
    if len(exclusions) > config.max_exclusion_names:
        raise ElicitationLimitError(
            f"exclusion names exceed configured limit {config.max_exclusion_names}"
        )
    groups = [
        exclusions[index : index + config.exclusion_group_size]
        for index in range(0, len(exclusions), config.exclusion_group_size)
    ]
    exclusion_text = "\n".join(
        f"Exclude group {index}: " + " | ".join(group)
        for index, group in enumerate(groups, start=1)
    )
    return (
        "Generate additional plausible resource leads beyond all known and seen "
        "names.\n"
        f"Category: {category}\n"
        f"Historical German stage: {stage.value}\n"
        f"Task-specific focus: {focus}\n"
        f"{exclusion_text or 'Exclude group 1: (none)'}\n"
        "Do not return any excluded resource or alias. Consider aliases, former names, "
        "project names, and responsible institutions only as possible names.\n"
        "Return only JSON matching "
        '{"candidates":[{"name":"canonical or project name","aliases":["alias"]}]}.\n'
        "Include names and aliases only. Do not include rationale, URLs, evidence, "
        "dates, versions, licenses, or stage claims. An empty candidates list is valid."
    )


def _check_prompt_bound(prompt: str, config: ElicitationConfig) -> None:
    if len(prompt) > config.max_prompt_chars:
        raise ElicitationLimitError(
            f"prompt exceeds configured limit {config.max_prompt_chars}"
        )


def _parse_output(raw: str, config: ElicitationConfig) -> _ResponseOutput:
    if not isinstance(raw, str):
        raise ElicitationOutputError("model output must be JSON text")
    if len(raw) > config.max_output_chars:
        raise ElicitationOutputError(
            f"model output exceeds configured limit {config.max_output_chars}"
        )
    try:
        value: Any = json.loads(raw)
        output = _ResponseOutput.model_validate(value)
    except (json.JSONDecodeError, ValidationError) as error:
        raise ElicitationOutputError(
            "model output is not valid name-only JSON"
        ) from error
    if len(output.candidates) > config.max_candidates_per_response:
        raise ElicitationOutputError(
            "model output exceeds the configured candidate count"
        )
    if any(
        len(candidate.aliases) > config.max_aliases_per_candidate
        for candidate in output.candidates
    ):
        raise ElicitationOutputError("model output exceeds the configured alias count")
    return output


def _add_output_leads(
    output: _ResponseOutput,
    *,
    known_normalized: set[str],
    leads: list[_LeadAccumulator],
) -> int:
    new_distinct = 0
    for candidate in output.candidates:
        values = _unique_names([candidate.name, *candidate.aliases])
        normalized = {_normalize_name(value) for value in values}
        if not normalized or normalized & known_normalized:
            continue
        matching = [
            index
            for index, lead in enumerate(leads)
            if normalized & lead.normalized_values()
        ]
        if matching:
            target = leads[matching[0]]
            _merge_names(target, values)
            for index in reversed(matching[1:]):
                duplicate = leads.pop(index)
                _merge_names(target, [duplicate.name, *duplicate.aliases])
            continue
        leads.append(_LeadAccumulator(name=values[0], aliases=values[1:]))
        new_distinct += 1
    return new_distinct


def _merge_names(target: _LeadAccumulator, values: Sequence[str]) -> None:
    existing = target.normalized_values()
    for value in values:
        normalized = _normalize_name(value)
        if normalized not in existing:
            target.aliases.append(value)
            existing.add(normalized)


__all__ = [
    "ElicitationConfig",
    "ElicitationError",
    "ElicitationLimitError",
    "ElicitationOutputError",
    "ElicitationPrompt",
    "ElicitationResult",
    "ElicitedLead",
    "ModelCall",
    "elicit_candidates",
]
