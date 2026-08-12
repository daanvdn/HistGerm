from __future__ import annotations

import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from histgerm.loading import load_yaml_mapping_bytes
from histgerm.models import Corpus
from histgerm.validation import validate_inventory

ROOT = Path(__file__).parents[1]
REM_PATH = ROOT / "src" / "histgerm" / "data" / "corpora" / "rem.yaml"
COMMON_ANNOTATIONS = {
    "lemma",
    "pos",
    "morphology",
    "normalization",
    "tokenization",
}
CANONICAL_ID_RE = re.compile(r"Canonical ReM ID: (M\d{3}[A-Z0-9]*)\.")


def _load_rem() -> tuple[dict[str, Any], Corpus]:
    payload = load_yaml_mapping_bytes(
        REM_PATH.read_bytes(),
        source_path="corpora/rem.yaml",
    )
    return payload, Corpus.model_validate(payload)


def _normalized_id(canonical_id: str) -> str:
    match = re.fullmatch(r"M(\d{3})([A-Z0-9]*)", canonical_id)
    assert match is not None
    suffix = match.group(2).lower()
    return f"m{match.group(1)}" + (f"-{suffix}" if suffix else "")


def _text_map(corpus: Corpus) -> dict[str, Any]:
    return {text.id: text for text in corpus.versions[0].texts}


def test_rem_yaml_safely_loads_and_validates() -> None:
    payload, corpus = _load_rem()
    inventory = validate_inventory(REM_PATH)

    assert payload["id"] == "corpus-rem"
    assert inventory.corpora == (corpus,)
    assert inventory.source_paths == {"corpus-rem": "rem.yaml"}
    assert corpus.reviewed_on == date(2026, 8, 12)
    assert corpus.name == "Reference Corpus of Middle High German"
    assert corpus.aliases == ["ReM"]
    assert corpus.covered_stages == ["mhg"]


def test_rem_has_unique_normalized_canonical_text_ids() -> None:
    _, corpus = _load_rem()
    texts = corpus.versions[0].texts
    ids = [text.id for text in texts]
    canonical_ids: list[str] = []

    assert texts
    assert len(ids) == len(set(ids))
    assert ids[0] == "m001"
    assert ids[-1] == "m552"
    assert "m057" not in ids

    for text in texts:
        assert text.note is not None
        match = CANONICAL_ID_RE.search(text.note)
        assert match is not None
        canonical_id = match.group(1)
        canonical_ids.append(canonical_id)
        assert text.id == _normalized_id(canonical_id)

    assert len(canonical_ids) == len(set(canonical_ids))
    assert "M057" not in canonical_ids
    assert "M013B" in canonical_ids
    assert "M121Y" in canonical_ids


def test_rem_required_text_values_and_sources_are_exhaustive() -> None:
    _, corpus = _load_rem()
    version = corpus.versions[0]
    source_ids = {source.id for source in corpus.sources}

    for text in version.texts:
        assert text.title
        assert text.stages == ["mhg"]
        assert text.dialect
        assert text.date
        assert text.annotation_ids
        assert text.source_ids
        assert set(text.source_ids) <= source_ids
        assert text.shared_work_id is None
        assert text.authors is None
        assert text.genres is None
        assert text.regions is None
        assert text.witness_id is None
        assert text.edition_id is None
        assert text.sizes is None
        assert text.overlaps is None

    assert Counter(tuple(text.text_types or []) for text in version.texts) == {
        ("prose",): 192,
        ("verse",): 183,
        ("charter",): 21,
        ("prose", "verse"): 10,
    }


def test_rem_component_membership_controls_char_align() -> None:
    _, corpus = _load_rem()
    texts = corpus.versions[0].texts
    component_counts: Counter[str] = Counter()

    for text in texts:
        assert text.note is not None
        annotations = set(text.annotation_ids)
        assert annotations >= COMMON_ANNOTATIONS
        if "MiGraKo core corpus (-G)" in text.note:
            component_counts["G"] += 1
            assert annotations == COMMON_ANNOTATIONS | {"char-align"}
        else:
            component_counts["X"] += 1
            assert "ReM extension corpus (-X)" in text.note
            assert annotations == COMMON_ANNOTATIONS

    assert component_counts == {"G": 103, "X": 303}

    texts_by_id = _text_map(corpus)
    assert texts_by_id["m001"].title == "Ad equum errehet"
    assert "extension" in texts_by_id["m001"].note.lower()
    assert texts_by_id["m005"].title == "Aegidius, Trierer"
    assert "MiGraKo" in texts_by_id["m005"].note
    assert texts_by_id["m301"].title == "Athis und Prophilias"
    assert "char-align" in texts_by_id["m301"].annotation_ids
    assert texts_by_id["m552"].title == "Ostmitteldeutsche Urkunden"
    assert "char-align" not in texts_by_id["m552"].annotation_ids


def test_rem_m330_preserves_resolved_text_and_manuscript_metadata() -> None:
    _, corpus = _load_rem()
    m330 = _text_map(corpus)["m330"]

    assert m330.title == "Mitteldeutsche Predigten (K)"
    assert m330.dialect == (
        "Text composition: vermutl. wmd. (rhfrk.-hess.?); "
        "manuscript/script dialect: hessisch-thüringisch?"
    )
    assert m330.date == "Text: um 1200 / 13,1V; manuscript: 13,M"
    assert m330.text_types == ["prose"]
    assert m330.source_ids == ["evidence-rem-texts", "evidence-rem-details"]
    assert m330.note is not None
    assert "ANNIS group: 13_1-thurhess-PV-G" in m330.note
    assert "MiGraKo core corpus (-G)" in m330.note


def test_rem_annotation_layers_preserve_tagsets_and_uncertain_coverage() -> None:
    _, corpus = _load_rem()
    version = corpus.versions[0]
    layers = {layer.id: layer for layer in version.annotations}

    assert list(layers) == [
        "lemma",
        "pos",
        "morphology",
        "normalization",
        "char-align",
        "tokenization",
    ]
    assert layers["lemma"].tagset_name == (
        "Lexers Mittelhochdeutsches Handwörterbuch (Lexer)"
    )
    assert str(layers["lemma"].tagset_link) == "http://woerterbuchnetz.de/Lexer/"
    assert layers["pos"].tagset_name == "HiTS (Historisches Tagset)"
    assert str(layers["pos"].tagset_link) == (
        "https://linguistics.rub.de/comphist/resources/hits/"
    )
    assert layers["morphology"].tagset_name is None
    assert layers["normalization"].tagset_name is None
    assert layers["tokenization"].type == "other"
    assert layers["char-align"].note is not None
    assert "small but unidentified subset" in layers["char-align"].note
    assert "extension assignments are omitted" in layers["char-align"].note


def test_rem_version_preserves_release_components_formats_and_size() -> None:
    _, corpus = _load_rem()
    version = corpus.versions[0]
    links = {key: str(value) for key, value in (version.links or {}).items()}

    assert version.id == "ver-rem-2-1"
    assert version.label == "2.1"
    assert version.released_on == date(2024, 10, 28)
    assert version.license == (
        "Creative Commons Attribution-ShareAlike 4.0 International"
    )
    assert version.availability == ["described", "browsable", "downloadable"]
    assert links == {
        "changelog": "https://linguistics.rub.de/rem/access/NEWS",
        "dataset": "https://zenodo.org/records/13982324",
        "doi": "https://doi.org/10.5281/zenodo.13982324",
        "annis_browser": "https://newannis.linguistics.rub.de/rem",
        "download_coraxml": (
            "https://zenodo.org/record/13982324/files/ReM-v2.1_coraxml.zip?download=1"
        ),
        "download_tei": (
            "https://zenodo.org/record/13982324/files/ReM-v2.1_tei.zip?download=1"
        ),
        "download_json": (
            "https://zenodo.org/record/13982324/files/ReM-v2.1_json.zip?download=1"
        ),
        "download_graphml": (
            "https://zenodo.org/record/13982324/files/ReM-v2.1_graphml.zip?download=1"
        ),
        "schema_tei": "https://tei-c.org/release/doc/tei-p5-doc/en/html/index.html",
        "schema_tabular_json": (
            "https://linguistics.rub.de/~roussel/tabular-json/specification.html"
        ),
        "schema_coraxml": "https://cora.readthedocs.io/en/latest/coraxml/",
        "schema_graphml": "http://graphml.graphdrawing.org/",
        "conversion_software": "https://gitlab.rub.de/comphist/rem_convert",
    }
    assert version.sizes is not None
    assert len(version.sizes) == 1
    assert version.sizes[0].value == 2_000_000
    assert version.sizes[0].unit == "orthographic_word"
    assert "approximate" in (version.sizes[0].note or "")
    assert version.note is not None
    assert "MiGraKo is the structurally sampled core" in version.note
    assert "additional texts and passages" in version.note
    assert "TEI-compatible XML" in version.note
    assert "Tabular JSON" in version.note
    assert "CorA-XML" in version.note
    assert "GraphML" in version.note
    assert "TEI lemma attribute contains ANNIS norm" in version.note


def test_rem_m057_stale_page_conflict_is_preserved_and_resolved() -> None:
    _, corpus = _load_rem()
    sources = {source.id: source for source in corpus.sources}
    version = corpus.versions[0]

    assert sources["evidence-rem-news"].quote == "Removed: M057."
    manifest = sources["evidence-rem-zenodo-manifest"]
    assert str(manifest.url) == (
        "https://zenodo.org/api/records/13982324/files/ReM-v2.1_coraxml.zip/container"
    )
    assert manifest.note is not None
    assert "M056.xml and M058.xml but no M057.xml" in manifest.note
    assert "no corpus payload was downloaded" in manifest.note
    assert version.note is not None
    assert "texts.html retains a stale M057 row" in version.note
    assert "M057 is therefore excluded" in version.note


def test_rem_access_and_direct_legal_evidence_are_preserved() -> None:
    _, corpus = _load_rem()
    access = corpus.access
    sources = {source.id: source for source in corpus.sources}

    assert access.availability == ["described", "browsable", "downloadable"]
    assert access.model_training == "unclear"
    assert access.original_data_redistribution == "permitted"
    assert access.processed_data_redistribution == "unclear"
    assert access.trained_weight_publication == "unclear"
    assert access.license == (
        "Creative Commons Attribution-ShareAlike 4.0 International"
    )
    assert access.source_ids == ["evidence-rem-access", "evidence-rem-license"]
    assert access.requirements is not None
    assert "ISLRN 937-948-254-174-0" in access.requirements[1]

    license_source = sources["evidence-rem-license"]
    assert license_source.supports == [
        "access.original_data_redistribution",
        "access.requirements",
    ]
    assert license_source.quote is not None
    assert "grants You a worldwide, royalty-free" in license_source.quote


def test_rem_preserves_all_old_and_new_evidence_sources() -> None:
    _, corpus = _load_rem()
    sources = {source.id: source for source in corpus.sources}

    assert set(sources) == {
        "evidence-rem-homepage",
        "evidence-rem-access",
        "evidence-rem-zenodo",
        "evidence-rem-structure",
        "evidence-rem-layers",
        "evidence-rem-license",
        "evidence-rem-texts",
        "evidence-rem-details",
        "evidence-rem-metadata",
        "evidence-rem-lemma",
        "evidence-rem-pos",
        "evidence-rem-morphology",
        "evidence-rem-hits",
        "evidence-rem-hits-publication",
        "evidence-rem-news",
        "evidence-rem-zenodo-manifest",
    }
    assert {source.accessed_on for source in sources.values()} == {
        date(2026, 8, 11),
        date(2026, 8, 12),
    }
    assert sources["evidence-rem-zenodo"].accessed_on == date(2026, 8, 12)
    assert sources["evidence-rem-hits-publication"].accessed_on == date(2026, 8, 11)
    assert all(source.supports for source in sources.values())
    assert sources["evidence-rem-homepage"].quote == (
        "The “Reference Corpus of Middle High German” (short: ReM) is a corpus "
        "of diplomatically transcribed and annotated texts from Middle High "
        "German (1050–1350) with a size of around 2 million word forms."
    )
    assert "covered_stages" in sources["evidence-rem-homepage"].supports
    assert sources["evidence-rem-layers"].quote == (
        "char_align ist für das Kernkorpus, d.h. für alle MiGraKo-Texte, "
        "annotiert, aber nur für einen kleinen Teil der Texte im "
        "Erweiterungskorpus."
    )
    assert sources["evidence-rem-zenodo"].note is not None
    assert "no files were downloaded" in sources["evidence-rem-zenodo"].note.casefold()


def test_rem_does_not_infer_overlaps_or_shared_works() -> None:
    payload, corpus = _load_rem()

    assert "overlaps" not in payload
    assert corpus.overlaps is None
    assert all(text.shared_work_id is None for text in corpus.versions[0].texts)
    assert all(text.overlaps is None for text in corpus.versions[0].texts)
    assert corpus.notes is not None
    assert any("no structured overlap is asserted" in note for note in corpus.notes)
