from __future__ import annotations

import json
from datetime import date

import pytest

from histgerm.models import BaseResource, LanguageStage
from histgerm.research.elicitation import (
    ElicitationConfig,
    ElicitationLimitError,
    ElicitationOutputError,
    elicit_candidates,
)
from histgerm.research.models import CandidateEntry


class StubModel:
    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.prompts: list[str] = []

    def __call__(self, prompt: str, /) -> str:
        self.prompts.append(prompt)
        return next(self.responses)


def response(*candidates: tuple[str, list[str]]) -> str:
    return json.dumps(
        {
            "candidates": [
                {"name": name, "aliases": aliases} for name, aliases in candidates
            ]
        }
    )


def trusted_resource(name: str, aliases: list[str] | None = None) -> BaseResource:
    return BaseResource.model_validate(
        {
            "id": "corpus-trusted",
            "name": name,
            "aliases": aliases,
            "sources": [
                {
                    "id": "source-main",
                    "url": "https://example.org/resource",
                    "accessed_on": "2026-08-12",
                    "supports": ["id", "name"],
                }
            ],
            "reviewed_on": "2026-08-12",
        }
    )


def ledger_candidate(name: str) -> CandidateEntry:
    return CandidateEntry(
        id="candidate-ledger",
        name=name,
        category="corpus",
        discovered_on=date(2026, 8, 12),
        last_checked_on=date(2026, 8, 12),
        discovery_urls=["https://example.org/ledger"],
        disposition="pending",
        refreshed_existing=False,
    )


def test_aliases_and_duplicates_are_normalized_and_merged() -> None:
    model = StubModel(
        [
            response(
                ("  New–Corpus  ", ["NC", "new corpus"]),
                ("NC", ["Project N"]),
                ("Trusted Alias", ["Should disappear"]),
                ("Ledger Name", []),
            ),
            response(("Project N", ["Neues Korpus"]), ("Another", ["A"])),
            response(("another", ["A2"])),
        ]
    )

    result = elicit_candidates(
        model,
        category="corpus",
        stage=LanguageStage.MHG,
        trusted_records=[trusted_resource("Trusted", ["Trusted Alias"])],
        ledger_candidates=[ledger_candidate("Ledger Name")],
    )

    assert [(lead.name, lead.aliases) for lead in result.leads] == [
        ("New–Corpus", ("NC", "Project N", "Neues Korpus")),
        ("Another", ("A", "A2")),
    ]
    assert len(result.prompts) == 3
    assert result.requires_external_search is True


def test_empty_broad_result_still_gets_one_follow_up_and_never_skips_search() -> None:
    model = StubModel([response(), response()])

    result = elicit_candidates(
        model,
        category="dictionary",
        stage=LanguageStage.OHG,
        trusted_records=[],
        ledger_candidates=[],
    )

    assert result.leads == ()
    assert [prompt.kind for prompt in result.prompts] == ["broad", "follow_up"]
    assert result.requires_external_search is True


def test_follow_ups_exclude_known_seen_names_in_bounded_groups() -> None:
    model = StubModel([response(("Fresh", ["Fresh Alias"])), response()])

    result = elicit_candidates(
        model,
        category="tool",
        stage=LanguageStage.ENHG,
        trusted_records=[trusted_resource("Trusted", ["Trusted Alias"])],
        ledger_candidates=[ledger_candidate("Ledger Name")],
        config=ElicitationConfig(exclusion_group_size=2),
    )

    follow_up = result.prompts[1].text
    assert (
        "Task-specific focus: tagging, part-of-speech annotation, and morphology"
        in follow_up
    )
    assert "Exclude group 1: Trusted | Trusted Alias" in follow_up
    assert "Exclude group 2: Ledger Name | Fresh" in follow_up
    assert "Exclude group 3: Fresh Alias" in follow_up
    assert "URLs, evidence" in follow_up


def test_iteration_cap_and_prompt_sequence_are_deterministic() -> None:
    responses = [
        response(("One", [])),
        response(("Two", [])),
        response(("Three", [])),
        response(("Four", [])),
    ]
    first = StubModel(responses.copy())
    second = StubModel(responses.copy())
    config = ElicitationConfig(max_iterations=4)
    first_result = elicit_candidates(
        first,
        category="tool",
        stage=LanguageStage.MHG,
        trusted_records=[],
        ledger_candidates=[],
        config=config,
    )
    second_result = elicit_candidates(
        second,
        category="tool",
        stage=LanguageStage.MHG,
        trusted_records=[],
        ledger_candidates=[],
        config=config,
    )

    assert len(first_result.leads) == 4
    assert first.prompts == second.prompts
    assert first_result.prompts == second_result.prompts
    assert [prompt.iteration for prompt in first_result.prompts] == [1, 2, 3, 4]
    assert "lemmatization, normalization" in first.prompts[2]
    assert "parsing, segmentation" in first.prompts[3]


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '{"candidates":[{"name":"X","aliases":[],"rationale":"because"}]}',
        '{"candidates":[{"name":"https://example.org/x","aliases":[]}]}',
        '{"candidates":[{"name":3,"aliases":[]}]}',
    ],
)
def test_invalid_or_non_name_output_is_rejected(raw: str) -> None:
    with pytest.raises(ElicitationOutputError, match="name-only JSON"):
        elicit_candidates(
            StubModel([raw]),
            category="corpus",
            stage=LanguageStage.MHG,
            trusted_records=[],
            ledger_candidates=[],
        )


def test_oversized_output_candidate_alias_and_exclusion_limits_are_rejected() -> None:
    with pytest.raises(ElicitationOutputError, match="output exceeds"):
        elicit_candidates(
            StubModel([" " * 101]),
            category="corpus",
            stage=LanguageStage.MHG,
            trusted_records=[],
            ledger_candidates=[],
            config=ElicitationConfig(max_output_chars=100),
        )

    with pytest.raises(ElicitationOutputError, match="candidate count"):
        elicit_candidates(
            StubModel([response(("A", []), ("B", []))]),
            category="corpus",
            stage=LanguageStage.MHG,
            trusted_records=[],
            ledger_candidates=[],
            config=ElicitationConfig(max_candidates_per_response=1),
        )

    with pytest.raises(ElicitationOutputError, match="alias count"):
        elicit_candidates(
            StubModel([response(("A", ["B"]))]),
            category="corpus",
            stage=LanguageStage.MHG,
            trusted_records=[],
            ledger_candidates=[],
            config=ElicitationConfig(max_aliases_per_candidate=0),
        )

    with pytest.raises(ElicitationLimitError, match="exclusion names"):
        elicit_candidates(
            StubModel([response(("Fresh", []))]),
            category="corpus",
            stage=LanguageStage.MHG,
            trusted_records=[trusted_resource("Trusted", ["Alias"])],
            ledger_candidates=[],
            config=ElicitationConfig(max_exclusion_names=1),
        )
