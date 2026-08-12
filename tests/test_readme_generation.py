"""Tests for deterministic README catalog generation."""

from __future__ import annotations

import sys
from pathlib import Path
from subprocess import run

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "generate_readme.py"
README = ROOT / "README.md"
START_MARKER = "<!-- histgerm-catalog:start -->"
END_MARKER = "<!-- histgerm-catalog:end -->"


def _generate(*arguments: str) -> None:
    run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=ROOT,
        check=True,
    )


def test_checked_in_readme_catalog_is_current() -> None:
    """Keep the committed README synchronized with the inventory."""

    _generate("--check")


def test_generation_preserves_authored_prefix_and_is_idempotent(
    tmp_path: Path,
) -> None:
    """Only replace the marker-delimited generated suffix."""

    readme = tmp_path / "README.md"
    prefix = b"# Authored\r\n\r\nKeep this exactly.\r\n"
    readme.write_bytes(prefix)

    _generate("--readme", str(readme))
    first = readme.read_bytes()
    _generate("--readme", str(readme))
    second = readme.read_bytes()

    assert first.startswith(prefix)
    assert first == second
    assert first.count(START_MARKER.encode()) == 1
    assert first.endswith(f"{END_MARKER}\n".encode())


def test_generated_catalog_contains_every_resource_id() -> None:
    """Represent every captured corpus, dictionary, and tool."""

    generated = README.read_text(encoding="utf-8")
    data_root = ROOT / "src" / "histgerm" / "data"

    for path in data_root.glob("*/*.yaml"):
        resource_id = next(
            line.removeprefix("id:").strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("id:")
        )
        assert f"`{resource_id}`" in generated
