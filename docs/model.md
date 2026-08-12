# Data model and evidence rules

HistGerm has exactly 11 public Pydantic domain concepts. `Catalog` is a loading
and query service, not a twelfth domain record. There is no universal resource
model: corpora, tools, and dictionaries have distinct structures.

All models reject unknown fields and empty strings. Stable IDs are lowercase
kebab-case. Unknown optional facts are omitted rather than guessed.

## The 11 concepts

1. **`BaseResource`** — shared top-level identity for `Corpus`, `Tool`, and
   `Dictionary`: `id`, `name`, optional aliases/description/links, non-empty
   resource-local `sources`, and `reviewed_on`.
2. **`Corpus`** — a top-level resource with directly evidenced, non-empty
   `covered_stages`, `access`, one or more `versions`, optional corpus-level
   `overlaps`, and optional notes.
3. **`CorpusVersion`** — one release with optional label/date/links/license,
   non-empty availability, optional reported sizes, version-local annotation
   layers, a required list of inline texts that may be empty, evidence
   references, and a note.
4. **`CorpusText`** — an inline text with a corpus-local ID, title, stages,
   authored dialect/date strings, referenced annotation layers, evidence, and
   optional authors, genres, text types, regions, languages, witness/edition
   detail, sizes, overlaps, and notes.
5. **`AnnotationLayer`** — a version-local layer with an ID, annotation type,
   optional tagset/quality/production detail, evidence references, and a note.
6. **`Tool`** — a resource with one or more `Task` values, optional supported
   stages/formats/training or evaluation data/metrics/Hugging Face links,
   `access`, and a note.
7. **`Dictionary`** — a resource with optional covered stages/languages,
   lexical features, search/API/download links, machine-readable status,
   citation detail, corpus links, `access`, and a note.
8. **`Source`** — reviewed evidence local to one resource: ID, URL, access date,
   non-empty `supports`, and optional title, citation, quotation, or note.
9. **`Access`** — availability plus four explicit legal permission fields,
   optional license/requirements/note, and supporting source IDs.
10. **`Size`** — a positive reported value, enum unit, source description, and
    optional qualification. HistGerm does not compute or convert sizes.
11. **`Overlap`** — a factual relationship, qualified target, required
    explanatory note, and optional source IDs. It is a warning, not arithmetic.

## The nine enums

| Enum | Accepted YAML/Python strings |
|---|---|
| `LanguageStage` | `ohg`, `mhg`, `enhg` |
| `LegalPermission` | `permitted`, `prohibited`, `unclear` |
| `Availability` | `described`, `browsable`, `downloadable`, `api`, `request_only`, `authentication_required`, `unavailable`, `discontinued` |
| `AnnotationType` | `lemma`, `pos`, `morphology`, `dependencies`, `named_entities`, `normalization`, `dating`, `other` |
| `AnnotationQuality` | `expert_gold`, `manually_corrected`, `silver`, `automatic` |
| `ProductionMethod` | `manual`, `manual_corrected`, `automatic`, `mixed` |
| `Task` | `pos_tagger`, `morphological_tagger`, `lemmatizer`, `syntactic_parser`, `language_model` |
| `SizeUnit` | `text`, `sentence`, `orthographic_word`, `token`, `character`, `byte` |
| `OverlapRelationship` | `duplicate`, `derived_from`, `overlaps`, `same_work` |

Enum strings are exact. Annotation quality and production method are omitted
when unknown; they do not have an `unclear` value. Use annotation type `other`
only with a `tagset_name` or explanatory note.

## Distinct resource structures

`Corpus` contains versions, and each version defines layers and texts inline.
`Tool` has tasks and format/stage metadata but no versions or texts.
`Dictionary` has lexical and access interfaces but no tool tasks. Shared
`BaseResource` fields do not erase these differences.

Corpus texts are never separate package files. A text's `annotation_ids` must
resolve to layers in its version. Text IDs are unique across every version of
their corpus. A bare `m005` is meaningful only inside `corpus-rem`; the
qualified ID is `corpus-rem:m005`.

Top-level resource IDs identify their category:

- corpora use `corpus-`;
- tools use `tool-`;
- dictionaries use `dictionary-`.

The remainder is lowercase kebab-case. Corpus-local text IDs retain their
existing form.

`Corpus.covered_stages` is the authoritative corpus-stage claim and must have
direct source support. `Catalog.find_corpora(stage=...)` uses this field; it
does not infer corpus coverage from titles, dates, dialects, or inline texts.
Each represented text still has its own required `stages` for finer evidence.

`CorpusVersion.texts` is required but may be `[]`. This represents a described
in-scope corpus whose canonical evidence does not support truthful text
records. Do not add placeholder, synthetic, title-only, or guessed texts.
Text queries and text coverage summaries return no rows for a textless
release. Curator-authored corpus records normally contain only the latest
directly evidenced release.

Overlap targets use:

- `corpus-rem` for an in-inventory corpus;
- `corpus-rem:m005` for an in-inventory text;
- `external:corpus-id` or `external:corpus-id:text-id` for an explicitly
  external target.

Corpus-level overlaps target corpus IDs; text-level overlaps target qualified
text IDs. Validation checks in-inventory references. Record only evidenced
relationships, add a note, and warn users even when no numeric extent is known.

## Evidence and legal claims

`Source.supports` uses documented dotted field or section names such as
`access.model_training`, `links.citation`, `versions`, or
`texts.m005.date`. It never uses JSON Pointer syntax. Source IDs and all
`source_ids` references are local to one resource YAML file.

Every legal permission other than `unclear` requires a source listed in
`Access.source_ids` that:

1. names that exact field in `supports`, and
2. contains a direct quotation.

Use explicit `unclear` when reviewed evidence does not establish a permission,
sources conflict, or terms are silent. Accessibility, an API, a download link,
a license name, or common academic practice is not enough to infer permission.
Keep reviewed terms and an explanatory note where available. HistGerm reports
evidence; it does not provide legal advice.

## Scholarly fields and no guessing

Dialect, date, author, genre, text type, region, witness, edition, and
`shared_work_id` are intentionally lightweight authored metadata, not central
registries or entity records. Preserve source wording and uncertainty. Do not
silently modernize labels, turn approximate dates into exact dates, equate
similarly titled witnesses, or infer corpus overlap.

ReM demonstrates why: `m330` keeps text-composition and manuscript evidence
distinct in its dialect/date strings; `char-align` is assigned only where
text-level coverage is verified. RNNTagger uses the exact task
`pos_tagger` and format `plain_text`. MWB is marked machine-readable because a
structured API response was inspected, not because a bulk download or reuse
right was inferred.

## Research models are separate

The auxiliary models under `histgerm.research` describe discovery workflow
state, not catalog domain concepts. They are not re-exported by `histgerm` or
`histgerm.models`. `DiscoveryLedger`, sweep/pass/candidate records, evidence
excerpts, and worker results reuse the trusted domain models for proposed
resources and do not weaken the domain validation rules. See
[Inventory curator workflow and research ledger](inventory-curator.md).
