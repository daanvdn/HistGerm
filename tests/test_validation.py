"""Focused tests for HistGerm inventory validation."""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path
from subprocess import run
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

from histgerm.loading import discover_bundled_yaml, load_bundled_yaml
from histgerm.models import Corpus, Dictionary, Tool
from histgerm.validation import InventoryValidationError, validate_inventory


def _source(
    source_id: str = "source-main",
    *,
    supports: list[str] | None = None,
    quote: str | None = "Direct legal evidence.",
) -> dict[str, Any]:
    """Build one valid source mapping."""

    return {
        "id": source_id,
        "url": "https://example.org/evidence",
        "accessed_on": "2026-08-11",
        "supports": ["name"] if supports is None else supports,
        "quote": quote,
    }


def _access(
    permission: str = "unclear", source_ids: list[str] | None = None
) -> dict[str, Any]:
    """Build explicit access and legal permission metadata."""

    return {
        "availability": ["described"],
        "model_training": permission,
        "original_data_redistribution": permission,
        "processed_data_redistribution": permission,
        "trained_weight_publication": permission,
        "source_ids": source_ids,
    }


def _text(text_id: str = "text-one") -> dict[str, Any]:
    """Build one valid inline corpus text."""

    return {
        "id": text_id,
        "title": "Text One",
        "stages": ["mhg"],
        "dialect": "West Central German",
        "date": "around 1200",
        "annotation_ids": ["lemma"],
        "source_ids": ["source-main"],
    }


def _version(version_id: str = "v1", text_id: str = "text-one") -> dict[str, Any]:
    """Build one valid corpus version."""

    return {
        "id": version_id,
        "availability": ["downloadable"],
        "annotations": [
            {
                "id": "lemma",
                "type": "lemma",
                "source_ids": ["source-main"],
            }
        ],
        "texts": [_text(text_id)],
        "source_ids": ["source-main"],
    }


def _corpus(corpus_id: str = "corpus-one") -> dict[str, Any]:
    """Build one valid corpus resource."""

    return {
        "id": corpus_id,
        "name": "Corpus One",
        "sources": [_source(supports=["name", "covered_stages"])],
        "reviewed_on": "2026-08-11",
        "covered_stages": ["mhg"],
        "access": _access(),
        "versions": [_version()],
    }


def _dictionary(dictionary_id: str = "dictionary-one") -> dict[str, Any]:
    """Build one valid dictionary resource."""

    return {
        "id": dictionary_id,
        "name": "Dictionary One",
        "sources": [_source()],
        "reviewed_on": "2026-08-11",
        "access": _access(),
    }


def _write(root: Path, category: str, name: str, payload: Any) -> Path:
    """Write a YAML fixture beneath an explicit resource category."""

    path = root / category / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def _failure(root: Path) -> str:
    """Validate a fixture and return its explicit failure text."""

    with pytest.raises(InventoryValidationError) as caught:
        validate_inventory(root)
    return str(caught.value)


def test_bundled_inventory_validates() -> None:
    """Validate the bundled V2 resources without mutation."""

    root = Path(__file__).parents[1] / "src" / "histgerm" / "data"
    inventory = validate_inventory(root)
    paths = discover_bundled_yaml()
    payload_ids = {path: load_bundled_yaml(path)["id"] for path in paths}
    expected_types = {
        "corpora": Corpus,
        "dictionaries": Dictionary,
        "tools": Tool,
    }

    assert inventory.source_paths == {
        resource_id: path for path, resource_id in payload_ids.items()
    }
    assert len(inventory.resources) == len(paths)
    assert len({resource.id for resource in inventory.resources}) == len(paths)
    for resource in inventory.resources:
        path = inventory.source_paths[resource.id]
        assert resource.id == payload_ids[path]
        assert isinstance(resource, expected_types[path.partition("/")[0]])

    corpus_ids = {corpus.id for corpus in inventory.corpora}
    assert all(
        set(dictionary.corpus_links or []).issubset(corpus_ids)
        for dictionary in inventory.dictionaries
    )


def test_empty_inventory_is_rejected_as_missing_resources(tmp_path: Path) -> None:
    """Reject an inventory where discovery finds no authored resources."""

    assert "contains no YAML resource files" in _failure(tmp_path)


@pytest.mark.parametrize(
    "content",
    [
        "id: [unterminated\n",
        "id: first\nid: second\n",
        "defaults: &defaults\n  value: one\ncopy: *defaults\n",
    ],
)
def test_restricted_yaml_failures_are_explicit(tmp_path: Path, content: str) -> None:
    """Reject malformed, duplicate-key, and aliased YAML documents."""

    path = tmp_path / "corpora" / "bad.yaml"
    path.parent.mkdir()
    path.write_text(content, encoding="utf-8")

    message = _failure(tmp_path)
    assert "inventory validation failed" in message
    assert "bad.yaml" in message


def test_unknown_fields_fail_top_level_model_validation(tmp_path: Path) -> None:
    """Reject fields outside the selected top-level model."""

    data = _corpus()
    data["manifest"] = "forbidden"
    _write(tmp_path, "corpora", "corpus.yaml", data)

    assert "Extra inputs are not permitted" in _failure(tmp_path)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda data: data["sources"].append(deepcopy(data["sources"][0])),
            "resource source IDs",
        ),
        (
            lambda data: data["versions"].append(deepcopy(data["versions"][0])),
            "corpus version IDs",
        ),
        (
            lambda data: data["versions"][0]["annotations"].append(
                deepcopy(data["versions"][0]["annotations"][0])
            ),
            "annotation layer IDs",
        ),
        (
            lambda data: data["versions"][0]["texts"].append(
                deepcopy(data["versions"][0]["texts"][0])
            ),
            "text IDs",
        ),
        (
            lambda data: data["versions"].append(_version("v2", "text-one")),
            "text IDs across",
        ),
    ],
)
def test_duplicate_ids_fail_at_every_resource_scope(
    tmp_path: Path, mutation: Any, expected: str
) -> None:
    """Reject duplicate source, version, layer, and text identifiers."""

    data = _corpus()
    mutation(data)
    _write(tmp_path, "corpora", "corpus.yaml", data)

    assert expected in _failure(tmp_path)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda data: data["versions"][0].update({"source_ids": ["missing"]}),
            "unknown source ID",
        ),
        (
            lambda data: data["versions"][0]["annotations"][0].update(
                {"source_ids": ["missing"]}
            ),
            "unknown source ID",
        ),
        (
            lambda data: data["versions"][0]["texts"][0].update(
                {"source_ids": ["missing"]}
            ),
            "unknown source ID",
        ),
        (
            lambda data: data["versions"][0]["texts"][0].update(
                {"annotation_ids": ["missing"]}
            ),
            "unknown annotation IDs",
        ),
    ],
)
def test_local_sources_and_layers_must_resolve(
    tmp_path: Path, mutation: Any, expected: str
) -> None:
    """Resolve version, layer, text source IDs and owned annotation layers."""

    data = _corpus()
    mutation(data)
    _write(tmp_path, "corpora", "corpus.yaml", data)

    assert expected in _failure(tmp_path)


@pytest.mark.parametrize(
    ("target", "scope_message"),
    [
        ("bare-text", "qualified text IDs"),
        ("corpus-one:bad_id", "with must be"),
    ],
)
def test_text_overlap_qualification_and_scope_are_enforced(
    tmp_path: Path, target: str, scope_message: str
) -> None:
    """Reject unqualified or syntactically invalid text overlap targets."""

    data = _corpus()
    data["versions"][0]["texts"][0]["overlaps"] = [
        {"relationship": "overlaps", "with": target, "note": "Known overlap."}
    ]
    _write(tmp_path, "corpora", "corpus.yaml", data)

    assert scope_message in _failure(tmp_path)


def test_unresolved_inventory_overlap_targets_fail(tmp_path: Path) -> None:
    """Reject unmarked corpus and text overlap targets absent from inventory."""

    corpus = _corpus()
    corpus["overlaps"] = [
        {"relationship": "overlaps", "with": "corpus-missing", "note": "Known."}
    ]
    corpus["versions"][0]["texts"][0]["overlaps"] = [
        {
            "relationship": "same_work",
            "with": "corpus-missing:text-two",
            "note": "Known.",
        }
    ]
    _write(tmp_path, "corpora", "corpus.yaml", corpus)

    assert "unknown corpus overlap target" in _failure(tmp_path)


def test_unresolved_inventory_text_overlap_target_fails(tmp_path: Path) -> None:
    """Reject an unmarked qualified text target absent from inventory."""

    corpus = _corpus()
    corpus["versions"][0]["texts"][0]["overlaps"] = [
        {
            "relationship": "same_work",
            "with": "corpus-missing:text-two",
            "note": "Known.",
        }
    ]
    _write(tmp_path, "corpora", "corpus.yaml", corpus)

    assert "unknown text overlap target" in _failure(tmp_path)


def test_unresolved_self_corpus_text_target_fails_locally(tmp_path: Path) -> None:
    """Reject an unknown qualified text owned by the same corpus."""

    corpus = _corpus()
    corpus["versions"][0]["texts"][0]["overlaps"] = [
        {
            "relationship": "same_work",
            "with": "corpus-one:text-missing",
            "note": "Known.",
        }
    ]
    _write(tmp_path, "corpora", "corpus.yaml", corpus)

    assert "unknown local text" in _failure(tmp_path)


def test_explicit_external_overlap_targets_are_accepted(tmp_path: Path) -> None:
    """Allow clearly marked external corpus and qualified text targets."""

    corpus = _corpus()
    corpus["overlaps"] = [
        {
            "relationship": "overlaps",
            "with": "external:corpus-missing",
            "note": "External corpus.",
        }
    ]
    corpus["versions"][0]["texts"][0]["overlaps"] = [
        {
            "relationship": "same_work",
            "with": "external:corpus-missing:text-two",
            "note": "External text.",
        }
    ]
    _write(tmp_path, "corpora", "corpus.yaml", corpus)

    assert len(validate_inventory(tmp_path).corpora) == 1


def test_dictionary_corpus_links_resolve_inventory_wide(tmp_path: Path) -> None:
    """Reject a dictionary link to an absent inventory corpus."""

    dictionary = _dictionary()
    dictionary["corpus_links"] = ["corpus-missing"]
    _write(tmp_path, "dictionaries", "dictionary.yaml", dictionary)

    assert "unknown corpus reference" in _failure(tmp_path)


@pytest.mark.parametrize(
    ("quote", "supports", "expected"),
    [
        (None, ["access.model_training"], "direct quote"),
        ("Terms permit use.", ["access"], "supports="),
    ],
)
def test_non_unclear_legal_claims_need_direct_support(
    tmp_path: Path,
    quote: str | None,
    supports: list[str],
    expected: str,
) -> None:
    """Require exact legal support scope and a direct quotation."""

    corpus = _corpus()
    corpus["sources"] = [_source(supports=["covered_stages", *supports], quote=quote)]
    corpus["access"] = _access("permitted", ["source-main"])
    _write(tmp_path, "corpora", "corpus.yaml", corpus)

    assert expected in _failure(tmp_path)


def test_explicit_unclear_legal_values_are_retained(tmp_path: Path) -> None:
    """Accept and preserve explicit unclear permissions without inference."""

    _write(tmp_path, "corpora", "corpus.yaml", _corpus())

    access = validate_inventory(tmp_path).corpora[0].access
    assert access.model_training.value == "unclear"
    assert access.original_data_redistribution.value == "unclear"


def test_duplicate_resource_ids_fail_across_files(tmp_path: Path) -> None:
    """Reject duplicate inventory resource IDs across category files."""

    _write(tmp_path, "corpora", "first.yaml", _corpus("corpus-shared"))
    _write(tmp_path, "corpora", "second.yaml", _corpus("corpus-shared"))

    assert "duplicate resource ID 'corpus-shared'" in _failure(tmp_path)


def test_duplicate_qualified_text_ids_fail_inventory_checks(tmp_path: Path) -> None:
    """Report duplicate fully qualified text IDs across duplicate corpus files."""

    _write(tmp_path, "corpora", "first.yaml", _corpus())
    _write(tmp_path, "corpora", "second.yaml", _corpus())

    message = _failure(tmp_path)
    assert "duplicate resource ID 'corpus-one'" in message
    assert "duplicate qualified text ID 'corpus-one:text-one'" in message


def test_unknown_resource_directory_does_not_trigger_shape_inference(
    tmp_path: Path,
) -> None:
    """Require explicit directory dispatch rather than guessing a model."""

    _write(tmp_path, "resources", "corpus.yaml", _corpus())

    assert "resource directories" in _failure(tmp_path)


def test_cli_returns_zero_only_for_valid_inventory(tmp_path: Path) -> None:
    """Return success for valid YAML and explicit failure for invalid YAML."""

    _write(tmp_path, "corpora", "corpus.yaml", _corpus())
    valid = run(
        [sys.executable, "-m", "histgerm.validation", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    bad = tmp_path / "corpora" / "bad.yaml"
    bad.write_text("id: [unterminated\n", encoding="utf-8")
    invalid = run(
        [sys.executable, "-m", "histgerm.validation", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert valid.returncode == 0
    assert "Validated 1 HistGerm resource" in valid.stdout
    assert valid.stderr == ""
    assert invalid.returncode == 1
    assert "validation failed" in invalid.stderr
    assert "Validated" not in invalid.stdout
