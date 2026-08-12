"""Minimal validation for authored HistGerm V2 YAML inventories."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from histgerm.loading import (
    HistGermLoadingError,
    discover_inventory_files,
    load_yaml_mapping_bytes,
)
from histgerm.models import Corpus, Dictionary, Tool
from histgerm.research.vocabulary_store import (
    VocabularyValidationError,
    validate_vocabulary,
)

type Resource = Corpus | Tool | Dictionary
_RESOURCE_CATEGORIES = frozenset({"corpora", "dictionaries", "tools"})


@dataclass(frozen=True, slots=True)
class ValidationDiagnostic:
    """One explicit inventory validation failure."""

    path: str
    location: str
    message: str

    def render(self) -> str:
        """Render the diagnostic for command-line output."""

        prefix = f"{self.path}:{self.location}" if self.location else self.path
        return f"{prefix}: {self.message}"


class InventoryValidationError(ValueError):
    """Raised when authored YAML does not form a valid V2 inventory."""

    def __init__(self, diagnostics: list[ValidationDiagnostic]) -> None:
        """Store deterministic diagnostics and build a non-success message."""

        self.diagnostics = tuple(diagnostics)
        super().__init__(
            "inventory validation failed:\n"
            + "\n".join(diagnostic.render() for diagnostic in self.diagnostics)
        )


@dataclass(frozen=True, slots=True)
class ValidatedInventory:
    """A validated collection of distinct top-level V2 resources."""

    resources: tuple[Resource, ...]
    source_paths: dict[str, str]

    @property
    def corpora(self) -> tuple[Corpus, ...]:
        """Return validated corpus resources."""

        return tuple(
            resource for resource in self.resources if isinstance(resource, Corpus)
        )

    @property
    def dictionaries(self) -> tuple[Dictionary, ...]:
        """Return validated dictionary resources."""

        return tuple(
            resource for resource in self.resources if isinstance(resource, Dictionary)
        )

    @property
    def tools(self) -> tuple[Tool, ...]:
        """Return validated tool resources."""

        return tuple(
            resource for resource in self.resources if isinstance(resource, Tool)
        )


def _boundary(root: Path) -> Path:
    """Return the directory used to calculate stable diagnostic paths."""

    return root.resolve() if root.is_dir() else root.parent.resolve()


def _relative_path(path: Path, boundary: Path) -> str:
    """Return a normalized path relative to the inventory boundary."""

    return path.resolve().relative_to(boundary).as_posix()


def _resource_category(path: Path, relative: str, root: Path) -> str:
    """Select a top-level model from the explicit inventory directory."""

    parts = Path(relative).parts
    category = path.parent.name if root.is_file() else (parts[0] if parts else "")
    if category not in _RESOURCE_CATEGORIES:
        expected = ", ".join(sorted(_RESOURCE_CATEGORIES))
        raise InventoryValidationError(
            [
                ValidationDiagnostic(
                    relative,
                    "",
                    f"YAML must be beneath one of these resource directories: "
                    f"{expected}",
                )
            ]
        )
    return category


def _model_diagnostics(
    relative: str, error: ValidationError
) -> list[ValidationDiagnostic]:
    """Convert all Pydantic failures into stable file diagnostics."""

    diagnostics: list[ValidationDiagnostic] = []
    for detail in error.errors(include_url=False):
        location = ".".join(str(part) for part in detail["loc"])
        diagnostics.append(ValidationDiagnostic(relative, location, str(detail["msg"])))
    return diagnostics


def _loading_diagnostic(error: HistGermLoadingError) -> ValidationDiagnostic:
    """Convert a restricted-YAML failure into an inventory diagnostic."""

    detail = error.diagnostic
    return ValidationDiagnostic(detail.path, detail.location, detail.message)


def _validate_payload(category: str, payload: dict[str, Any]) -> Resource:
    """Validate a mapping with the model selected by its explicit directory."""

    if category == "corpora":
        return Corpus.model_validate(payload)
    if category == "dictionaries":
        return Dictionary.model_validate(payload)
    return Tool.model_validate(payload)


def _load_resource(
    path: Path, relative: str, root: Path
) -> tuple[Resource | None, list[ValidationDiagnostic]]:
    """Load and validate one authored resource without compatibility dispatch."""

    try:
        category = _resource_category(path, relative, root)
        payload: dict[str, Any] = load_yaml_mapping_bytes(
            path.read_bytes(), source_path=relative
        )
        return _validate_payload(category, payload), []
    except InventoryValidationError as error:
        return None, list(error.diagnostics)
    except HistGermLoadingError as error:
        return None, [_loading_diagnostic(error)]
    except ValidationError as error:
        return None, _model_diagnostics(relative, error)


def _duplicate_diagnostics(
    resources: list[tuple[Resource, str]],
) -> list[ValidationDiagnostic]:
    """Reject duplicate resource and fully qualified text identifiers."""

    diagnostics: list[ValidationDiagnostic] = []
    resource_paths: dict[str, str] = {}
    qualified_text_paths: dict[str, str] = {}
    for resource, path in resources:
        previous = resource_paths.get(resource.id)
        if previous is not None:
            diagnostics.append(
                ValidationDiagnostic(
                    path,
                    "id",
                    f"duplicate resource ID {resource.id!r}; first declared in "
                    f"{previous}",
                )
            )
        else:
            resource_paths[resource.id] = path
        if not isinstance(resource, Corpus):
            continue
        for version in resource.versions:
            for text in version.texts:
                qualified = f"{resource.id}:{text.id}"
                previous_text = qualified_text_paths.get(qualified)
                if previous_text is not None:
                    diagnostics.append(
                        ValidationDiagnostic(
                            path,
                            f"versions.{version.id}.texts.{text.id}.id",
                            f"duplicate qualified text ID {qualified!r}; first "
                            f"declared in {previous_text}",
                        )
                    )
                else:
                    qualified_text_paths[qualified] = path
    return diagnostics


def _reference_diagnostics(
    resources: list[tuple[Resource, str]],
) -> list[ValidationDiagnostic]:
    """Resolve inventory-local corpus and overlap references."""

    diagnostics: list[ValidationDiagnostic] = []
    corpus_ids = {
        resource.id for resource, _ in resources if isinstance(resource, Corpus)
    }
    qualified_text_ids = {
        f"{resource.id}:{text.id}"
        for resource, _ in resources
        if isinstance(resource, Corpus)
        for version in resource.versions
        for text in version.texts
    }
    for resource, path in resources:
        if isinstance(resource, Corpus):
            try:
                resource.validate_inventory_references(corpus_ids, qualified_text_ids)
            except ValueError as error:
                diagnostics.append(ValidationDiagnostic(path, "overlaps", str(error)))
        elif isinstance(resource, Dictionary):
            for corpus_id in resource.corpus_links or []:
                if corpus_id not in corpus_ids:
                    diagnostics.append(
                        ValidationDiagnostic(
                            path,
                            "corpus_links",
                            f"unknown corpus reference {corpus_id!r}",
                        )
                    )
    return diagnostics


def _repository_vocabulary_path(inventory_root: Path) -> Path | None:
    """Locate research vocabulary only for the repository data directory."""

    if not inventory_root.is_dir():
        return None
    resolved = inventory_root.resolve()
    if tuple(part.casefold() for part in resolved.parts[-3:]) != (
        "src",
        "histgerm",
        "data",
    ):
        return None
    return resolved.parents[2] / "research" / "discovery-vocabulary.yaml"


def _validate_repository_vocabulary(inventory_root: Path) -> None:
    """Validate repository research state without changing inventory models."""

    vocabulary_path = _repository_vocabulary_path(inventory_root)
    if vocabulary_path is None:
        return
    relative = "research/discovery-vocabulary.yaml"
    try:
        validate_vocabulary(vocabulary_path)
    except (OSError, ValidationError, VocabularyValidationError) as error:
        raise InventoryValidationError(
            [ValidationDiagnostic(relative, "", str(error))]
        ) from error


def validate_inventory(root: Path | str) -> ValidatedInventory:
    """Load restricted YAML and validate the complete V2 inventory."""

    inventory_root = Path(root)
    try:
        files = discover_inventory_files(inventory_root)
    except HistGermLoadingError as error:
        raise InventoryValidationError([_loading_diagnostic(error)]) from error
    if not files:
        raise InventoryValidationError(
            [
                ValidationDiagnostic(
                    inventory_root.as_posix(),
                    "",
                    "inventory contains no YAML resource files",
                )
            ]
        )

    boundary = _boundary(inventory_root)
    loaded: list[tuple[Resource, str]] = []
    diagnostics: list[ValidationDiagnostic] = []
    for path in files:
        relative = _relative_path(path, boundary)
        resource, failures = _load_resource(path, relative, inventory_root)
        diagnostics.extend(failures)
        if resource is not None:
            loaded.append((resource, relative))
    if diagnostics:
        raise InventoryValidationError(diagnostics)

    diagnostics.extend(_duplicate_diagnostics(loaded))
    if not diagnostics:
        diagnostics.extend(_reference_diagnostics(loaded))
    if diagnostics:
        raise InventoryValidationError(diagnostics)

    _validate_repository_vocabulary(inventory_root)
    return ValidatedInventory(
        resources=tuple(resource for resource, _ in loaded),
        source_paths={resource.id: path for resource, path in loaded},
    )


def _parser() -> argparse.ArgumentParser:
    """Build the concise inventory validation command parser."""

    parser = argparse.ArgumentParser(
        prog="python -m histgerm.validation",
        description="Validate authored HistGerm V2 YAML inventory data.",
    )
    parser.add_argument("inventory", type=Path, help="YAML file or data directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run inventory validation and return a conventional process exit code."""

    arguments = _parser().parse_args(argv)
    try:
        inventory = validate_inventory(arguments.inventory)
    except InventoryValidationError as error:
        print("HistGerm inventory validation failed:", file=sys.stderr)
        for diagnostic in error.diagnostics:
            print(f"- {diagnostic.render()}", file=sys.stderr)
        return 1
    print(
        f"Validated {len(inventory.resources)} HistGerm resource(s): "
        f"{len(inventory.corpora)} corpora, {len(inventory.tools)} tools, "
        f"{len(inventory.dictionaries)} dictionaries."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "InventoryValidationError",
    "ValidatedInventory",
    "ValidationDiagnostic",
    "main",
    "validate_inventory",
]
