# Querying HistGerm

Load the bundled package data through the single public entry point:

```python
from histgerm import load_catalog

catalog = load_catalog()
```

The catalog exposes exactly four find methods. Each returns a plain Python list
of matching Pydantic objects in stable load order. Multiple filters on one call
are combined with AND.

## `find_corpora`

```python
corpora = catalog.find_corpora(stage="mhg")
assert [corpus.id for corpus in corpora] == ["res-rem"]
```

With no filter it returns every corpus. `stage` is exact and must be one of
`ohg`, `mhg`, or `enhg`. A corpus matches when any inline text has that stage.

## `find_texts`

```python
texts = catalog.find_texts(
    corpus_id="res-rem",
    text_id="m005",
    stage="mhg",
    dialect="hess-thür",
    annotation_type="lemma",
    tagset="Lexers Mittelhochdeutsches Handwörterbuch (Lexer)",
    date_contains="um 1200",
    has_overlap=False,
)
assert [text.id for text in texts] == ["m005"]
```

`corpus_id` and the bare, corpus-local `text_id` are exact ID matches.
`text_id` is rejected without `corpus_id`, and a qualified value such as
`res-rem:m005` is rejected in the `text_id` argument. Use qualification when
storing or displaying a globally meaningful text reference.

`stage` and `annotation_type` use exact enum values. `dialect` and `tagset`
use case-insensitive exact matching after trimming and collapsing whitespace.
They are not substring or fuzzy searches. `date_contains` is the only substring
filter: it performs a case-insensitive search over the authored free-text date.
`has_overlap=True` requires at least one text overlap; `False` requires none.
Annotation and tagset filters inspect only layers referenced by that text.

Dialect and date strings preserve source terminology. For example, ReM includes
verified values such as `hess-thür`, `um 1200`, and the more explicit M330
values that distinguish text composition from manuscript evidence. Do not
translate abbreviations or normalize dates unless a source supports the change.

## `find_tools`

```python
tools = catalog.find_tools(
    task="pos_tagger",
    stage="mhg",
    output_format="plain_text",
)
assert [tool.id for tool in tools] == ["res-rnntagger"]
```

`task` and `stage` use exact enum values. `output_format` uses normalized exact
membership, not substring matching. Missing optional stage or format metadata
never counts as a match.

## `find_dictionaries`

```python
dictionaries = catalog.find_dictionaries(
    stage="mhg",
    lexical_feature="lemmas",
    machine_readable=True,
)
assert [dictionary.id for dictionary in dictionaries] == ["res-mwb"]
```

`stage` is exact. `lexical_feature` uses normalized exact membership.
`machine_readable` is an exact Boolean filter; omission means either value.

## Legal warnings

```python
warnings = catalog.legal_warnings(tools)
assert {row["field"] for row in warnings} == {
    "model_training",
    "original_data_redistribution",
    "processed_data_redistribution",
    "trained_weight_publication",
}
assert {row["value"] for row in warnings} == {"unclear"}
```

The helper emits one plain dictionary per `prohibited` or `unclear` permission.
It omits `permitted` fields. For a text, it resolves the owning corpus and adds
the qualified text ID. The rows repeat source IDs and the access note so users
can inspect the evidence. They are not legal advice and do not authorize use.

RNNTagger's reviewed terms cover software use and redistribution but do not
separately establish rights for model training, original or processed data, or
trained weights; those fields therefore remain explicitly `unclear`. The MWB
API is machine-readable and freely accessible, but accessibility alone does not
establish reuse or training rights. ReM has direct CC BY-SA evidence for
original-data redistribution; its other modeled permissions remain `unclear`.

## Overlap warnings

```python
warnings = catalog.overlap_warnings(texts)
assert warnings == []
```

Each authored overlap becomes a plain row with owner, relationship, target,
note, and source IDs. Corpus and text overlap warnings remain separate from
legal warnings. HistGerm never subtracts overlap, deduplicates texts, or adjusts
size totals. An empty result means no overlap was authored for those records,
not proof that no overlap exists.

## Coverage summaries

```python
rows = catalog.coverage_summary(texts, by=["stage", "dialect"])
assert rows == [
    {
        "stage": "mhg",
        "dialect": "hess-thür",
        "text_count": 1,
        "text_ids": ["res-rem:m005"],
    }
]
```

Allowed dimensions are `stage`, `dialect`, `annotation_type`, and `tagset`.
The `by` list must be non-empty, contain no duplicates, and use only those
values. Multi-valued dimensions create one row per authored combination.
Counts use distinct qualified text IDs. Coverage is descriptive: absent
metadata remains absent, and layer presence does not measure span completeness,
quality, or suitability.

## No-guessing checklist

- Treat omitted optional fields as unknown, not false.
- Treat `unclear` as unresolved, not permitted.
- Preserve authored dialect and approximate-date wording.
- Do not infer annotation quality, production method, license scope, overlap,
  or corpus linkage from names, URLs, accessibility, or common practice.
- Inspect `sources`, `Source.supports`, quotations, and notes before making a
  research or legal decision.
