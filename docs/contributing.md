# Contributing catalog resources

All authored resources live directly in package data:

```text
src\histgerm\data\
├── corpora\
├── tools\
└── dictionaries\
```

Add one UTF-8 YAML mapping per resource. The directory selects the model; there
is no category field or compatibility dispatch. Use a stable lowercase
kebab-case resource ID with the category prefix `corpus-`, `tool-`, or
`dictionary-`, cite authoritative sources, and retain useful academic
qualifications without inventing unsupported precision.

Contributors may prepare metadata manually as described below. The repository
also contains the manually selected
`histgerm-inventory-curator` custom agent for evidence-backed discovery,
refresh, validation, and pull-request publication. Its research workflow,
ledger, gates, safety limits, and CLI are documented in
[Inventory curator workflow and research ledger](inventory-curator.md).

## Workflow for every resource

1. Review canonical project documentation, release records, terms/licenses,
   and relevant scholarly publications.
2. Record facts only when the evidence supports them. Omit unknown optional
   fields. Use explicit `unclear` for unresolved legal permissions.
3. Add resource-local `Source` entries with access dates and precise dotted
   `supports` names. Include short direct quotations for legal claims and other
   claims where a quotation improves auditability.
4. Add `source_ids` wherever the model provides them.
5. Validate from the repository root:

   ```powershell
   uv run python -m histgerm.validation src\histgerm\data
   ```

6. Load the catalog and exercise the affected find method.
7. Run the repository checks required for the change and use a pull request
   for human review. Never commit third-party corpus text, dictionary content,
   annotations, model weights, binaries, archives, database dumps, software
   packages, credentials, or other payloads.

## Add a corpus

Create `src\histgerm\data\corpora\<id>.yaml`. A corpus requires access and at
least one version; a version requires availability and at least one inline
text. Layers are declared once per version and texts reference their IDs.

```yaml
id: corpus-example
name: Example Corpus
reviewed_on: 2026-08-11
covered_stages: [mhg]
access:
  availability: [described]
  model_training: unclear
  original_data_redistribution: unclear
  processed_data_redistribution: unclear
  trained_weight_publication: unclear
  source_ids: [project]
sources:
  - id: project
    url: https://example.org/corpus
    accessed_on: 2026-08-11
    supports: [name, covered_stages, access, versions, texts, annotations]
    note: Synthetic shape example, not evidence about a real resource.
versions:
  - id: v1
    availability: [described]
    annotations:
      - id: pos
        type: pos
        tagset_name: Example tagset
        source_ids: [project]
    texts:
      - id: text-a
        title: Example Text
        stages: [mhg]
        dialect: documented dialect wording
        date: documented approximate date
        annotation_ids: [pos]
        source_ids: [project]
```

Keep every text inline. Text IDs must be unique across all versions. Qualify
references as `corpus-example:text-a`. Add reported sizes only with a positive
value, enum unit, and source description. Add overlap only when evidenced; use
the correct corpus- or text-level target syntax and a required explanatory
note. Never infer overlap from matching titles or shared provenance.

`covered_stages` is required, directly evidenced corpus-level coverage.
Text-level `stages` remain required when a text is represented. If canonical
evidence establishes an in-scope described corpus but cannot support any
truthful text record, keep the required version field as `texts: []`; never
invent a placeholder text. For curator-authored corpora, record only the
latest directly evidenced release. Conflicting latest-release evidence blocks
the change rather than authorizing a guess.

## Add a tool

Create `src\histgerm\data\tools\<id>.yaml`. Use exact `Task` strings such as
`pos_tagger`, and plain authored format strings such as `plain_text`.

```yaml
id: tool-example
name: Example Tagger
reviewed_on: 2026-08-11
tasks: [pos_tagger]
supported_stages: [mhg]
input_formats: [plain_text]
output_formats: [plain_text]
access:
  availability: [described]
  model_training: unclear
  original_data_redistribution: unclear
  processed_data_redistribution: unclear
  trained_weight_publication: unclear
  source_ids: [project]
sources:
  - id: project
    url: https://example.org/tool
    accessed_on: 2026-08-11
    supports: [name, tasks, supported_stages, input_formats, output_formats, access]
    note: Synthetic shape example, not evidence about a real resource.
```

Do not infer training data, model licensing, supported stages, or formats from
a package name. Metrics are compact mappings requiring `name` and `value`, with
optional `task`, `dataset`, and `note`.

## Add a dictionary

Create `src\histgerm\data\dictionaries\<id>.yaml`.

```yaml
id: dictionary-example
name: Example Dictionary
reviewed_on: 2026-08-11
covered_stages: [mhg]
lexical_features: [headwords, lemmas]
machine_readable: true
api_links: [https://example.org/dictionary/api]
access:
  availability: [described, api]
  model_training: unclear
  original_data_redistribution: unclear
  processed_data_redistribution: unclear
  trained_weight_publication: unclear
  source_ids: [project]
sources:
  - id: project
    url: https://example.org/dictionary
    accessed_on: 2026-08-11
    supports: [name, covered_stages, lexical_features, machine_readable, links, access]
    note: Synthetic shape example, not evidence about a real resource.
```

Set `machine_readable: true` only after verifying structured machine-readable
access. That does not imply bulk download, reuse, redistribution, or training
permission. `corpus_links` must contain existing HistGerm corpus IDs.

## Safe YAML

Authored YAML must be BOM-free UTF-8, contain exactly one document, and have a
mapping at its root. HistGerm rejects anchors, aliases, merge keys, explicit
tags, duplicate keys, non-string keys, empty/whitespace-only keys, unknown
model fields, and empty strings. Symlinked inventory boundaries and files are
not followed.

The package ships these authored YAML files directly. There is no generated
snapshot, manifest, second inventory tree, or old `inventory\` authoring path.
After installing or building a wheel, `load_catalog()` discovers package data
under `histgerm:data` and validates each record while loading.

## Automated curator workflow

The curator starts from a corpus/tool/dictionary and OHG/MHG/ENHG brief, a
public seed, or the next incomplete ledger sweep. It searches in German and
English, dispositions every candidate, refreshes verified existing matches,
and delegates deterministic ledger and validation work to checked-in Python.
Research workers are read-only; only the coordinator writes the ledger,
trusted YAML, and Git state.

Do not start a real batch before the documented owner gate. Local runs also
require a clean, attached, authenticated, current default-branch checkout
before research. Every successful resource, refresh, mixed, or ledger-only
batch is validated, committed on a non-default `copilot/inventory-*` branch,
pushed without force, and opened as a pull request. The agent never merges;
human review and merge are mandatory.

A normal validation failure must be fixed. Draft pull requests are reserved
for unresolved representation/schema decisions or intentionally demonstrated
schema work that cannot yet validate. Minimal evidence-driven schema changes
must update every affected model, validation, query, YAML, test, and
documentation surface; a new public domain model, resource category, generic
resource abstraction, or compatibility layer requires separate design
approval.
