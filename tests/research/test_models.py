from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from conftest import candidate_data, pass_data
from pydantic import ValidationError

from histgerm.research import (
    CandidateEntry,
    CandidateResearchResult,
    EvidenceExcerpt,
    SearchPass,
    SearchQueryRecord,
    load_ledger,
    resolve_request_destination,
    upsert_candidate,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/x",
        "https://user:secret@example.org/x",
        "https://127.0.0.1/x",
        "https://10.0.0.1/x",
        "https://169.254.1.1/x",
        "https://192.0.2.1/x",
        "https://[::1]/x",
        "https://service.internal/x",
        "https://example.invalid/x",
        "https://127.0.0.1.nip.io/x",
        "https://127-0-0-1.nip.io/x",
    ],
)
@pytest.mark.parametrize("model_field", ["query", "candidate", "evidence"])
def test_all_research_urls_reject_non_public_destinations(
    url: str, model_field: str
) -> None:
    with pytest.raises(ValidationError, match="public|credentials"):
        if model_field == "query":
            SearchQueryRecord(
                query="x",
                language="en",
                channel="olac",
                source_urls=[url],
                completed=True,
            )
        elif model_field == "candidate":
            CandidateEntry.model_validate(candidate_data(discovery_urls=[url]))
        else:
            EvidenceExcerpt(
                url=url,
                accessed_on=date(2026, 8, 12),
                kind="institutional",
                supports=["identity"],
            )


def test_request_boundary_rejects_redirect_and_mixed_dns_answers() -> None:
    calls: list[str] = []

    def resolver(
        host: str, port: int, **kwargs: object
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        calls.append(host)
        addresses = {
            "public.example.org": ["93.184.216.34"],
            "mixed.example.org": ["93.184.216.34", "127.0.0.1"],
            "redirect.example.org": ["169.254.169.254"],
        }[host]
        return [(2, 1, 6, "", (address, port)) for address in addresses]

    destination = resolve_request_destination(
        "https://public.example.org/start", resolver=resolver
    )
    assert str(destination.connect_ip) == "93.184.216.34"
    assert destination.hostname == "public.example.org"
    with pytest.raises(ValueError, match="non-public"):
        resolve_request_destination("https://mixed.example.org/", resolver=resolver)
    with pytest.raises(ValueError, match="non-public"):
        resolve_request_destination("https://redirect.example.org/", resolver=resolver)
    with pytest.raises(ValueError, match="embeds"):
        resolve_request_destination("https://127.0.0.1.nip.io/", resolver=resolver)
    assert calls == [
        "public.example.org",
        "mixed.example.org",
        "redirect.example.org",
    ]


def test_public_http_urls_and_policy_noted_empty_results_are_allowed() -> None:
    record = SearchQueryRecord(
        query="x",
        language="en",
        channel="olac",
        source_urls=["https://catalog.clarin.eu/ds/ComponentRegistry"],
        completed=True,
    )
    assert record.source_urls[0].scheme == "https"
    assert SearchQueryRecord(
        query="not applicable",
        language="en",
        channel="huggingface",
        source_urls=[],
        completed=True,
        note="The model channel is inapplicable to this dictionary pass.",
    )
    with pytest.raises(ValidationError, match="explicit inapplicable"):
        SearchQueryRecord(
            query="x",
            language="en",
            channel="olac",
            source_urls=[],
            completed=True,
            note="No results.",
        )


def test_complete_pass_requires_exact_bilingual_families_and_channels() -> None:
    assert SearchPass.model_validate(pass_data()).complete
    missing_family = pass_data()
    for query in missing_family["queries"]:
        if query["language"] == "de":
            query["query"] = "Mittelhochdeutsch Korpus"
    with pytest.raises(ValidationError, match="query families"):
        SearchPass.model_validate(missing_family)
    missing_channel = pass_data()
    missing_channel["queries"] = missing_channel["queries"][:-1]
    with pytest.raises(ValidationError, match="required channels"):
        SearchPass.model_validate(missing_channel)
    incomplete = pass_data()
    incomplete["queries"][0]["completed"] = False
    with pytest.raises(ValidationError, match="incomplete queries"):
        SearchPass.model_validate(incomplete)


def test_complete_tool_pass_requires_bilingual_architecture_families() -> None:
    assert SearchPass.model_validate(pass_data(category="tool")).complete
    missing_architecture = pass_data(category="tool")
    for query in missing_architecture["queries"]:
        query["query"] = query["query"].replace("BERT family", "")
    with pytest.raises(ValidationError, match="tool architecture families"):
        SearchPass.model_validate(missing_architecture)


def test_candidate_and_result_dispositions_remain_strict() -> None:
    with pytest.raises(ValidationError, match="requires resource_id"):
        CandidateEntry.model_validate(
            candidate_data(disposition="added", evidence_gaps=None)
        )
    with pytest.raises(ValidationError, match="requires one or more evidence gaps"):
        CandidateResearchResult(
            candidate_id="candidate-example",
            category="corpus",
            disposition="blocked",
            verified_stages=[],
            evidence=[],
            evidence_gaps=[],
            risk_flags=[],
            summary="Blocked.",
        )


def test_seed_rows_preserve_aliases_wordings_and_every_public_url() -> None:
    first = CandidateEntry.model_validate(
        candidate_data(
            id="candidate-component",
            name="Generic Tagger",
            aliases=["GT", "GenericTagger"],
            source_wordings=[
                "Generic Tagger used by the MHG pipeline",
                "modern German tagging component",
            ],
            discovery_urls=[
                "https://example.org/seed",
                "https://example.org/component",
                "https://example.org/application",
            ],
        )
    )
    second = CandidateEntry.model_validate(
        candidate_data(
            id="candidate-application",
            name="MHG Pipeline",
            aliases=["Middle High German Pipeline"],
            source_wordings=["MHG Pipeline (uses Generic Tagger)"],
            discovery_urls=[
                "https://example.org/seed",
                "https://example.org/application",
            ],
        )
    )

    assert first.aliases == ["GT", "GenericTagger"]
    assert first.source_wordings == [
        "Generic Tagger used by the MHG pipeline",
        "modern German tagging component",
    ]
    assert [str(url) for url in first.discovery_urls] == [
        "https://example.org/seed",
        "https://example.org/component",
        "https://example.org/application",
    ]
    assert first.id != second.id
    assert first.name != second.name


def test_component_application_and_corpus_identity_remain_distinct() -> None:
    shared = ["https://example.org/project"]
    component = CandidateEntry.model_validate(
        candidate_data(
            id="candidate-generic-component",
            name="Generic Modern German Tagger",
            category="tool",
            discovery_urls=shared,
            disposition="blocked",
            evidence_gaps=["Canonical component-level MHG support is unverified."],
        )
    )
    application = CandidateEntry.model_validate(
        candidate_data(
            id="candidate-mhg-application",
            name="MHG Annotation Pipeline",
            category="tool",
            discovery_urls=shared,
        )
    )
    corpus = CandidateEntry.model_validate(
        candidate_data(
            id="candidate-training-corpus",
            name="Pipeline Training Corpus",
            category="corpus",
            discovery_urls=shared,
        )
    )

    assert len({component.id, application.id, corpus.id}) == 3
    assert component.disposition == "blocked"
    assert application.category == "tool"
    assert corpus.category == "corpus"


def test_revision_95_ledger_defaults_new_seed_fields_without_rewriting() -> None:
    ledger = load_ledger(Path("research") / "discovery-ledger.yaml")
    assert ledger.revision == 95
    legacy = next(candidate for candidate in ledger.candidates if not candidate.aliases)
    assert legacy.aliases == []
    assert legacy.source_wordings == []


def test_seed_handoff_round_trip_is_lossless_and_deterministic(
    ledger_path: Path,
) -> None:
    candidate = CandidateEntry.model_validate(
        candidate_data(
            aliases=[" Exact Alias ", "Alias  with punctuation (v2)"],
            source_wordings=[
                " Exact authored  seed wording ",
                "No named-entity recognition model exists.",
            ],
            discovery_urls=[
                "https://example.org/seed",
                "https://example.org/component",
                "https://example.org/application",
            ],
        )
    )
    updated = upsert_candidate(ledger_path, candidate, expected_revision=0)
    reloaded = load_ledger(ledger_path)
    stored = next(item for item in reloaded.candidates if item.id == candidate.id)

    assert stored.aliases == candidate.aliases
    assert stored.source_wordings == candidate.source_wordings
    assert stored.discovery_urls == candidate.discovery_urls
    assert updated.model_dump(mode="json") == reloaded.model_dump(mode="json")
