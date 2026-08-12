from __future__ import annotations

from datetime import date

import pytest
from conftest import candidate_data, pass_data
from pydantic import ValidationError

from histgerm.research import (
    CandidateEntry,
    CandidateResearchResult,
    EvidenceExcerpt,
    SearchPass,
    SearchQueryRecord,
    resolve_request_destination,
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
