from __future__ import annotations

import json
from datetime import date
from typing import cast

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


def raw_candidates(*entries: object) -> str:
    return json.dumps({"candidates": list(entries)})


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


def test_one_malformed_candidate_keeps_valid_siblings() -> None:
    model = StubModel(
        [
            raw_candidates(
                {"name": "Alpha", "aliases": []},
                {"name": "Bad", "aliases": [], "rationale": "because"},
                {"name": "Beta", "aliases": []},
            ),
            response(),
        ]
    )

    result = elicit_candidates(
        model,
        category="corpus",
        stage=LanguageStage.MHG,
        trusted_records=[],
        ledger_candidates=[],
    )

    assert [(lead.name, lead.aliases) for lead in result.leads] == [
        ("Alpha", ()),
        ("Beta", ()),
    ]
    assert len(result.quarantines) == 1
    quarantine = result.quarantines[0]
    assert quarantine.scope == "candidate"
    assert quarantine.iteration == 1
    assert quarantine.position == 2
    assert quarantine.name == "Bad"
    assert quarantine.reason == "candidate contains fields beyond name and aliases"
    assert result.metrics.candidates_quarantined == 1
    assert result.metrics.retries_attempted == 0
    assert result.metrics.responses_blocked == 0
    assert result.requires_external_search is True


def test_non_object_candidate_entry_is_quarantined_without_a_name() -> None:
    model = StubModel(
        [
            raw_candidates("loose string", {"name": "Good", "aliases": []}),
            response(),
        ]
    )

    result = elicit_candidates(
        model,
        category="corpus",
        stage=LanguageStage.MHG,
        trusted_records=[],
        ledger_candidates=[],
    )

    assert [lead.name for lead in result.leads] == ["Good"]
    assert len(result.quarantines) == 1
    quarantine = result.quarantines[0]
    assert quarantine.scope == "candidate"
    assert quarantine.position == 1
    assert quarantine.name is None
    assert quarantine.reason == "candidate entry is not a name-only object"
    assert result.metrics.candidates_quarantined == 1


def test_url_and_wrong_type_candidates_are_quarantined_with_raw_name() -> None:
    model = StubModel(
        [
            raw_candidates(
                {"name": "https://example.org/x", "aliases": []},
                {"name": 3, "aliases": []},
                {"name": "Fine", "aliases": []},
            ),
            response(),
        ]
    )

    result = elicit_candidates(
        model,
        category="corpus",
        stage=LanguageStage.MHG,
        trusted_records=[],
        ledger_candidates=[],
    )

    assert [lead.name for lead in result.leads] == ["Fine"]
    reasons = [(q.position, q.name, q.reason) for q in result.quarantines]
    assert reasons == [
        (
            1,
            "https://example.org/x",
            "candidate name is missing or not a valid name-only string",
        ),
        (2, None, "candidate name is missing or not a valid name-only string"),
    ]
    assert result.metrics.candidates_quarantined == 2


def test_invalid_json_recovers_on_the_second_valid_response() -> None:
    model = StubModel(["not valid json", response(("Gamma", [])), response()])

    result = elicit_candidates(
        model,
        category="corpus",
        stage=LanguageStage.MHG,
        trusted_records=[],
        ledger_candidates=[],
    )

    assert [lead.name for lead in result.leads] == ["Gamma"]
    assert result.quarantines == ()
    assert result.metrics.retries_attempted == 1
    assert result.metrics.retries_recovered == 1
    assert result.metrics.responses_blocked == 0
    assert [(prompt.kind, prompt.iteration) for prompt in result.prompts] == [
        ("broad", 1),
        ("retry", 1),
        ("follow_up", 2),
    ]
    retry_prompt = result.prompts[1].text
    assert "must be corrected" in retry_prompt
    assert "Reason: model output is not valid JSON" in retry_prompt
    assert "URLs, evidence" in retry_prompt


def test_repeated_invalid_output_becomes_a_scoped_block() -> None:
    model = StubModel(["not json", "still not json", response()])

    result = elicit_candidates(
        model,
        category="corpus",
        stage=LanguageStage.MHG,
        trusted_records=[],
        ledger_candidates=[],
    )

    assert result.leads == ()
    assert result.metrics.retries_attempted == 1
    assert result.metrics.retries_recovered == 0
    assert result.metrics.responses_blocked == 1
    assert len(result.quarantines) == 1
    quarantine = result.quarantines[0]
    assert quarantine.scope == "response"
    assert quarantine.iteration == 1
    assert quarantine.position is None
    assert quarantine.reason == "model output is not valid JSON"


def test_malformed_candidates_array_is_retried_not_dropped_silently() -> None:
    model = StubModel(
        [
            json.dumps({"candidates": {"name": "X"}}),
            response(("Delta", [])),
            response(),
        ]
    )

    result = elicit_candidates(
        model,
        category="tool",
        stage=LanguageStage.MHG,
        trusted_records=[],
        ledger_candidates=[],
    )

    assert [lead.name for lead in result.leads] == ["Delta"]
    assert result.metrics.retries_attempted == 1
    assert result.metrics.retries_recovered == 1
    assert (
        "Reason: model output is not a name-only candidates object"
        in result.prompts[1].text
    )


def test_candidate_count_excess_is_truncated_with_a_warning() -> None:
    model = StubModel([response(("A", []), ("B", []), ("C", [])), response()])

    result = elicit_candidates(
        model,
        category="corpus",
        stage=LanguageStage.MHG,
        trusted_records=[],
        ledger_candidates=[],
        config=ElicitationConfig(max_candidates_per_response=2),
    )

    assert [lead.name for lead in result.leads] == ["A", "B"]
    assert result.quarantines == ()
    assert result.metrics.candidates_truncated == 1
    assert any("kept the first 2" in warning for warning in result.warnings)


def test_candidate_alias_excess_is_truncated_with_a_warning() -> None:
    model = StubModel([response(("A", ["x", "y", "z"])), response()])

    result = elicit_candidates(
        model,
        category="corpus",
        stage=LanguageStage.MHG,
        trusted_records=[],
        ledger_candidates=[],
        config=ElicitationConfig(max_aliases_per_candidate=1),
    )

    assert [(lead.name, lead.aliases) for lead in result.leads] == [("A", ("x",))]
    assert result.metrics.aliases_truncated == 2
    assert any("kept the first 1" in warning for warning in result.warnings)


def test_oversized_output_is_retried_and_can_recover() -> None:
    model = StubModel([" " * 101, response(("Epsilon", [])), response()])

    result = elicit_candidates(
        model,
        category="corpus",
        stage=LanguageStage.MHG,
        trusted_records=[],
        ledger_candidates=[],
        config=ElicitationConfig(max_output_chars=100),
    )

    assert [lead.name for lead in result.leads] == ["Epsilon"]
    assert result.metrics.retries_attempted == 1
    assert result.metrics.retries_recovered == 1
    assert "exceeds the configured limit 100" in result.prompts[1].text


def test_non_string_model_output_is_rejected_loudly() -> None:
    class NonStringModel:
        def __call__(self, prompt: str, /) -> str:
            return cast(str, 123)

    with pytest.raises(ElicitationOutputError, match="must be JSON text"):
        elicit_candidates(
            NonStringModel(),
            category="corpus",
            stage=LanguageStage.MHG,
            trusted_records=[],
            ledger_candidates=[],
        )


def test_exclusion_name_limit_is_still_rejected() -> None:
    with pytest.raises(ElicitationLimitError, match="exclusion names"):
        elicit_candidates(
            StubModel([response(("Fresh", []))]),
            category="corpus",
            stage=LanguageStage.MHG,
            trusted_records=[trusted_resource("Trusted", ["Alias"])],
            ledger_candidates=[],
            config=ElicitationConfig(max_exclusion_names=1),
        )
