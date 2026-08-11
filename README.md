# HistGerm

HistGerm is a curated, versioned inventory and typed Python library for
discovering metadata about Historical German corpora, dictionaries, NLP tools,
annotations, access conditions, provenance, and relationships.

The project stores metadata and external links only. It does **not** distribute
third-party corpora, dictionary contents, software bundles, model weights, or
other resource payloads. Access and permission metadata is not legal advice;
inspect the cited terms for the exact resource version and distribution.

## Requirements and installation

HistGerm requires Python 3.13 or newer and uses
[uv](https://docs.astral.sh/uv/).

```powershell
git clone https://github.com/daanvdn/HistGerm.git
Set-Location HistGerm
uv sync --locked --all-groups
```

For a runtime-only environment, use `uv sync --locked --no-dev`.

## Quick start

The installed package contains a canonical, verified inventory snapshot:

```python
from histgerm.packaging import load_verified_bundled_catalog
from histgerm.query import CatalogQuery

catalog = load_verified_bundled_catalog()
query = CatalogQuery(catalog=catalog)

match = query.find("Reference Corpus of Middle High German")[0]
assert match.resource_id == "res-rem"

for resource in query.by_language_stage(frozenset({"mhg"})):
    print(resource.resource_id, resource.canonical_name)
```

See [Querying HistGerm](docs/querying.md) for filters, annotations, coverage,
overlap-aware size summaries, relationships, suitability analysis, and
external-reference-only training manifests.

## Representative MVP inventory

The checked-in snapshot currently demonstrates three resource shapes:

- `res-rem`: Reference Corpus of Middle High German (corpus);
- `res-mwb`: Mittelhochdeutsches Wörterbuch (dictionary);
- `res-rnntagger`: RNNTagger (POS tagging and lemmatization tool).

These are representative records, not a comprehensive census of Historical
German resources. Unknown and unclear values are intentional: HistGerm does not
infer missing facts or permissions.

## Authoring and validation

`inventory/**/*.yaml` is the reviewed authoring source. The package snapshot in
`src/histgerm/resources/inventory/` is generated and must not be hand-edited.
Authoring YAML is UTF-8 without a BOM and rejects aliases, anchors, merge keys,
explicit tags, duplicate keys, non-string keys, and multiple documents.

```powershell
uv run python -m histgerm.validation inventory
uv run python -m histgerm.packaging check inventory src\histgerm\resources\inventory
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv build --no-sources
```

Validation, tests, snapshot checks, and examples are deterministic and
network-free after dependencies are installed. Research and legal review remain
human responsibilities. See
[Contributing inventory metadata](docs/contributing-inventory.md).

## Current limitations

- The inventory is deliberately small and may contain unresolved metadata.
- Query APIs operate locally over an immutable in-memory catalog; there is no
  database, web service, downloader, fuzzy search, or automatic legal decision.
- Name lookup is normalized exact matching, not substring or relevance search.
- Coverage and size results use explicit metadata only; unknowns and unresolved
  overlaps remain visible.
- Manifests contain safe external references, metadata, evidence IDs, and
  warnings only. They do not download data or authorize its use.
