# HistGerm

HistGerm is a curated Python catalog of metadata about Historical German
corpora, NLP tools, and dictionaries. It records scholarly descriptions,
coverage, access conditions, evidence, and known overlap; it does not distribute
third-party data, software, or model weights.

The bundled catalog is a verified, evolving inventory, not a claim of a
comprehensive census.

## Install

HistGerm requires Python 3.13 or newer.

```powershell
git clone https://github.com/daanvdn/HistGerm.git
Set-Location HistGerm
uv sync --locked
```

## Load and query

```python
from histgerm import load_catalog

catalog = load_catalog()

corpora = catalog.find_corpora(stage="mhg")

tools = catalog.find_tools(
    task="pos_tagger",
    stage="mhg",
    output_format="plain_text",
)

dictionaries = catalog.find_dictionaries(
    stage="mhg",
    lexical_feature="lemmas",
    machine_readable=True,
)
```

All find methods return ordinary lists of Pydantic objects. HistGerm does not
guess missing facts or permissions. Check warnings and explicit coverage before
using results:

```python
texts = catalog.find_texts(
    corpus_id="corpus-rem",
    text_id="m005",
    dialect="hess-thür",
    date_contains="um 1200",
    annotation_type="lemma",
)

legal = catalog.legal_warnings(texts)
overlap = catalog.overlap_warnings(texts)
coverage = catalog.coverage_summary(texts, by=["stage", "dialect"])
```

`unclear` is a real legal value, not permission. Warning rows are factual
metadata, not legal advice or suitability decisions. Coverage reports only
authored metadata and does not imply completeness.

## Validate authored data

```powershell
uv run python -m histgerm.validation src\histgerm\data
```

Repository-level inventory validation also validates the checked-in
`research\discovery-vocabulary.yaml` when present. The vocabulary is untrusted
research state; its terms, contexts, and classifications are discovery leads,
never inventory evidence.

## Curator research setup

Install the locked research dependencies and compatible browser components
using the checked-in setup:

```powershell
uv sync --locked --group research
uv run python -m playwright install --with-deps chromium
```

Discovery uses Crawl4AI only as a single-URL renderer/extractor: each
invocation receives one canonical inventory URL, never a deep crawl or
discovered-link graph. Its single external cache root is
`%LOCALAPPDATA%\HistGerm\crawl4ai\.crawl4ai` on Windows, or
`${XDG_CACHE_HOME:-~/.cache}/HistGerm/crawl4ai/.crawl4ai` on POSIX, with a
30-day TTL and 512 MiB ceiling. Keep that cache outside the checkout.
Programmatic research runs may set an absolute external base with
`Crawl4AIConfig(cache_base_directory=...)`; Crawl4AI state then lives only in
that base's `.crawl4ai` directory.
Cached/fetched pages, generated Markdown, browser profiles or state, SQLite
files, and downloaded assets must never be committed or packaged.

The initial vocabulary implementation and the MHG tools pilot are separate;
do not run the live pilot as part of setup or implementation validation.

## Guides

- [Data model and evidence rules](docs/model.md)
- [Queries, warnings, and coverage](docs/querying.md)
- [Contributing a corpus, tool, or dictionary](docs/contributing.md)
- [Inventory curator workflow and research ledger](docs/inventory-curator.md)
- [V2 breaking changes and intentional limitations](docs/migration.md)
