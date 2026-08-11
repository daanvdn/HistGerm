from __future__ import annotations

from datetime import date
from pathlib import Path

from histgerm.loading import load_yaml_mapping_bytes
from histgerm.models import Tool
from histgerm.validation import validate_inventory

YAML_PATH = (
    Path(__file__).parents[2] / "src" / "histgerm" / "data" / "tools" / "rnntagger.yaml"
)


def _load_rnntagger() -> tuple[dict[str, object], Tool]:
    payload = load_yaml_mapping_bytes(
        YAML_PATH.read_bytes(), source_path="tools/rnntagger.yaml"
    )
    return payload, Tool.model_validate(payload)


def test_rnntagger_yaml_safely_loads_and_validates() -> None:
    payload, tool = _load_rnntagger()
    inventory = validate_inventory(YAML_PATH)

    assert payload["id"] == "res-rnntagger"
    assert inventory.tools == (tool,)
    assert tool.name == "RNNTagger"
    assert tool.description == (
        "Neural command-line tool for part-of-speech tagging and lemmatization "
        "with pretrained parameters for modern and historical languages."
    )
    assert tool.reviewed_on == date(2026, 8, 11)
    assert [task.value for task in tool.tasks] == ["lemmatizer", "pos_tagger"]
    assert [stage.value for stage in tool.supported_stages or []] == ["enhg", "mhg"]
    assert tool.input_formats == ["plain_text"]
    assert tool.output_formats == ["plain_text"]


def test_rnntagger_links_and_technical_details_are_preserved() -> None:
    payload, tool = _load_rnntagger()
    links = {key: str(value) for key, value in (tool.links or {}).items()}

    assert links == {
        "homepage": "https://www.cis.uni-muenchen.de/~schmid/tools/RNNTagger/",
        "documentation": "https://www.cis.uni-muenchen.de/~schmid/tools/RNNTagger/",
        "download": (
            "https://www.cis.uni-muenchen.de/~schmid/tools/RNNTagger/data/"
            "RNNTagger-1.5.0.zip"
        ),
        "installation": "https://www.cis.uni-muenchen.de/~schmid/tools/RNNTagger/",
        "usage": "https://www.cis.uni-muenchen.de/~schmid/tools/RNNTagger/",
        "license": (
            "https://www.cis.uni-muenchen.de/~schmid/tools/RNNTagger/Tagger-Licence"
        ),
        "citation": "https://doi.org/10.1145/3322905.3322915",
    }
    assert tool.note is not None
    for detail in (
        "Helmut Schmid (person, creator)",
        "Version 1.5.0",
        "RNNTagger-1.5.0.zip",
        "ZIP format",
        "package name: RNNTagger",
        "Python",
        "PyTorch",
        "cmd/rnn-tagger-english.sh",
        "Installation and usage",
    ):
        assert detail in tool.note

    omitted = {
        "aliases",
        "training_data",
        "evaluation_data",
        "reported_metrics",
        "hugging_face_links",
        "repository",
        "api",
        "release_date",
        "maintenance",
        "model_architecture",
        "model_license",
    }
    assert omitted.isdisjoint(payload)


def test_rnntagger_access_keeps_software_terms_and_permissions_distinct() -> None:
    _, tool = _load_rnntagger()
    access = tool.access

    assert [value.value for value in access.availability] == [
        "described",
        "downloadable",
    ]
    assert access.license == "RNNTagger License"
    assert str(access.license_url) == (
        "https://www.cis.uni-muenchen.de/~schmid/tools/RNNTagger/Tagger-Licence"
    )
    assert access.requirements == [
        "Free software use is limited to evaluation, research, and teaching purposes.",
        "Any other use, in particular commercial use, requires a commercial license.",
        "Redistribution of the RNNTagger software to other persons requires "
        "written permission.",
    ]
    permissions = (
        access.model_training,
        access.original_data_redistribution,
        access.processed_data_redistribution,
        access.trained_weight_publication,
    )
    assert [permission.value for permission in permissions] == ["unclear"] * 4
    assert access.note is not None
    assert "do not separately identify the pretrained parameter files as models" in (
        access.note
    )
    assert "Automated access was also unclear" in access.note
    assert access.source_ids == [
        "evidence-rnntagger-homepage",
        "evidence-rnntagger-license",
    ]


def test_rnntagger_sources_preserve_all_evidence_values() -> None:
    _, tool = _load_rnntagger()
    sources = {source.id: source for source in tool.sources}
    expected_ids = {
        "evidence-rnntagger-homepage",
        "evidence-rnntagger-license",
        "evidence-rnntagger-publication",
        "evidence-rnntagger-publication-crossref",
        "evidence-rnntagger-publication-project",
    }

    assert set(sources) == expected_ids
    assert {source.accessed_on for source in sources.values()} == {date(2026, 8, 11)}

    homepage = sources["evidence-rnntagger-homepage"]
    assert str(homepage.url) == (
        "https://www.cis.uni-muenchen.de/~schmid/tools/RNNTagger/"
    )
    assert homepage.quote == (
        "The RNNTagger is a tool for annotating text with part-of-speech and "
        "lemma information. It comes with pretrained parameter files for over "
        "50 modern and historical languages. RNNTagger was implemented in "
        "Python using the PyTorch library."
    )
    assert homepage.supports == [
        "name",
        "description",
        "links",
        "tasks",
        "supported_stages",
        "input_formats",
        "output_formats",
        "access.availability",
        "note",
    ]
    assert homepage.note == (
        "The page lists Early New High German and Middle High German among the "
        "supported historical languages and documents the 1.5.0 ZIP download, "
        "installation requirements, command-line example, output example, and "
        "citation."
    )

    license_source = sources["evidence-rnntagger-license"]
    assert str(license_source.url) == (
        "https://www.cis.uni-muenchen.de/~schmid/tools/RNNTagger/Tagger-Licence"
    )
    assert license_source.supports == ["access"]
    assert license_source.quote == (
        "You can freely use the RNNTagger software for evaluation, research and "
        "teaching purposes. Any other usage of the system (in particular for "
        "commercial purposes) requires a commercial license. You are not "
        "allowed to distribute the RNNTagger software to other persons without "
        "written permission."
    )
    assert license_source.note == (
        "The terms expressly address software use and software redistribution, "
        "but do not separately identify the pretrained parameter files as "
        "models or address model training, processed data, or publication of "
        "trained weights."
    )

    citation = (
        "Schmid, Helmut. 2019. Deep Learning-Based Morphological Taggers and "
        "Lemmatizers for Annotating Historical Texts. DATeCH, 133–137. "
        "https://doi.org/10.1145/3322905.3322915"
    )
    for source_id in (
        "evidence-rnntagger-publication",
        "evidence-rnntagger-publication-crossref",
        "evidence-rnntagger-publication-project",
    ):
        assert sources[source_id].citation == citation
        assert sources[source_id].supports == ["links.citation", "sources"]

    crossref = sources["evidence-rnntagger-publication-crossref"]
    assert str(crossref.url) == (
        "https://api.crossref.org/works/10.1145/3322905.3322915"
    )
    assert crossref.quote == (
        "Deep Learning-Based Morphological Taggers and Lemmatizers for "
        "Annotating Historical Texts"
    )
    assert crossref.note == (
        "Crossref identifies the ACM proceedings article, author Helmut Schmid, "
        "publication date 2019-05-08, pages 133–137, and DOI "
        "10.1145/3322905.3322915."
    )
    publication = sources["evidence-rnntagger-publication"]
    assert str(publication.url) == str(crossref.url)
    assert publication.quote == crossref.quote
    assert publication.note == crossref.note

    project = sources["evidence-rnntagger-publication-project"]
    assert str(project.url) == (
        "https://www.cis.uni-muenchen.de/~schmid/tools/RNNTagger/"
    )
    assert project.quote == (
        "Helmut Schmid (2019). Deep Learning-Based Morphological Taggers and "
        "Lemmatizers for Annotating Historical Texts, DATeCH, May 2019, "
        "Brussels, Belgium."
    )
    assert project.note == (
        "The canonical tool page identifies this paper as the RNNTagger citation."
    )
