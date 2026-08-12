"""Run-local focused-discovery coverage metrics."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from .search_providers import SearchAssessmentRecord

type CandidateDisposition = Literal["added", "duplicate", "blocked", "out_of_scope"]


@dataclass(slots=True)
class DiscoveryCoverage:
    """Accumulate ephemeral discovery counters without persistence behavior."""

    focused_queries_attempted: int = 0
    focused_queries_completed: int = 0
    model_leads: int = 0
    inventory_terms: int = 0
    inventory_leads: int = 0
    vocabulary_revision: int | None = None
    vocabulary_sources_refreshed: int = 0
    vocabulary_sources_reused: int = 0
    vocabulary_new_terms: int = 0
    vocabulary_reused_decisions: int = 0
    vocabulary_inactive_associations: int = 0
    vocabulary_access_gaps: int = 0
    providers_by_mode: Counter[str] = field(default_factory=Counter)
    interfaces: Counter[str] = field(default_factory=Counter)
    access_gaps: Counter[str] = field(default_factory=Counter)
    failures_by_stage: Counter[str] = field(default_factory=Counter)
    dispositions: Counter[str] = field(default_factory=Counter)
    unrelated_reasons: Counter[str] = field(default_factory=Counter)
    yield_by_family_channel: Counter[str] = field(default_factory=Counter)

    def record_assessment(
        self,
        record: SearchAssessmentRecord,
        *,
        family: str,
        channel: str,
        new_candidates: int = 0,
    ) -> None:
        """Record one exact provider attempt and its inspected outcome."""

        if new_candidates < 0:
            raise ValueError("new_candidates must not be negative")
        self.focused_queries_attempted += 1
        if record.completed:
            self.focused_queries_completed += 1
        provider_mode = f"{record.provider.value}:{record.retrieval_mode}"
        self.providers_by_mode[provider_mode] += 1
        self.interfaces[f"{record.provider.value}:{record.response_format.value}"] += 1
        if record.assessment == "access_gap":
            self.access_gaps[provider_mode] += 1
        if record.failure_stage is not None:
            self.failures_by_stage[
                f"{record.retrieval_mode}:{record.failure_stage}"
            ] += 1
        for inspection in record.inspections:
            if inspection.classification == "unrelated":
                self.unrelated_reasons[inspection.reason] += 1
        if new_candidates:
            self.yield_by_family_channel[f"{family}:{channel}"] += new_candidates

    def record_disposition(self, disposition: CandidateDisposition) -> None:
        """Record one final candidate disposition."""

        self.dispositions[disposition] += 1

    def snapshot(self) -> dict[str, object]:
        """Return a detached run-report mapping; no data is written persistently."""

        return {
            "focused_queries_attempted": self.focused_queries_attempted,
            "focused_queries_completed": self.focused_queries_completed,
            "providers_by_retrieval_mode": dict(self.providers_by_mode),
            "provider_interfaces": dict(self.interfaces),
            "model_leads": self.model_leads,
            "inventory_terms": self.inventory_terms,
            "inventory_leads": self.inventory_leads,
            "vocabulary_revision": self.vocabulary_revision,
            "vocabulary_sources_refreshed": self.vocabulary_sources_refreshed,
            "vocabulary_sources_reused": self.vocabulary_sources_reused,
            "vocabulary_new_terms": self.vocabulary_new_terms,
            "vocabulary_reused_decisions": self.vocabulary_reused_decisions,
            "vocabulary_inactive_associations": (self.vocabulary_inactive_associations),
            "vocabulary_access_gaps": self.vocabulary_access_gaps,
            "candidate_dispositions": dict(self.dispositions),
            "unrelated_result_reasons": dict(self.unrelated_reasons),
            "access_gaps": dict(self.access_gaps),
            "failures_by_stage": dict(self.failures_by_stage),
            "new_candidate_yield": dict(self.yield_by_family_channel),
        }


__all__ = ["CandidateDisposition", "DiscoveryCoverage"]
