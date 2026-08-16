# LAUDATIO Search Provider Plan

## Status

Planning only. Do not implement this document as part of the current task.

## Objective

Add `www.laudatio-repository.org` as a first-class corpus search provider that:

- participates in the required discovery channels;
- uses only the resolver-pinned, redirect-revalidated, byte-bounded transport;
- retrieves the verified public LAUDATIO search interface without authentication,
  browser automation, payload downloads, or guessed pagination;
- normalizes provider records into existing untrusted `SearchResult` objects;
- records truthful exhaustion, truncation, access-gap, and transport evidence;
- is covered by deterministic fixtures and orchestration tests.

## Current architecture

The implementation should extend the existing search stack rather than add a
parallel client:

- `search_providers.py` owns provider identities, request construction,
  pagination bounds, parsing, result normalization, deduplication, and audit
  records.
- `discovery_runtime.py` executes `SearchRequest` objects through
  `fetch_public_metadata()` with a 2 MiB provider-response ceiling.
- `fetching.py` validates public destinations, pins resolved addresses,
  revalidates redirects, restricts content types, and streams responses under a
  byte cap.
- `discovery_orchestration.py` defines the required channels and sends every
  normalized result through the existing semantic inspector.

The runtime currently returns no provider-derived cursor or exhaustion state.
The LAUDATIO work must not mark a request complete merely because one HTTP 200
response was received.

## Verified LAUDATIO interface facts

As observed on 2026-08-16:

- the public search page is `https://www.laudatio-repository.org/search`;
- the page advertises German/English metadata search and `*`/`?` wildcards;
- the site publishes REST API documentation at
  `https://www.laudatio-repository.org/docs/elasticapi/`;
- the documented collection endpoint is
  `https://www.laudatio-repository.org/api/elasticapi/v1/corpora`;
- the documented response is JSON with `success`, `data`, Elasticsearch-style
  identifiers, and corpus metadata under `_source`;
- the documented request includes the static `Api-Version: v1` header;
- the site exposes `laudatioApp.elasticsearchLimit = 100`;
- `robots.txt` allows crawling;
- the live frontend creates an unauthenticated JSON API client at
  `/api/elasticapi/v1` with `Accept: application/json` and
  `Content-Type: application/json`;
- corpus search uses
  `POST /api/elasticapi/v1/corpora/latest/searchMain`;
- the request body is
  `{"searchData":{"from":<offset>,"size":<page-size>,"query":"<term>"}}`;
- the frontend requests later pages by increasing `from` by the configured
  result limit;
- corpus-search responses contain `success`, `status`, and `data`, where each
  item has `_id`, `_index`, and `_source`;
- the frontend builds canonical links as
  `/browse/corpus/<_id>/<_index>`;
- the related `/corpora/latest/searchMain/count` endpoint returned an empty JSON
  string during planning and is not a usable exhaustion signal;
- the documented collection `GET /corpora` returned an empty HTTP 200
  `text/html` response during planning and must not be used as a corpus
  collection;
- non-empty offset pages followed by an empty page were stable for the bounded
  phase probes below.

These observations identify a usable interface, but the implementation must
freeze the request and response contract in sanitized fixtures because the
public API documentation does not cover the frontend search routes.

## Observed phase-query behavior

Bounded probes used `size=20`, at most five pages, and stopped after the first
empty page. All tested queries exhausted after one non-empty page plus one empty
page:

| Phase | Query | Unique results |
| --- | --- | ---: |
| OHG | `Old High German` | 15 |
| OHG | `Althochdeutsch` | 4 |
| OHG | `goh` | 2 |
| MHG | `Middle High German` | 15 |
| MHG | `Mittelhochdeutsch` | 4 |
| MHG | `gmh` | 2 |
| ENHG | `Early New High German` | 15 |
| ENHG | `Frühneuhochdeutsch` | 9 |
| ENHG | `fnhd` | 2 |

The English phase labels returned the same broad 15-result set for all three
phases. Quoting the label did not make it an exact phrase. Appending
`corpus`/`Korpus` also broadened results rather than improving precision.

The German stage labels and ISO 639-3 aliases were more selective, but they did
not cover the same records. For example:

- `Althochdeutsch` found diachronic corpora whose metadata explicitly names the
  German stage;
- `goh` found the two records carrying that exact identifier;
- `Middle High German` surfaced the named reference corpus, while
  `Mittelhochdeutsch` and `gmh` mostly found mixed-stage corpora;
- `Frühneuhochdeutsch` found nine German-labelled records, while `fnhd` added
  the Anselm Corpus.

The provider therefore needs bounded bilingual/alias query variants followed by
normalization, deduplication, and semantic inspection. No single formulation is
both precise and complete.

## Design

### Provider identity and channel

Add `SearchProvider.LAUDATIO = "laudatio"` and a required `laudatio` channel
using `ResponseFormat.API`.

Treat LAUDATIO as a specialist corpus repository:

- activate the channel only for `corpus` discovery targets;
- issue the canonical German stage label and ISO 639-3 alias as the primary
  provider-aware formulations;
- retain the English stage label as a bounded recall formulation because it
  finds title-only records, while recognizing that it is noisy;
- do not append the generic `corpus` or `Korpus` token;
- do not include it in general-web controlled-recall variants;
- preserve the exact executed provider formulation in every audit record;
- keep all returned records untrusted until the existing inspector classifies
  them.

### Safe request construction

Extend `SearchRequest` and the bounded transport to represent the verified JSON
`POST` without adding an arbitrary HTTP-client surface:

- `build_provider_request()` should target
  `/api/elasticapi/v1/corpora/latest/searchMain`;
- encode the provider query in a deterministic JSON body with `from=0` and
  `size=20`;
- allow only checked-in `GET` and JSON `POST` request forms;
- calculate and send a fixed `Content-Length`;
- permit only `Accept`, `Content-Type`, and, if retained for documented
  compatibility, `Api-Version`;
- reject authorization, cookies, proxy/forwarding headers, caller-selected
  methods, arbitrary bodies, and non-JSON content;
- reject 301, 302, and 303 redirects for POST requests; only preserve a POST
  across a resolver-revalidated 307/308 redirect;
- retain the original host for TLS SNI and the `Host` header.

Retain the existing limits:

- maximum 2 MiB per provider response;
- maximum 5 pages and 100 normalized results;
- streaming enforcement when `Content-Length` is absent;
- no response body or API payload written to disk.

Use a fixed LAUDATIO page size of 20. Five pages therefore align exactly with
the repository-wide 100-result ceiling and the site's published
`elasticsearchLimit`.

### JSON parsing and normalization

Add a dedicated `parse_laudatio_api()` parser. Do not route API JSON through
the generic anchor parser.

The parser should:

1. decode JSON and require an object with the documented success/result shape;
2. reject malformed JSON, false success states, wrong container types, and
   records missing a usable identifier or title;
3. read only bounded scalar/list metadata needed for discovery;
4. normalize one corpus record to:
   - `url`: canonical
     `https://www.laudatio-repository.org/browse/corpus/<id>/<index>`, after
     validating that the index is the expected `corpora` collection;
   - `title`: the first non-empty normalized `corpus_title`;
   - `snippet`: a concise deterministic composition of available description,
     historical-language identifiers, authors/editors, genre, publication
     year, and version;
   - `trusted_evidence=False`;
5. accept only HTTPS canonical URLs on
   `www.laudatio-repository.org`;
6. collapse whitespace, ignore duplicate metadata values, cap title/snippet
   lengths, and preserve source order;
7. deduplicate records through the existing stable canonical-URL key.

Do not reproduce LAUDATIO's search syntax locally. Send only the checked-in
stage-label and identifier formulations, and treat provider wildcard behavior
as unsupported until it has a documented contract.

### Pagination and completion semantics

Add provider-specific page-state extraction rather than setting cursor fields
in the generic HTTP transport.

- Treat the cursor as the next numeric `from` offset.
- After a non-empty page, set the next offset to
  `current_offset + returned_item_count`.
- Rebuild only the `from` field; preserve the same endpoint, page size, query,
  method, headers, and all other body fields.
- Mark `exhausted=True` only when a later offset request returns a valid empty
  `data` array.
- Keep an empty first page as `first_page_inconclusive` while the count endpoint
  is unreliable.
- Do not infer exhaustion from a short non-empty page.
- Treat malformed envelopes, repeated pages, repeated offsets, more than 100
  unique records, non-empty pages beyond the fifth request, and contradictory
  response state as existing fail-closed pagination gaps.
- If the API returns an HTTP failure or invalid JSON, preserve the exact status
  and failure stage and continue the other channels.

### Orchestration integration

Add `_Channel("laudatio", SearchProvider.LAUDATIO, ResponseFormat.API)` to the
corpus-target channel selection. Do not run a corpus-only repository endpoint
for dictionary or tool targets.

Update channel-set assertions and serialized-provider assertions so every
corpus discovery pass includes the bounded LAUDATIO stage formulations.
LAUDATIO failures must remain isolated and must not stop Google, Bing, Brave,
CLARIN, OLAC, Zenodo, institutional, GitHub, GitLab, or Hugging Face channels.

## Implementation tasks

### LAUDATIO-001 — Freeze the observed public contract

**Scope**

- LAUDATIO API documentation and public search frontend
- sanitized fixtures under `tests/research/fixtures/`

**Work**

1. Capture the exact JSON POST endpoint, request body, headers, response
   envelope, canonical corpus identifier, and offset behavior described above.
2. Confirm that the interface is unauthenticated and allowed by `robots.txt`.
3. Record that the count route and collection GET are currently unusable and
   must not control completion.
4. Save minimal synthetic/sanitized fixtures for a non-empty page, confirming
   empty page, malformed envelope, invalid JSON, and over-limit sequence.

**Gate**

Do not infer completion from response length or the broken count route.

### LAUDATIO-002 — Add provider request and transport support

**Scope**

- `src/histgerm/research/search_providers.py`
- `src/histgerm/research/discovery_runtime.py`
- `src/histgerm/research/fetching.py` if the version header is required
- `tests/research/test_fetching.py`
- `tests/research/test_discovery_runtime.py`

**Work**

1. Add the provider enum and deterministic JSON POST request builder.
2. Add narrowly validated bounded POST support to the pinned transport.
3. Preserve DNS pinning, TLS hostname validation, redirect revalidation,
   content-type policy, timeout behavior, and byte ceilings.
4. Derive the next numeric offset and confirming-empty-page exhaustion without
   exposing raw bodies outside the existing response boundary.

### LAUDATIO-003 — Normalize API results

**Scope**

- `src/histgerm/research/search_providers.py`
- `tests/research/test_search_providers.py`
- `tests/research/fixtures/`

**Work**

1. Add strict JSON shape validation and provider-specific parsing.
2. Build canonical browse URLs from validated corpus identifiers.
3. Produce deterministic title/snippet values from bounded metadata.
4. Reuse existing inspection, assessment, deduplication, and pagination code.

### LAUDATIO-004 — Wire the discovery channel

**Scope**

- `src/histgerm/research/discovery_orchestration.py`
- `tests/research/test_discovery_orchestration.py`
- agent/discovery documentation only where required-channel lists are explicit

**Work**

1. Add the category-gated `laudatio` corpus channel.
2. Update expected channel sets, ordering-sensitive assertions, metrics, and
   serialized provider checks.
3. Add German-label, ISO-alias, and bounded English-recall formulations for
   OHG, MHG, and ENHG, without appending `corpus`/`Korpus`.
4. Prove that formulation results are deduplicated, reach the standard
   inspector, and remain untrusted leads.
5. Prove that LAUDATIO transport or schema failures are audited without
   stopping the run.

### LAUDATIO-005 — Add regression and safety tests

Cover:

- exact provider identity, host, path, POST method, JSON body, interface format,
  locale, query, and allowed headers;
- two representative historical-German corpus records;
- Unicode titles and metadata;
- missing/empty title, invalid identifier, wrong top-level shape, malformed
  JSON, and `success: false`;
- canonical URL construction and rejection of foreign/private URLs;
- snippet whitespace normalization, deduplication, and length caps;
- stable record ordering and duplicate corpus IDs;
- numeric offset construction and confirming-empty-page exhaustion;
- empty first page, repeated page, repeated offset, invalid envelope, and
  over-limit continuation;
- 100-result and 5-page ceilings;
- declared and streamed byte-limit failures;
- HTTP 429, 500, challenge text, timeout, unsafe POST redirect, and unsafe
  destination handling;
- no authentication, cookies, browser fallback, payload download, or disk
  persistence;
- category-gated orchestration inclusion, phase formulation coverage,
  cross-formulation deduplication, result inspection, audit serialization, and
  failure isolation.

## Acceptance criteria

The item is complete only when:

1. `SearchProvider.LAUDATIO` builds the verified bounded JSON POST to
   `www.laudatio-repository.org`.
2. LAUDATIO is attempted for corpus targets using the bounded German-label,
   ISO-alias, and English-recall formulations, and is skipped for tools and
   dictionaries.
3. Every request uses the existing pinned bounded transport and the 2 MiB
   provider ceiling.
4. Bounded POST support accepts only deterministic JSON bodies and static,
   allowlisted, non-secret headers, and fails closed on unsafe redirects.
5. API records normalize to canonical LAUDATIO corpus URLs, deterministic
   titles/snippets, and `trusted_evidence=False`.
6. Every normalized item is inspected before response classification.
7. Completion is reported only after a valid empty page at a later numeric
   offset; a short page or broken count response never proves exhaustion.
8. Malformed, oversized, unavailable, challenged, or contradictory responses
   fail closed and do not stop other providers.
9. No third-party response body, corpus payload, credential, cookie, or browser
   state is persisted.
10. Targeted and full repository validation pass.

## Validation commands

```powershell
uv run pytest tests\research\test_search_providers.py tests\research\test_fetching.py tests\research\test_discovery_runtime.py tests\research\test_discovery_orchestration.py
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run python -m histgerm.research validate --ledger research\discovery-ledger.yaml --format json
uv run python -m histgerm.validation src\histgerm\data
uv build --no-sources
git diff --check
```

Inspect the wheel and source archive to confirm that fixtures are sanitized and
that no live response bodies, fetched pages, caches, or third-party payloads are
included.

## Non-goals

- Downloading LAUDATIO corpus files or TEI/annotation payloads.
- Treating LAUDATIO metadata as trusted evidence.
- Using login, CSRF tokens, cookies, browser automation, Elasticsearch SDKs, or
  direct access to an underlying Elasticsearch service.
- Depending on the broken collection GET or search-count route.
- Guessing undocumented wildcard, exact-phrase, sorting, or count semantics.
- Raising the repository-wide page, result, byte, redirect, or timeout limits.
- Changing unrelated discovery providers or ranking results.
