"""Read-only catalog discovery facade."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from pydantic import Field

from histgerm.models.catalog import Catalog
from histgerm.models.common import HistGermModel, KnownValue
from histgerm.models.resource import Resource, ResourceVersion
from histgerm.query.filters import (
    AmbiguousNameError,
    AnnotationMatch,
    DimensionFilter,
    DistributionMatch,
    InvalidIdentifierError,
    InvalidQueryError,
    MatchMode,
    PermissionReview,
    QueryFilter,
    ResourceMatch,
    SortSpec,
    UnknownVocabularyValueError,
)

_STABLE_ID = re.compile(
    r"^(?:res|ver|dist|comp|work|wit|doc|ann|rel|pub|evidence)-"
    r"[a-z0-9]+(?:-[a-z0-9]+)*$"
)
_DIRECT = frozenset({"available"})
_PARTIAL = frozenset({"available", "partially_available"})
_ACTIVE = frozenset(
    {"available", "partially_available", "request_only", "authentication_required"}
)
T = TypeVar("T")
_DEFAULT_FILTER = QueryFilter()
_DEFAULT_SORT = SortSpec()


def normalize_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().split()).casefold()


def _known_set(value: Any) -> frozenset[str]:
    if isinstance(value, KnownValue):
        return frozenset(value.value)
    return frozenset()


def _matches(actual: frozenset[str], requested: DimensionFilter) -> bool:
    if requested.match == "any":
        return bool(actual & requested.values)
    return actual >= requested.values


def _evidence_ids(resource: Resource, prefix: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                evidence_id
                for pointer, evidence_ids in resource.claims.items()
                if pointer == prefix or pointer.startswith(prefix + "/")
                for evidence_id in evidence_ids
            }
        )
    )


class CatalogQuery(HistGermModel):
    catalog: Catalog = Field(repr=False)

    def _validate_values(
        self, dimension: str, values: frozenset[str], registry: str
    ) -> None:
        if not values:
            raise InvalidQueryError(f"{dimension} filter values must not be empty")
        if registry == "licenses":
            definition = self.catalog.registries.root.get(registry)
            known = (
                {term.id for term in definition.terms}
                if definition is not None
                else set()
            )
        else:
            vocabulary = self.catalog.vocabularies.root.get(registry)
            known = set(vocabulary.ids) if vocabulary is not None else set()
        unknown = values - known
        if unknown:
            raise UnknownVocabularyValueError(dimension, frozenset(unknown))

    def _resource_stages(
        self, resource: Resource, version: ResourceVersion | None = None
    ) -> frozenset[str]:
        stages = set(_known_set(resource.language_stage_ids))
        if version is not None:
            stages.update(_known_set(version.language_stage_ids))
        return frozenset(stages)

    def _resource_match(
        self,
        resource: Resource,
        *,
        matched_on: str = "filters",
        matched_name: str | None = None,
    ) -> ResourceMatch:
        stages = set(self._resource_stages(resource))
        for version in resource.versions:
            stages.update(self._resource_stages(resource, version))
        return ResourceMatch(
            resource_id=resource.id,
            canonical_name=resource.canonical_name,
            matched_on=matched_on,  # type: ignore[arg-type]
            matched_name=matched_name,
            category_ids=tuple(sorted(resource.categories)),
            language_stage_ids=tuple(sorted(stages)),
        )

    def _sort(
        self,
        values: Iterable[T],
        sort: SortSpec,
        *,
        identity: Callable[[T], tuple[str, ...]],
        name: Callable[[T], str],
    ) -> tuple[T, ...]:
        ordered = sorted(values, key=identity)
        if sort.field == "canonical_name":
            ordered.sort(
                key=lambda item: normalize_name(name(item)),
                reverse=sort.direction == "descending",
            )
        elif sort.direction == "descending":
            ordered.reverse()
        return tuple(ordered)

    def _find_resources(
        self, value: str
    ) -> tuple[tuple[Resource, str, str | None], ...]:
        if _STABLE_ID.fullmatch(value):
            resource = next(
                (item for item in self.catalog.resources if item.id == value), None
            )
            return () if resource is None else ((resource, "identifier", None),)
        if value.startswith("res-"):
            raise InvalidIdentifierError(value)
        normalized = normalize_name(value)
        if not normalized:
            raise InvalidQueryError(
                "identifier_or_name must not be empty after normalization"
            )
        matches: dict[str, tuple[Resource, str, str]] = {}
        for resource in self.catalog.resources:
            if normalize_name(resource.canonical_name) == normalized:
                matches[resource.id] = (
                    resource,
                    "canonical_name",
                    resource.canonical_name,
                )
                continue
            aliases = sorted(
                (
                    alias.text,
                    getattr(
                        getattr(alias.language, "value", None), "__str__", lambda: ""
                    )(),
                    alias.name_type,
                )
                for alias in resource.alternative_names
                if normalize_name(alias.text) == normalized
            )
            if aliases:
                matches[resource.id] = (resource, "alternative_name", aliases[0][0])
        if len(matches) > 1:
            raise AmbiguousNameError(normalized, tuple(sorted(matches)))
        return tuple(matches.values())

    def find(
        self, identifier_or_name: str, *, sort: SortSpec = _DEFAULT_SORT
    ) -> tuple[ResourceMatch, ...]:
        matches = [
            self._resource_match(item, matched_on=matched, matched_name=name)
            for item, matched, name in self._find_resources(identifier_or_name)
        ]
        return self._sort(
            matches,
            sort,
            identity=lambda item: (item.resource_id,),
            name=lambda item: item.canonical_name,
        )

    def resources(
        self,
        filters: QueryFilter = _DEFAULT_FILTER,
        *,
        sort: SortSpec = _DEFAULT_SORT,
    ) -> tuple[ResourceMatch, ...]:
        dimensions = (
            ("category", filters.categories, "resource_category"),
            ("language_stage", filters.language_stages, "language_stage"),
            ("annotation_type", filters.annotation_types, "annotation_task"),
            ("format", filters.formats, "data_format"),
            ("license", filters.licenses, "licenses"),
            ("availability", filters.availability, "availability"),
            ("annotation_quality", filters.annotation_qualities, "annotation_quality"),
        )
        for dimension, requested, registry in dimensions:
            if requested is not None:
                self._validate_values(dimension, requested.values, registry)

        named = (
            {
                item.id: (matched, name)
                for item, matched, name in self._find_resources(
                    filters.identifier_or_name
                )
            }
            if filters.identifier_or_name is not None
            else None
        )
        matches: list[ResourceMatch] = []
        for resource in self.catalog.resources:
            if named is not None and resource.id not in named:
                continue
            if filters.categories and not _matches(
                resource.categories, filters.categories
            ):
                continue
            versions: tuple[ResourceVersion | None, ...] = tuple(resource.versions) or (
                None,
            )
            path_matches = False
            for version in versions:
                if filters.language_stages and not _matches(
                    self._resource_stages(resource, version), filters.language_stages
                ):
                    continue
                annotations = () if version is None else version.annotations
                if (
                    filters.annotation_types or filters.annotation_qualities
                ) and not any(
                    (
                        not filters.annotation_types
                        or _matches(frozenset({ann.task}), filters.annotation_types)
                    )
                    and (
                        not filters.annotation_qualities
                        or _matches(
                            frozenset({ann.quality}), filters.annotation_qualities
                        )
                    )
                    for ann in annotations
                ):
                    continue
                distributions = () if version is None else version.distributions
                if (
                    filters.formats or filters.licenses or filters.availability
                ) and not any(
                    self._distribution_satisfies(dist, filters)
                    for dist in distributions
                ):
                    continue
                path_matches = True
                break
            if path_matches:
                matched, name = (
                    named[resource.id] if named is not None else ("filters", None)
                )
                matches.append(
                    self._resource_match(
                        resource, matched_on=matched, matched_name=name
                    )
                )
        return self._sort(
            matches,
            sort,
            identity=lambda item: (item.resource_id,),
            name=lambda item: item.canonical_name,
        )

    def _distribution_satisfies(self, distribution: Any, filters: QueryFilter) -> bool:
        formats: frozenset[str] = frozenset()
        if isinstance(distribution.formats, KnownValue):
            formats = frozenset(
                value
                for item in distribution.formats.value
                for value in ({item.format_id} | set(_known_set(item.inner_format_ids)))
            )
        licenses = (
            frozenset({distribution.license.license_id.value})
            if isinstance(distribution.license.license_id, KnownValue)
            else frozenset()
        )
        return (
            (not filters.formats or _matches(formats, filters.formats))
            and (not filters.licenses or _matches(licenses, filters.licenses))
            and (
                not filters.availability
                or _matches(
                    frozenset({distribution.availability}), filters.availability
                )
            )
        )

    def by_category(
        self,
        category_ids: frozenset[str],
        *,
        match: MatchMode = "any",
        sort: SortSpec = _DEFAULT_SORT,
    ) -> tuple[ResourceMatch, ...]:
        return self.resources(
            QueryFilter(categories=DimensionFilter(values=category_ids, match=match)),
            sort=sort,
        )

    def by_language_stage(
        self,
        stage_ids: frozenset[str],
        *,
        match: MatchMode = "any",
        sort: SortSpec = _DEFAULT_SORT,
    ) -> tuple[ResourceMatch, ...]:
        return self.resources(
            QueryFilter(language_stages=DimensionFilter(values=stage_ids, match=match)),
            sort=sort,
        )

    def _annotation_matches(
        self,
        task_ids: frozenset[str] | None,
        quality_ids: frozenset[str] | None,
        *,
        task_match: MatchMode,
        quality_match: MatchMode,
        language_stages: frozenset[str] | None,
        sort: SortSpec,
    ) -> tuple[AnnotationMatch, ...]:
        if task_ids is not None:
            self._validate_values("annotation_type", task_ids, "annotation_task")
        if quality_ids is not None:
            self._validate_values(
                "annotation_quality", quality_ids, "annotation_quality"
            )
        if language_stages is not None:
            self._validate_values("language_stage", language_stages, "language_stage")
        results: list[AnnotationMatch] = []
        for resource in self.catalog.resources:
            for version_index, version in enumerate(resource.versions):
                if (
                    language_stages
                    and not self._resource_stages(resource, version) & language_stages
                ):
                    continue
                for annotation_index, annotation in enumerate(version.annotations):
                    if task_ids and not _matches(
                        frozenset({annotation.task}),
                        DimensionFilter(values=task_ids, match=task_match),
                    ):
                        continue
                    if quality_ids and not _matches(
                        frozenset({annotation.quality}),
                        DimensionFilter(values=quality_ids, match=quality_match),
                    ):
                        continue
                    results.append(
                        AnnotationMatch(
                            resource_id=resource.id,
                            version_id=version.id,
                            annotation_id=annotation.id,
                            task_id=annotation.task,
                            quality_id=annotation.quality,
                            scope=annotation.scope,
                            evidence_ids=_evidence_ids(
                                resource,
                                f"/versions/{version_index}/annotations/{annotation_index}",
                            ),
                        )
                    )
        return self._sort(
            results,
            sort,
            identity=lambda item: (
                item.resource_id,
                item.version_id,
                item.annotation_id,
            ),
            name=lambda item: next(
                resource.canonical_name
                for resource in self.catalog.resources
                if resource.id == item.resource_id
            ),
        )

    def by_annotation(
        self,
        task_ids: frozenset[str],
        *,
        qualities: frozenset[str] | None = None,
        task_match: MatchMode = "any",
        quality_match: MatchMode = "any",
        sort: SortSpec = _DEFAULT_SORT,
    ) -> tuple[AnnotationMatch, ...]:
        return self._annotation_matches(
            task_ids,
            qualities,
            task_match=task_match,
            quality_match=quality_match,
            language_stages=None,
            sort=sort,
        )

    def by_annotation_quality(
        self,
        quality_ids: frozenset[str],
        *,
        match: MatchMode = "any",
        sort: SortSpec = _DEFAULT_SORT,
    ) -> tuple[AnnotationMatch, ...]:
        return self._annotation_matches(
            None,
            quality_ids,
            task_match="any",
            quality_match=match,
            language_stages=None,
            sort=sort,
        )

    def with_annotation(
        self,
        task: str,
        *,
        quality: frozenset[str] | None = None,
        language_stages: frozenset[str] | None = None,
        sort: SortSpec = _DEFAULT_SORT,
    ) -> tuple[AnnotationMatch, ...]:
        return self._annotation_matches(
            frozenset({task}),
            quality,
            task_match="any",
            quality_match="any",
            language_stages=language_stages,
            sort=sort,
        )

    def _distribution_matches(
        self, filters: QueryFilter, sort: SortSpec
    ) -> tuple[DistributionMatch, ...]:
        self.resources(filters)
        results: list[DistributionMatch] = []
        for resource in self.catalog.resources:
            if filters.categories and not _matches(
                resource.categories, filters.categories
            ):
                continue
            for version_index, version in enumerate(resource.versions):
                if filters.language_stages and not _matches(
                    self._resource_stages(resource, version), filters.language_stages
                ):
                    continue
                for distribution_index, distribution in enumerate(
                    version.distributions
                ):
                    if self._distribution_satisfies(distribution, filters):
                        results.append(
                            self._distribution_match(
                                resource,
                                version,
                                version_index,
                                distribution,
                                distribution_index,
                            )
                        )
        return self._sort(
            results,
            sort,
            identity=lambda item: (
                item.resource_id,
                item.version_id,
                item.distribution_id,
            ),
            name=lambda item: next(
                resource.canonical_name
                for resource in self.catalog.resources
                if resource.id == item.resource_id
            ),
        )

    def _distribution_match(
        self,
        resource: Resource,
        version: ResourceVersion,
        version_index: int,
        distribution: Any,
        distribution_index: int,
    ) -> DistributionMatch:
        urls = (
            tuple(sorted({str(item.url) for item in distribution.access_urls.value}))
            if isinstance(distribution.access_urls, KnownValue)
            else ()
        )
        warnings = []
        requirement = distribution.access.authentication_or_agreement
        if requirement not in {"none", "not_applicable"}:
            warnings.append(f"access_requirement={requirement}")
        return DistributionMatch(
            resource_id=resource.id,
            version_id=version.id,
            distribution_id=distribution.id,
            component_ids=tuple(sorted(distribution.scope.component_ids)),
            availability=distribution.availability,
            model_training=distribution.access.model_training,
            download_status=distribution.access.download,
            external_urls=urls,
            evidence_ids=_evidence_ids(
                resource,
                f"/versions/{version_index}/distributions/{distribution_index}",
            ),
            warnings=tuple(sorted(warnings)),
        )

    def by_format(
        self,
        format_ids: frozenset[str],
        *,
        match: MatchMode = "any",
        sort: SortSpec = _DEFAULT_SORT,
    ) -> tuple[DistributionMatch, ...]:
        return self._distribution_matches(
            QueryFilter(formats=DimensionFilter(values=format_ids, match=match)), sort
        )

    def by_license(
        self,
        license_ids: frozenset[str],
        *,
        match: MatchMode = "any",
        sort: SortSpec = _DEFAULT_SORT,
    ) -> tuple[DistributionMatch, ...]:
        return self._distribution_matches(
            QueryFilter(licenses=DimensionFilter(values=license_ids, match=match)), sort
        )

    def by_availability(
        self,
        states: frozenset[str],
        *,
        match: MatchMode = "any",
        sort: SortSpec = _DEFAULT_SORT,
    ) -> tuple[DistributionMatch, ...]:
        return self._distribution_matches(
            QueryFilter(availability=DimensionFilter(values=states, match=match)), sort
        )

    @staticmethod
    def _has_url(distribution: Any, kind: str) -> bool:
        return isinstance(distribution.access_urls, KnownValue) and any(
            item.kind == kind for item in distribution.access_urls.value
        )

    def _downloadable(self, distribution: Any, include_partial: bool) -> bool:
        allowed = _PARTIAL if include_partial else _DIRECT
        return (
            distribution.availability in allowed
            and distribution.access.download in allowed
            and self._has_url(distribution, "download")
            and distribution.access.authentication_or_agreement
            in {"none", "click_through_agreement"}
        )

    def downloadable_corpora(
        self,
        language_stage: str,
        *,
        include_partially_available: bool = False,
        sort: SortSpec = _DEFAULT_SORT,
    ) -> tuple[DistributionMatch, ...]:
        self._validate_values(
            "language_stage", frozenset({language_stage}), "language_stage"
        )
        results = []
        for resource in self.catalog.resources:
            if "corpus" not in resource.categories:
                continue
            for vi, version in enumerate(resource.versions):
                if language_stage not in self._resource_stages(resource, version):
                    continue
                for di, distribution in enumerate(version.distributions):
                    if self._downloadable(distribution, include_partially_available):
                        results.append(
                            self._distribution_match(
                                resource, version, vi, distribution, di
                            )
                        )
        return self._sort(
            results,
            sort,
            identity=lambda item: (
                item.resource_id,
                item.version_id,
                item.distribution_id,
            ),
            name=lambda item: next(
                r.canonical_name
                for r in self.catalog.resources
                if r.id == item.resource_id
            ),
        )

    def _usable_route(self, distribution: Any) -> bool:
        if self._downloadable(distribution, True):
            return True
        if distribution.availability not in _ACTIVE:
            return False
        if distribution.access.api_access in _ACTIVE and self._has_url(
            distribution, "api"
        ):
            return bool(distribution.access.automated_access == "permitted")
        return bool(
            distribution.access.request_only in _ACTIVE
            or distribution.access.download == "authentication_required"
        )

    def training_compatible_corpora(
        self,
        language_stage: str | None = None,
        *,
        require_downloadable: bool = True,
        sort: SortSpec = _DEFAULT_SORT,
    ) -> tuple[DistributionMatch, ...]:
        if language_stage is not None:
            self._validate_values(
                "language_stage", frozenset({language_stage}), "language_stage"
            )
        results = []
        for resource in self.catalog.resources:
            if "corpus" not in resource.categories:
                continue
            for vi, version in enumerate(resource.versions):
                if language_stage and language_stage not in self._resource_stages(
                    resource, version
                ):
                    continue
                for di, distribution in enumerate(version.distributions):
                    if distribution.access.model_training != "permitted":
                        continue
                    usable = (
                        self._downloadable(distribution, False)
                        if require_downloadable
                        else self._usable_route(distribution)
                    )
                    if usable:
                        results.append(
                            self._distribution_match(
                                resource, version, vi, distribution, di
                            )
                        )
        return self._sort(
            results,
            sort,
            identity=lambda item: (
                item.resource_id,
                item.version_id,
                item.distribution_id,
            ),
            name=lambda item: next(
                r.canonical_name
                for r in self.catalog.resources
                if r.id == item.resource_id
            ),
        )

    def training_permission_review(
        self,
        *,
        language_stage: str | None = None,
        states: frozenset[str] = frozenset({"unclear"}),
    ) -> tuple[PermissionReview, ...]:
        if not states:
            raise InvalidQueryError("permission_state filter values must not be empty")
        if language_stage is not None:
            self._validate_values(
                "language_stage", frozenset({language_stage}), "language_stage"
            )
        results = []
        for resource in self.catalog.resources:
            if "corpus" not in resource.categories:
                continue
            for vi, version in enumerate(resource.versions):
                if language_stage and language_stage not in self._resource_stages(
                    resource, version
                ):
                    continue
                for di, distribution in enumerate(version.distributions):
                    permission = distribution.access.model_training
                    if permission in states:
                        results.append(
                            PermissionReview(
                                resource_id=resource.id,
                                version_id=version.id,
                                distribution_id=distribution.id,
                                permission=permission,
                                availability=distribution.availability,
                                reasons=(f"model_training={permission}",),
                                evidence_ids=_evidence_ids(
                                    resource,
                                    f"/versions/{vi}/distributions/{di}/access/model_training",
                                ),
                            )
                        )
        return tuple(
            sorted(
                results,
                key=lambda item: (
                    item.resource_id,
                    item.version_id,
                    item.distribution_id,
                ),
            )
        )


__all__ = ["CatalogQuery", "normalize_name"]
