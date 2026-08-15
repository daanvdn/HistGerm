"""Offline synthetic transport shared by in-process and subprocess CLI tests."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from histgerm.research.fetching import FetchedMetadata
from histgerm.research.search_providers import ResultClassification, SearchResult

LEAD_URL = "https://example.org/mhgbert"
LEAD_TITLE = "MhgBERT Middle High German language model"
FEED_URL = "https://example.org/mhgtagger"
FEED_TITLE = "MhgTagger Middle High German tagger"
PROVIDER_HOSTS = frozenset(
    {
        "www.google.com",
        "www.bing.com",
        "search.brave.com",
        "vlo.clarin.eu",
        "www.language-archives.org",
        "zenodo.org",
        "github.com",
        "gitlab.com",
        "huggingface.co",
    }
)


def synthetic_fetch(url: str, /, *, max_bytes: int) -> FetchedMetadata:
    """Return one bounded synthetic response without any network access."""

    if urlsplit(url).netloc in PROVIDER_HOSTS:
        if "format=rss" in url:
            body = (
                f"<rss><channel><item><title>{FEED_TITLE}</title>"
                f"<link>{FEED_URL}</link></item></channel></rss>"
            )
            return FetchedMetadata(url, "application/xml", body.encode("utf-8"))
        return FetchedMetadata(
            url, "text/html", f'<a href="{LEAD_URL}">{LEAD_TITLE}</a>'.encode()
        )
    return FetchedMetadata(
        url,
        "text/html",
        b"<h1>Middle High German tagger documentation</h1>",
    )


def model_answer(prompt: str, /) -> str:
    """Return the bounded name-only elicitation JSON for one exact prompt."""

    if "additional plausible" in prompt:
        return json.dumps({"candidates": []})
    return json.dumps({"candidates": [{"name": "MhgBERT", "aliases": ["MHG BERT"]}]})


def inspect_result(result: SearchResult) -> tuple[ResultClassification, str]:
    """Classify one normalized item exactly as the hosting agent would."""

    if result.url in {LEAD_URL, FEED_URL}:
        return "lead", "synthetic Middle High German model lead"
    return "unrelated", "offline synthetic fixture"
