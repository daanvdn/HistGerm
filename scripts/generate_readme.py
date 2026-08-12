"""Generate the README catalog tables from the bundled inventory."""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from histgerm.catalog import Catalog, load_catalog
from histgerm.models import Corpus, Dictionary, Tool

START_MARKER = "<!-- histgerm-catalog:start -->"
END_MARKER = "<!-- histgerm-catalog:end -->"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_README = ROOT / "README.md"
DEFAULT_TEMPLATE = ROOT / "templates" / "readme-catalog.md.j2"


@dataclass(frozen=True, slots=True)
class ResourceRow:
    """One deterministic Markdown table row."""

    id: str
    name: str
    url: str | None
    stages: tuple[str, ...]
    availability: tuple[str, ...]
    reviewed_on: str
    description: str
    details: tuple[str, ...] = ()


def _values(items: Iterable[StrEnum] | None) -> tuple[str, ...]:
    return tuple(item.value for item in items or ())


def _resource_url(resource: Corpus | Dictionary | Tool) -> str | None:
    if not resource.links:
        return None
    link = resource.links.get("homepage")
    if link is None:
        link = resource.links[min(resource.links)]
    return str(link)


def _common_row(
    resource: Corpus | Dictionary | Tool,
    *,
    stages: Iterable[StrEnum] | None,
    details: Iterable[str] = (),
) -> ResourceRow:
    return ResourceRow(
        id=resource.id,
        name=resource.name,
        url=_resource_url(resource),
        stages=_values(stages),
        availability=_values(resource.access.availability),
        reviewed_on=resource.reviewed_on.isoformat(),
        description=resource.description or "",
        details=tuple(details),
    )


def catalog_context(catalog: Catalog) -> dict[str, tuple[ResourceRow, ...]]:
    """Convert validated resources into immutable template rows."""

    corpora = tuple(
        _common_row(corpus, stages=corpus.covered_stages)
        for corpus in catalog.find_corpora()
    )
    dictionaries = tuple(
        _common_row(
            dictionary,
            stages=dictionary.covered_stages,
            details=(
                *(dictionary.lexical_features or ()),
                f"machine readable: {str(dictionary.machine_readable).lower()}"
                if dictionary.machine_readable is not None
                else "machine readable: unknown",
            ),
        )
        for dictionary in catalog.find_dictionaries()
    )
    tools = tuple(
        _common_row(
            tool,
            stages=tool.supported_stages,
            details=(task.value for task in tool.tasks),
        )
        for tool in catalog.find_tools()
    )
    return {
        "corpora": corpora,
        "dictionaries": dictionaries,
        "tools": tools,
    }


def _markdown_cell(value: object) -> str:
    text = " ".join(str(value).split())
    return (
        text.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def render_catalog(template_path: Path = DEFAULT_TEMPLATE) -> str:
    """Render the generated README suffix with stable whitespace."""

    environment = Environment(
        loader=FileSystemLoader(template_path.parent),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )
    environment.filters["markdown_cell"] = _markdown_cell
    template = environment.get_template(template_path.name)
    return template.render(**catalog_context(load_catalog()))


def updated_readme(source: str, generated: str) -> str:
    """Append or replace the generated suffix without changing the authored prefix."""

    marker_count = source.count(START_MARKER)
    if marker_count > 1:
        raise ValueError(f"README contains {marker_count} catalog start markers")
    if marker_count == 1:
        prefix, suffix = source.split(START_MARKER, maxsplit=1)
        if END_MARKER not in suffix:
            raise ValueError("README catalog start marker has no matching end marker")
        _, trailing = suffix.split(END_MARKER, maxsplit=1)
        if trailing.strip():
            raise ValueError("README catalog must remain the final section")
        return prefix + generated

    separator = (
        "" if source.endswith("\n\n") else "\n" if source.endswith("\n") else "\n\n"
    )
    return source + separator + generated


def generate(readme_path: Path = DEFAULT_README, *, check: bool = False) -> bool:
    """Update the README and return whether it was already current."""

    source = readme_path.read_bytes().decode("utf-8")
    expected = updated_readme(source, render_catalog())
    current = source == expected
    if not current and not check:
        readme_path.write_bytes(expected.encode("utf-8"))
    return current


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report stale generated content without changing README.md",
    )
    parser.add_argument(
        "--readme",
        type=Path,
        default=DEFAULT_README,
        help="README path to update (defaults to the repository README.md)",
    )
    args = parser.parse_args()
    current = generate(args.readme, check=args.check)
    if args.check and not current:
        parser.error("README.md catalog is stale; run scripts/generate_readme.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
