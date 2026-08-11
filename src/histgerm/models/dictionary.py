"""Dictionary model for the HistGerm V2 schema."""

from __future__ import annotations

from pydantic import HttpUrl, field_validator, model_validator

from .common import STABLE_ID_RE, Access, BaseResource, LanguageStage

__all__ = ["Dictionary"]


class Dictionary(BaseResource):
    """A historical dictionary with lexical and access metadata."""

    covered_stages: list[LanguageStage] | None = None
    covered_languages: list[str] | None = None
    lexical_features: list[str] | None = None
    search_links: list[HttpUrl] | None = None
    api_links: list[HttpUrl] | None = None
    download_links: list[HttpUrl] | None = None
    machine_readable: bool | None = None
    access: Access
    citation_detail: str | None = None
    corpus_links: list[str] | None = None
    note: str | None = None

    @field_validator("corpus_links")
    @classmethod
    def validate_corpus_links(cls, values: list[str] | None) -> list[str] | None:
        """Require corpus links to use stable corpus identifiers."""

        for value in values or []:
            if not STABLE_ID_RE.fullmatch(value):
                raise ValueError("corpus_links entries must be stable corpus IDs")
        return values

    @model_validator(mode="after")
    def validate_dictionary_sources(self) -> Dictionary:
        """Validate the dictionary's access evidence against local sources."""

        self._validate_access_and_references(self.access)
        return self
