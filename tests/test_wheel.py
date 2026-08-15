"""Wheel-boundary tests for the simplified HistGerm package."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tarfile
from collections import Counter
from pathlib import Path, PurePosixPath
from zipfile import ZipFile, ZipInfo

import pytest

ROOT = Path(__file__).parents[1]
DATA_ROOT = ROOT / "src" / "histgerm" / "data"
RESOURCE_CATEGORIES = ("corpora", "dictionaries", "tools")
RESEARCH_LEDGER = "research/discovery-ledger.yaml"
RESEARCH_VOCABULARY = "research/discovery-vocabulary.yaml"
MIGRATION_STATE = "migration-state.json"
FORBIDDEN_STATE_PARTS = {
    ".crawl4ai",
    ".playwright",
    "browser-pages",
    "browser-profiles",
    "browser-state",
    "crawl4ai-cache",
    "fetched-pages",
    "generated-markdown",
    "ms-playwright",
}
MAX_MEMBER_SIZE = 1024 * 1024
MAX_METADATA_SIZE = 512 * 1024
MAGIC_READ_SIZE = 16
PAYLOAD_SUFFIXES = {
    ".7z",
    ".avi",
    ".bin",
    ".bz2",
    ".conll",
    ".conllu",
    ".corpus",
    ".csv",
    ".db",
    ".dll",
    ".dylib",
    ".exe",
    ".gz",
    ".json",
    ".mdb",
    ".model",
    ".mov",
    ".mp3",
    ".mp4",
    ".onnx",
    ".parquet",
    ".pdf",
    ".pt",
    ".pth",
    ".rar",
    ".safetensors",
    ".so",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tgz",
    ".tsv",
    ".wav",
    ".weights",
    ".xml",
    ".xz",
    ".zip",
}
FORBIDDEN_MAGIC = (
    b"PK\x03\x04",
    b"\x1f\x8b",
    b"BZh",
    b"\xfd7zXZ\x00",
    b"7z\xbc\xaf\x27\x1c",
    b"Rar!",
    b"MZ",
    b"\x7fELF",
    b"SQLite format 3\x00",
)


def authored_yaml() -> list[str]:
    """Return the authored package-data paths in build-backend order."""

    return sorted(
        path.relative_to(ROOT / "src").as_posix()
        for path in DATA_ROOT.rglob("*")
        if path.is_file() and path.suffix.casefold() in {".yaml", ".yml"}
    )


def normalized_archive_path(name: str) -> PurePosixPath:
    """Return a safe, normalized relative archive path."""

    assert name
    assert "\\" not in name
    assert "\0" not in name
    path = PurePosixPath(name.rstrip("/"))
    assert not path.is_absolute()
    assert path.parts
    assert all(part not in {"", ".", ".."} for part in path.parts)
    assert ":" not in path.parts[0]
    assert path.as_posix() == name.rstrip("/")
    return path


def assert_safe_file(name: str, size: int, leading_bytes: bytes) -> None:
    """Reject payload-like, oversized, or stateful distribution files."""

    path = normalized_archive_path(name)
    suffixes = "".join(path.suffixes).casefold()
    assert path.suffix.casefold() not in PAYLOAD_SUFFIXES
    assert not suffixes.endswith((".tar.gz", ".tar.bz2", ".tar.xz"))
    assert not leading_bytes.startswith(FORBIDDEN_MAGIC)
    assert size < MAX_MEMBER_SIZE
    if path.suffix.casefold() in {".json", ".yaml", ".yml"} or (
        "fixtures" in path.parts
    ):
        assert size < MAX_METADATA_SIZE
    normalized = path.as_posix()
    for research_state in (RESEARCH_LEDGER, RESEARCH_VOCABULARY):
        assert normalized != research_state
        assert not normalized.endswith(f"/{research_state}")
    assert not (set(part.casefold() for part in path.parts) & FORBIDDEN_STATE_PARTS)
    assert not path.name.casefold().endswith(".lock")


def assert_safe_zip_member(archive: ZipFile, member: ZipInfo) -> None:
    """Validate a wheel member without extracting or fully reading it."""

    normalized_archive_path(member.filename)
    mode = member.external_attr >> 16
    if member.is_dir():
        assert not mode or stat.S_ISDIR(mode)
        return
    assert not mode or stat.S_ISREG(mode)
    with archive.open(member) as source:
        leading_bytes = source.read(MAGIC_READ_SIZE)
    assert_safe_file(member.filename, member.file_size, leading_bytes)


@pytest.mark.parametrize(
    "name",
    [
        "package/.crawl4ai/cache.db",
        "package/browser-profiles/profile.json",
        "package/browser-state/state.json",
        "package/crawl4ai-cache/page.html",
        "package/fetched-pages/page.html",
        "package/generated-markdown/page.md",
        "package/ms-playwright/browser.exe",
        f"source/{RESEARCH_VOCABULARY}",
    ],
)
def test_safe_file_rejects_synthetic_research_state(name: str) -> None:
    """Reject cache, fetched-page, browser-state, and vocabulary fixtures."""

    with pytest.raises(AssertionError):
        assert_safe_file(name, 10, b"synthetic")


def sdist_data_path(name: str) -> str | None:
    """Map an sdist member to its authored package-data path."""

    parts = normalized_archive_path(name).parts
    marker = ("src", "histgerm", "data")
    for index in range(len(parts) - len(marker) + 1):
        if parts[index : index + len(marker)] == marker:
            return PurePosixPath(*parts[index + 1 :]).as_posix()
    return None


@pytest.fixture(scope="session")
def built_distributions(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path]:
    """Build one wheel and sdist for package-boundary tests."""

    output = tmp_path_factory.mktemp("distributions")
    environment = os.environ.copy()
    environment["UV_OFFLINE"] = "1"
    environment["UV_PYTHON_DOWNLOADS"] = "never"
    subprocess.run(
        ["uv", "build", "--no-sources", "--out-dir", str(output)],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(output.glob("*.whl"))
    sdists = list(output.glob("*.tar.gz"))
    assert len(wheels) == 1
    assert len(sdists) == 1
    return wheels[0], sdists[0]


def test_distributions_contain_every_authored_yaml_once(
    built_distributions: tuple[Path, Path],
) -> None:
    """Include exactly the dynamically discovered YAML in both artifacts."""

    built_wheel, built_sdist = built_distributions
    with ZipFile(built_wheel) as archive:
        wheel_names = archive.namelist()
    with tarfile.open(built_sdist, mode="r:gz") as archive:
        sdist_names = [member.name for member in archive if member.isfile()]

    expected = authored_yaml()
    wheel_yaml = [
        name
        for name in wheel_names
        if name.startswith("histgerm/data/") and name.endswith((".yaml", ".yml"))
    ]
    sdist_yaml = [
        path
        for name in sdist_names
        if (path := sdist_data_path(name)) is not None
        and path.endswith((".yaml", ".yml"))
    ]
    assert Counter(wheel_yaml) == Counter({path: 1 for path in expected})
    assert Counter(sdist_yaml) == Counter({path: 1 for path in expected})
    forbidden_names = {"manifest.json", "snapshot.json", "inventory.json"}
    assert not any(PurePosixPath(name).name in forbidden_names for name in wheel_names)
    assert not any(PurePosixPath(name).name in forbidden_names for name in sdist_names)
    assert not any(PurePosixPath(name).name == MIGRATION_STATE for name in wheel_names)
    assert not any(PurePosixPath(name).name == MIGRATION_STATE for name in sdist_names)
    assert not any(name.endswith(RESEARCH_VOCABULARY) for name in wheel_names)
    assert not any(name.endswith(RESEARCH_VOCABULARY) for name in sdist_names)


def test_distributions_have_safe_members(
    built_distributions: tuple[Path, Path],
) -> None:
    """Reject unsafe paths, links, devices, payloads, state, and large members."""

    built_wheel, built_sdist = built_distributions
    with ZipFile(built_wheel) as archive:
        for zip_member in archive.infolist():
            assert_safe_zip_member(archive, zip_member)

    with tarfile.open(built_sdist, mode="r:gz") as archive:
        for tar_member in archive:
            normalized_archive_path(tar_member.name)
            assert tar_member.isdir() or tar_member.isfile()
            if tar_member.isdir():
                continue
            extracted = archive.extractfile(tar_member)
            assert extracted is not None
            leading_bytes = extracted.read(MAGIC_READ_SIZE)
            assert_safe_file(tar_member.name, tar_member.size, leading_bytes)


def test_installed_wheel_imports_loads_yaml_and_queries_each_type(
    built_distributions: tuple[Path, Path], tmp_path: Path
) -> None:
    """Install the wheel away from the checkout and smoke-test its public API."""

    built_wheel, _ = built_distributions
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["UV_OFFLINE"] = "1"
    environment["UV_PYTHON_DOWNLOADS"] = "never"
    environment["UV_CACHE_DIR"] = str(tmp_path / "empty-uv-cache")
    target = tmp_path / "installed"
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--target",
            str(target),
            str(built_wheel),
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    environment["PYTHONPATH"] = str(target)
    environment["PYTHONNOUSERSITE"] = "1"
    for name in tuple(environment):
        if name.startswith("COV_CORE_"):
            environment.pop(name)
    script = "\n".join(
        [
            "import histgerm",
            "from pathlib import Path",
            "from histgerm.catalog import load_catalog",
            "from histgerm.loading import discover_bundled_yaml, load_bundled_yaml",
            f"installed = Path({str(target)!r}).resolve()",
            "assert Path(histgerm.__file__).resolve().is_relative_to(installed)",
            f"expected = {tuple(authored_yaml())!r}".replace("histgerm/data/", ""),
            "assert discover_bundled_yaml() == expected",
            "expected_ids = {'corpora': set(), 'dictionaries': set(), 'tools': set()}",
            "for path in expected:",
            "    category = path.partition('/')[0]",
            "    expected_ids[category].add(load_bundled_yaml(path)['id'])",
            "assert all(expected_ids.values())",
            "catalog = load_catalog()",
            "assert {item.id for item in catalog.find_corpora()} == "
            "expected_ids['corpora']",
            "assert {item.id for item in catalog.find_dictionaries()} == "
            "expected_ids['dictionaries']",
            "assert {item.id for item in catalog.find_tools()} == "
            "expected_ids['tools']",
        ]
    )
    subprocess.run(
        [sys.executable, "-c", script],
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
    files = sorted(path for path in DATA_ROOT.rglob("*") if path.is_file())
    assert [path.relative_to(ROOT / "src").as_posix() for path in files] == (
        authored_yaml()
    )
    assert {path.parent.name for path in files} == set(RESOURCE_CATEGORIES)

    staged = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    assert not [
        entry for entry in staged if entry and entry.split(maxsplit=1)[0] == b"160000"
    ]

    repository_files = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    for encoded in repository_files:
        if not encoded:
            continue
        relative = Path(os.fsdecode(encoded))
        path = ROOT / relative
        assert not path.is_symlink()
        assert not (
            {part.casefold() for part in relative.parts} & FORBIDDEN_STATE_PARTS
        )
        if not path.exists():
            continue
        size = path.stat().st_size
        with path.open("rb") as source:
            leading_bytes = source.read(MAGIC_READ_SIZE)
        # The exact root migration-state.json is the durable, Git-tracked machine
        # state for the curator migration; it is excluded from every distribution
        # (verified above) but is the sole permitted repository JSON payload.
        if relative.as_posix() != MIGRATION_STATE:
            assert relative.suffix.casefold() not in PAYLOAD_SUFFIXES
        assert not leading_bytes.startswith(FORBIDDEN_MAGIC)
        assert size < MAX_MEMBER_SIZE
        if relative.suffix.casefold() in {".json", ".yaml", ".yml"} or (
            "fixtures" in relative.parts
        ):
            assert size < MAX_METADATA_SIZE
