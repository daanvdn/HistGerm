from __future__ import annotations

import os
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from histgerm.research.inventory_vocabulary import InventoryURL, URLKind
from histgerm.research.vocabulary_store import (
    DiscoveryVocabulary,
    VocabularyPolicyError,
    VocabularyRevisionError,
    VocabularyValidationError,
    VocabularyWriteError,
    apply_vocabulary,
    load_vocabulary,
    mark_source_access_gap,
    reconcile_inventory_sources,
    select_sources_for_refresh,
    serialize_vocabulary,
)


def populated_data() -> dict[str, Any]:
    url = "https://www.example.com/project"
    return {
        "schema_version": 1,
        "revision": 0,
        "updated_on": "2026-08-12",
        "sources": [
            {
                "url": url,
                "resource_ids": ["tool-example"],
                "source_fields": ["links.homepage"],
                "last_attempted_on": "2026-08-12",
                "last_successful_on": "2026-08-12",
                "refresh_after": "2026-09-11",
                "status": "active",
                "etag": None,
                "last_modified": None,
                "crawl_cache_key": "cache-key",
                "raw_content_sha256": "a" * 64,
                "cleaned_content_sha256": "b" * 64,
                "crawler_version": "1.0",
                "extractor_version": 1,
                "gap": None,
                "decisions": [
                    {
                        "normalized": "part of speech tagging",
                        "suggested_kind": "task",
                        "accepted": True,
                        "active": True,
                        "first_seen_on": "2026-08-12",
                        "last_seen_on": "2026-08-12",
                    }
                ],
            }
        ],
        "terms": [
            {
                "normalized": "part of speech tagging",
                "kind": "task",
                "active": True,
                "contexts": [{"category": "tool", "stage": "mhg"}],
                "wordings": [
                    {
                        "value": "Part-of-Speech Tagging",
                        "source_urls": [url],
                        "inactive_source_urls": [],
                        "first_seen_on": "2026-08-12",
                        "last_seen_on": "2026-08-12",
                    }
                ],
            }
        ],
    }


def write_vocabulary(path: Path, data: dict[str, Any]) -> None:
    model = DiscoveryVocabulary.model_validate(data)
    path.write_bytes(serialize_vocabulary(model))


def test_revision_zero_artifact_and_populated_document_validate() -> None:
    empty = load_vocabulary(Path("research") / "discovery-vocabulary.yaml")
    populated = DiscoveryVocabulary.model_validate(populated_data())

    assert (empty.schema_version, empty.revision, empty.sources, empty.terms) == (
        1,
        0,
        [],
        [],
    )
    assert populated.terms[0].trusted_inventory_evidence is False
    assert populated.sources[0].untrusted_extracted_content is True
    assert populated.terms[0].contexts[0].untrusted_observation is True


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda data: data.update({"unknown": True}), "extra"),
        (lambda data: data.update({"schema_version": 2}), "schema_version"),
        (
            lambda data: data["sources"][0].update({"url": "http://127.0.0.1/x"}),
            "public",
        ),
        (
            lambda data: data["sources"][0]["decisions"][0].update({"accepted": 1}),
            "Boolean",
        ),
        (
            lambda data: data["terms"][0].update({"normalized": "Not Normalized"}),
            "canonical",
        ),
    ],
)
def test_schema_rejects_invalid_state(
    mutation: Any,
    match: str,
) -> None:
    data = populated_data()
    mutation(data)
    with pytest.raises(ValidationError, match=match):
        DiscoveryVocabulary.model_validate(data)


def test_duplicate_yaml_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "vocabulary.yaml"
    path.write_text(
        "schema_version: 1\nrevision: 0\nrevision: 1\n"
        "updated_on: 2026-08-12\nsources: []\nterms: []\n",
        encoding="utf-8",
    )
    with pytest.raises(VocabularyValidationError, match="duplicate"):
        load_vocabulary(path)


def test_serialization_is_canonical_and_byte_identical() -> None:
    data = populated_data()
    second = deepcopy(data)
    second["sources"][0]["resource_ids"] = ["tool-zeta", "tool-example"]
    first = deepcopy(second)
    first["sources"][0]["resource_ids"].reverse()

    assert serialize_vocabulary(
        DiscoveryVocabulary.model_validate(first)
    ) == serialize_vocabulary(DiscoveryVocabulary.model_validate(second))


def test_apply_increments_once_and_rejects_stale_revision(tmp_path: Path) -> None:
    path = tmp_path / "vocabulary.yaml"
    write_vocabulary(path, populated_data())
    proposed = DiscoveryVocabulary.model_validate(populated_data())

    updated = apply_vocabulary(path, proposed, expected_revision=0)
    assert updated.revision == 1
    with pytest.raises(VocabularyRevisionError, match="found 1"):
        apply_vocabulary(path, proposed, expected_revision=0)
    assert load_vocabulary(path).revision == 1
    assert not list(tmp_path.glob(".vocabulary.yaml.*.tmp"))
    assert not (tmp_path / ".vocabulary.yaml.lock").exists()


def test_invalid_update_and_replace_failure_roll_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "vocabulary.yaml"
    write_vocabulary(path, populated_data())
    before = path.read_bytes()
    invalid = populated_data()
    invalid["revision"] = 9
    with pytest.raises(VocabularyPolicyError, match="input revision"):
        apply_vocabulary(path, invalid, expected_revision=0)
    assert path.read_bytes() == before

    monkeypatch.setattr(
        os,
        "replace",
        lambda source, destination: (_ for _ in ()).throw(OSError("synthetic")),
    )
    with pytest.raises(VocabularyWriteError, match="atomically replace"):
        apply_vocabulary(path, populated_data(), expected_revision=0)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".vocabulary.yaml.*.tmp"))


def test_vocabulary_update_does_not_touch_discovery_ledger(tmp_path: Path) -> None:
    vocabulary = tmp_path / "vocabulary.yaml"
    ledger = tmp_path / "discovery-ledger.yaml"
    write_vocabulary(vocabulary, populated_data())
    ledger.write_bytes(b"schema_version: 1\nrevision: 73\n")
    before = ledger.read_bytes()

    apply_vocabulary(vocabulary, populated_data(), expected_revision=0)

    assert ledger.read_bytes() == before


def test_reconciliation_orphans_without_deleting_history() -> None:
    vocabulary = DiscoveryVocabulary.model_validate(populated_data())

    orphaned = reconcile_inventory_sources(vocabulary, [], on=date(2026, 8, 13))

    assert orphaned.sources[0].status == "orphaned"
    assert orphaned.sources[0].resource_ids == []
    assert orphaned.terms[0].active is False
    wording = orphaned.terms[0].wordings[0]
    assert wording.source_urls == []
    assert wording.inactive_source_urls == ["https://www.example.com/project"]
    assert orphaned.sources[0].decisions[0].active is False


def test_new_source_selection_and_access_gap_preserve_terms() -> None:
    empty = DiscoveryVocabulary(
        schema_version=1,
        revision=0,
        updated_on=date(2026, 8, 12),
        sources=[],
        terms=[],
    )
    reconciled = reconcile_inventory_sources(
        empty,
        [
            InventoryURL(
                url="https://www.example.com/new",
                kinds=(URLKind.HOMEPAGE,),
                resource_ids=("tool-example",),
                source_fields=("links.homepage",),
            )
        ],
        on=date(2026, 8, 12),
    )
    assert [
        source.url
        for source in select_sources_for_refresh(reconciled, on=date(2026, 8, 12))
    ] == ["https://www.example.com/new"]

    gapped = mark_source_access_gap(
        reconciled,
        source_url="https://www.example.com/new",
        attempted_on=date(2026, 8, 12),
        reason="synthetic timeout",
    )
    assert gapped.sources[0].gap is not None
    assert gapped.sources[0].gap.inventory_availability_claim is False
    assert select_sources_for_refresh(gapped, on=date(2026, 8, 18)) == ()
    assert len(select_sources_for_refresh(gapped, on=date(2026, 8, 19))) == 1
