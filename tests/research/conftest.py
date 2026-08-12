from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from histgerm.research import initialize_ledger

STAGE = {
    "ohg": {"de": "Althochdeutsch OHG", "en": "Old High German OHG"},
    "mhg": {"de": "Mittelhochdeutsch MHG", "en": "Middle High German MHG"},
    "enhg": {
        "de": "Frühneuhochdeutsch ENHG",
        "en": "Early New High German ENHG",
    },
}
CATEGORY = {
    "corpus": {
        "de": "Korpus Textkorpus Textsammlung Sprachdaten",
        "en": "corpus text collection dataset language data",
    },
    "tool": {
        "de": "Tagger Lemmatisierer Parser Sprachmodell",
        "en": "tagger lemmatizer parser language model",
    },
    "dictionary": {
        "de": "Wörterbuch Lexikon Wortschatz",
        "en": "dictionary lexicon vocabulary",
    },
}
CHANNELS = (
    ("web_de", "de"),
    ("web_en", "en"),
    ("clarin", "de"),
    ("olac", "en"),
    ("zenodo", "en"),
    ("institutional", "de"),
    ("github", "en"),
    ("huggingface", "en"),
)


def pass_data(
    *,
    category: str = "corpus",
    stage: str = "mhg",
    suffix: str = "one",
    complete: bool = True,
    candidate_ids: list[str] | None = None,
    new_candidate_ids: list[str] | None = None,
) -> dict[str, Any]:
    queries = [
        {
            "query": f"{STAGE[stage][language]} {CATEGORY[category][language]}",
            "language": language,
            "channel": channel,
            "source_urls": [f"https://example.org/{channel}"],
            "completed": complete,
        }
        for channel, language in CHANNELS
    ]
    return {
        "id": f"pass-{category}-{stage}-{suffix}",
        "run_on": "2026-08-12",
        "queries": queries,
        "candidate_ids": candidate_ids or [],
        "new_candidate_ids": new_candidate_ids or [],
        "complete": complete,
    }


def candidate_data(**updates: Any) -> dict[str, Any]:
    value = {
        "id": "candidate-example",
        "name": "Example",
        "category": "corpus",
        "discovered_on": "2026-01-01",
        "last_checked_on": "2026-01-01",
        "discovery_urls": ["https://example.org/project"],
        "disposition": "blocked",
        "evidence_gaps": ["Canonical stage evidence unavailable."],
        "refreshed_existing": False,
    }
    value.update(updates)
    return value


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def ledger_path(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.yaml"
    initialize_ledger(path, on=date(2026, 8, 12))
    return path
