from __future__ import annotations

from datetime import UTC, datetime

from histgerm.research.discovery_metrics import DiscoveryCoverage
from histgerm.research.search_providers import (
    ResponseFormat,
    SearchProvider,
    assess_search_response,
)


def test_coverage_is_run_local_and_tracks_provider_mode_yield_and_gaps() -> None:
    metrics = DiscoveryCoverage(model_leads=2, inventory_terms=4, inventory_leads=1)
    record = assess_search_response(
        provider=SearchProvider.BING,
        query="Mittelhochdeutsch POS-Tagger",
        retrieval_mode="bounded_http",
        response_format=ResponseFormat.RSS,
        locale="de-DE",
        observed_at=datetime(2026, 8, 12, tzinfo=UTC),
        http_status=200,
        body="<rss><channel><item><title>Tool</title>"
        "<link>https://example.org/tool</link></item></channel></rss>",
        inspector=lambda result: ("lead", "untrusted lead"),
    )
    metrics.record_assessment(
        record, family="tagging", channel="general_web", new_candidates=1
    )
    metrics.record_disposition("added")
    snapshot = metrics.snapshot()
    assert snapshot["focused_queries_attempted"] == 1
    assert snapshot["focused_queries_completed"] == 1
    assert snapshot["providers_by_retrieval_mode"] == {"bing:bounded_http": 1}
    assert snapshot["provider_interfaces"] == {"bing:rss": 1}
    assert snapshot["new_candidate_yield"] == {"tagging:general_web": 1}
    assert snapshot["candidate_dispositions"] == {"added": 1}


def test_google_access_gap_is_counted_as_incomplete() -> None:
    metrics = DiscoveryCoverage()
    record = assess_search_response(
        provider=SearchProvider.GOOGLE,
        query="Middle High German corpus",
        retrieval_mode="bounded_http",
        locale="en-US",
        observed_at=datetime(2026, 8, 12, tzinfo=UTC),
        http_status=403,
        body="Access denied",
        inspector=lambda result: ("lead", "unused"),
    )
    metrics.record_assessment(record, family="corpus", channel="general_web")
    snapshot = metrics.snapshot()
    assert snapshot["focused_queries_completed"] == 0
    assert snapshot["access_gaps"] == {"google:bounded_http": 1}
    assert snapshot["failures_by_stage"] == {"bounded_http:challenge": 1}


def test_vocabulary_lifecycle_metrics_are_reported_separately() -> None:
    metrics = DiscoveryCoverage(
        vocabulary_revision=7,
        vocabulary_sources_refreshed=2,
        vocabulary_sources_reused=3,
        vocabulary_new_terms=4,
        vocabulary_reused_decisions=5,
        vocabulary_inactive_associations=6,
        vocabulary_access_gaps=1,
    )

    snapshot = metrics.snapshot()

    assert snapshot["vocabulary_revision"] == 7
    assert snapshot["vocabulary_sources_refreshed"] == 2
    assert snapshot["vocabulary_sources_reused"] == 3
    assert snapshot["vocabulary_new_terms"] == 4
    assert snapshot["vocabulary_reused_decisions"] == 5
    assert snapshot["vocabulary_inactive_associations"] == 6
    assert snapshot["vocabulary_access_gaps"] == 1


def test_elicitation_recovery_counters_are_reported() -> None:
    metrics = DiscoveryCoverage(
        elicitation_retries=2,
        elicitation_recovered_retries=1,
        elicitation_blocked_responses=1,
        elicitation_quarantined_candidates=3,
    )

    snapshot = metrics.snapshot()

    assert snapshot["elicitation_retries"] == 2
    assert snapshot["elicitation_recovered_retries"] == 1
    assert snapshot["elicitation_blocked_responses"] == 1
    assert snapshot["elicitation_quarantined_candidates"] == 3
