# HistGerm

HistGerm is a curated Python catalog of metadata about Historical German
corpora, NLP tools, and dictionaries. It records scholarly descriptions,
coverage, access conditions, evidence, and known overlap; it does not distribute
third-party data, software, or model weights.

The bundled catalog is deliberately small: ReM (`res-rem`), RNNTagger
(`res-rnntagger`), and the Mittelhochdeutsches Wörterbuch (`res-mwb`). It is a
verified demonstration inventory, not a comprehensive census.

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
assert [item.id for item in corpora] == ["res-rem"]

tools = catalog.find_tools(
    task="pos_tagger",
    stage="mhg",
    output_format="plain_text",
)
assert [item.id for item in tools] == ["res-rnntagger"]

dictionaries = catalog.find_dictionaries(
    stage="mhg",
    lexical_feature="lemmas",
    machine_readable=True,
)
assert [item.id for item in dictionaries] == ["res-mwb"]
```

All find methods return ordinary lists of Pydantic objects. HistGerm does not
guess missing facts or permissions. Check warnings and explicit coverage before
using results:

```python
texts = catalog.find_texts(
    corpus_id="res-rem",
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

## Guides

- [Data model and evidence rules](docs/model.md)
- [Queries, warnings, and coverage](docs/querying.md)
- [Contributing a corpus, tool, or dictionary](docs/contributing.md)
- [V2 breaking changes and intentional limitations](docs/migration.md)
