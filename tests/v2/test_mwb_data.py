from __future__ import annotations

from pathlib import Path
from typing import Any

from histgerm.loading import load_yaml_mapping_bytes
from histgerm.models import Dictionary
from histgerm.validation import validate_inventory

ROOT = Path(__file__).parents[2]
MWB_PATH = ROOT / "src" / "histgerm" / "data" / "dictionaries" / "mwb.yaml"


def _payload() -> dict[str, Any]:
    return load_yaml_mapping_bytes(
        MWB_PATH.read_bytes(),
        source_path="dictionaries/mwb.yaml",
    )


def test_mwb_yaml_safely_loads_and_validates() -> None:
    payload = _payload()
    dictionary = Dictionary.model_validate(payload)
    inventory = validate_inventory(MWB_PATH)

    assert dictionary.id == "dictionary-mwb"
    assert inventory.dictionaries == (dictionary,)
    assert inventory.source_paths == {"dictionary-mwb": "mwb.yaml"}


def test_mwb_preserves_identity_scope_and_online_state() -> None:
    dictionary = Dictionary.model_validate(_payload())

    assert dictionary.name == "Mittelhochdeutsches Wörterbuch"
    assert dictionary.aliases == ["MWB"]
    assert dictionary.covered_stages == ["mhg"]
    assert dictionary.covered_languages == ["German"]
    assert dictionary.reviewed_on.isoformat() == "2026-08-11"
    assert dictionary.description is not None
    assert dictionary.note is not None
    assert "1050–1350" in dictionary.description
    assert "a–merhensun" in dictionary.note
    assert "approximately 27,400 articles" in dictionary.note
    assert "not an exact size" in dictionary.note


def test_mwb_preserves_verified_interfaces_and_explicit_unknowns() -> None:
    payload = _payload()
    dictionary = Dictionary.model_validate(payload)

    assert dictionary.access.availability == ["described", "browsable", "api"]
    assert dictionary.machine_readable is True
    assert "download_links" not in payload
    assert "license" not in payload["access"]
    assert dictionary.corpus_links is None
    assert dictionary.note is not None
    assert "no verified HistGerm corpus identifier or link unit" in dictionary.note
    assert dictionary.access.model_training == "unclear"
    assert dictionary.access.original_data_redistribution == "unclear"
    assert dictionary.access.processed_data_redistribution == "unclear"
    assert dictionary.access.trained_weight_publication == "unclear"
    assert dictionary.access.note is not None
    assert dictionary.access.requirements is not None
    assert dictionary.note is not None
    assert "No bulk download" in dictionary.access.note
    assert "No authentication" in dictionary.access.requirements[0]
    assert "JSON encoded as UTF-8" in dictionary.note
    assert "API parameters, output, and continuous availability may change" in (
        dictionary.note
    )


def test_mwb_preserves_only_verified_lexical_and_scholarly_facts() -> None:
    payload = _payload()
    dictionary = Dictionary.model_validate(payload)

    assert dictionary.lexical_features == [
        "headwords",
        "lemmas",
        "spelling variants",
        "part of speech",
        "senses",
    ]
    assert "morphology" not in dictionary.lexical_features
    assert "etymology" not in dictionary.lexical_features
    assert dictionary.citation_detail is not None
    assert dictionary.note is not None
    assert "source sigla link to bibliography records" in (
        dictionary.citation_detail.lower()
    )
    assert "hidden homographs" in dictionary.note
    assert "six-month protection period" in dictionary.note
    assert "ISBN-13 9783777623276" in dictionary.note
    assert "No complete ISO publication date was established" in dictionary.note


def test_mwb_preserves_every_reviewed_source_value() -> None:
    dictionary = Dictionary.model_validate(_payload())
    sources = {source.id: source for source in dictionary.sources}

    assert set(sources) == {
        "evidence-mwb-project",
        "evidence-mwb-publication-status",
        "evidence-mwb-usage",
        "evidence-mwb-entry",
        "evidence-mwb-api",
        "evidence-mwb-api-query",
        "evidence-mwb-network",
        "evidence-mwb-publisher",
        "evidence-mwb-band-1-project",
        "evidence-mwb-band-1-publisher",
    }
    assert all(
        source.accessed_on.isoformat() == "2026-08-11" for source in sources.values()
    )
    assert all(
        source.quote and source.note and source.supports for source in sources.values()
    )
    assert sources["evidence-mwb-publication-status"].quote == (
        "Online a – merhensun (rund 27.400 Artikel)"
    )
    assert sources["evidence-mwb-api-query"].quote == (
        '{"sigle":"MWB","lemma":"abe","gram":"Adv.",'
        '"wbnetzid":"247419600","bookref":"247419600"}'
    )
    assert sources["evidence-mwb-band-1-publisher"].note == (
        "Canonical publisher record and ISBN-bearing URL; no complete ISO "
        "publication date was established."
    )
