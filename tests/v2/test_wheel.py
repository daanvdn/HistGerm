"""Wheel-boundary tests for the simplified HistGerm package."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).parents[2]
AUTHORED_YAML = [
    "histgerm/data/corpora/rem.yaml",
    "histgerm/data/dictionaries/mwb.yaml",
    "histgerm/data/tools/rnntagger.yaml",
]


@pytest.fixture(scope="session")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build one wheel for package-content and installed-import smoke tests."""

    output = tmp_path_factory.mktemp("wheel")
    subprocess.run(
        ["uv", "build", "--wheel", "--no-sources", "--out-dir", str(output)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(output.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_wheel_contains_only_three_authored_data_files_once(
    built_wheel: Path,
) -> None:
    """Exclude generated inventories and duplicate or third-party data."""

    with ZipFile(built_wheel) as archive:
        names = archive.namelist()

    packaged_data = [
        name
        for name in names
        if name.startswith("histgerm/data/") and not name.endswith("/")
    ]
    assert packaged_data == AUTHORED_YAML
    assert all(names.count(path) == 1 for path in AUTHORED_YAML)
    forbidden_names = {"manifest.json", "snapshot.json", "inventory.json"}
    assert not any(Path(name).name in forbidden_names for name in names)
    assert not any(name.endswith(".json") for name in packaged_data)


def test_installed_wheel_imports_loads_yaml_and_queries_each_type(
    built_wheel: Path, tmp_path: Path
) -> None:
    """Install the wheel away from the checkout and smoke-test its public API."""

    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    venv = tmp_path / "venv"
    subprocess.run(
        [
            "uv",
            "venv",
            str(venv),
            "--python",
            f"{sys.version_info.major}.{sys.version_info.minor}",
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), str(built_wheel)],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    script = "\n".join(
        [
            "import histgerm",
            "from histgerm.catalog import load_catalog",
            "from histgerm.loading import discover_bundled_yaml, load_bundled_yaml",
            f"assert discover_bundled_yaml() == {tuple(AUTHORED_YAML)!r}".replace(
                "histgerm/data/", ""
            ),
            "assert load_bundled_yaml('corpora/rem.yaml')['id'] == 'res-rem'",
            "assert load_bundled_yaml('dictionaries/mwb.yaml')['id'] == 'res-mwb'",
            "assert load_bundled_yaml('tools/rnntagger.yaml')['id'] == 'res-rnntagger'",
            "catalog = load_catalog()",
            "assert catalog.find_corpora(stage='mhg')[0].id == 'res-rem'",
            "assert catalog.find_tools(task='lemmatizer')[0].id == 'res-rnntagger'",
            "assert catalog.find_dictionaries("
            "machine_readable=True)[0].id == 'res-mwb'",
        ]
    )
    subprocess.run(
        [str(python), "-c", script],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def test_repository_has_no_duplicate_inventory_or_third_party_payloads() -> None:
    """Keep authored metadata in package YAML without local resource payloads."""

    assert not (ROOT / "inventory").exists()
    assert not (ROOT / "src" / "histgerm" / "resources").exists()
    data_root = ROOT / "src" / "histgerm" / "data"
    files = sorted(path for path in data_root.rglob("*") if path.is_file())
    assert [
        path.relative_to(ROOT / "src").as_posix() for path in files
    ] == AUTHORED_YAML

    payload_suffixes = {
        ".7z",
        ".conll",
        ".conllu",
        ".csv",
        ".json",
        ".tar",
        ".tsv",
        ".xml",
        ".zip",
    }
    tracked = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert not [
        path
        for path in tracked
        if Path(path).suffix.casefold() in payload_suffixes
        and not path.startswith("tests/v2/")
    ]
