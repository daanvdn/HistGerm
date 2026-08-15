# Discover CLI Runtime Capabilities Plan

## Status

Planning only. Do not implement this document as part of the current task.

## Problem

The `discover` command is executable only when called from the same Python
process with a constructed `DiscoveryDependencies` object:

```python
main(arguments, discovery_dependencies=dependencies)
```

This works in tests, but not through:

```powershell
uv run python -m histgerm.research discover ...
```

The module entry point calls `main()` without dependencies. The command
therefore exits with `capability_unavailable` before discovery starts.

The missing values are Python callables or runtime objects for:

1. model-led elicitation;
2. bounded metadata retrieval;
3. provider request execution;
4. item-level result inspection.

A prompt-hosted custom agent can invoke commands and tools, but it cannot pass
live Python callback objects into a separate CLI process. The current
in-process injection seam is consequently not a usable production interface
for the agent.

## Success condition

From the custom agent, a coordinator can run a documented `discover` CLI loop
that:

- does not exit with `capability_unavailable`;
- uses the exact pinned model through the hosting agent, not an undeclared
  model API;
- performs all network retrieval through the checked-in resolver-pinned,
  byte-bounded transport;
- asks the hosting agent for only bounded model or classification judgments;
- validates every exchanged object with checked-in strict models;
- preserves exact provider, locale, query, request, pagination, result,
  inspection, and access-gap audit data;
- never stores raw pages, credentials, model rationale, browser state, or
  fetched payloads in the repository or checkpoint;
- resumes safely after each interaction without repeating completed external
  requests;
- produces the existing final `DiscoveryRunResult` JSON shape;
- leaves ledger and vocabulary mutation under their existing explicit,
  revision-safe commands.

The MHG tool sweep is the acceptance scenario: the custom agent must be able to
start discovery, answer every bounded interaction request, resume it, and
receive a final result or a truthful policy/access gap without constructing
`DiscoveryDependencies` in Python.

## Recommended design

Use a **resumable capability-exchange protocol**, not dynamic plugins or an
implicit model/network client.

### Keep deterministic capabilities inside the CLI

Create production adapters for capabilities that already have safe,
deterministic implementations:

- catalog loading through `load_catalog()`;
- metadata retrieval through `histgerm.research.fetching`;
- provider transport through the same resolver-pinned fetching module;
- provider response parsing, pagination, deduplication, and auditing through
  `search_providers`;
- vocabulary loading and deterministic reconciliation through existing
  modules.

These adapters must not use `requests`, `curl`, hostname fallback, environment
proxy discovery, provider SDKs, credentials, or a second cache.

### Exchange only judgments across the process boundary

Pause discovery when it needs a capability available only in the hosting
agent:

1. `model_elicitation`: one exact bounded prompt expecting the existing
   name-and-alias JSON response;
2. `result_inspection`: a bounded batch of normalized search-result items
   expecting one `lead` or `unrelated` classification and reason per item.

The CLI emits strict JSON requests. The custom agent returns strict JSON
responses in a later invocation. No chain-of-thought or free-form model
rationale is accepted or persisted.

This split keeps DNS pinning, TLS verification, redirects, byte limits,
pagination, and raw-body handling inside trusted code while allowing the
prompt-hosted model to perform the two judgments that cannot be instantiated
by a standalone Python process.

## Proposed CLI interaction

Keep `discover` as the public command and add explicit start/resume modes:

```powershell
uv run python -m histgerm.research discover `
  --category tool `
  --stage mhg `
  --checkpoint "$env:TEMP\histgerm-discover-<run-id>.json"
```

Expected response while interaction is required:

```json
{
  "ok": true,
  "command": "discover",
  "state": "needs_input",
  "checkpoint_revision": 1,
  "requests": []
}
```

The coordinator writes a strict response object to another OS-temporary file:

```powershell
uv run python -m histgerm.research discover `
  --resume "$env:TEMP\histgerm-discover-<run-id>.json" `
  --input "$env:TEMP\histgerm-discover-response-<run-id>.json"
```

The loop continues until the CLI returns:

```json
{
  "ok": true,
  "command": "discover",
  "state": "complete",
  "result": {}
}
```

`needs_input` is an expected successful state and should use exit code `0`.
Malformed, stale, mismatched, unsafe, or policy-invalid exchanges retain
failure-shaped JSON and nonzero exit codes.

The response file must be deleted after a successful resume. The checkpoint
must be deleted after completion or terminal failure.

## Exchange models

Add strict, versioned Pydantic models with `extra="forbid"`:

- `DiscoveryCheckpoint`
  - schema version;
  - run ID;
  - monotonic checkpoint revision;
  - category, stage, bounds, and run date;
  - completed deterministic phase state;
  - normalized elicited leads;
  - authored query plan;
  - normalized provider assessments and inspection state;
  - pending request IDs;
  - completion gaps.
- `ModelElicitationRequest`
  - request ID;
  - exact prompt;
  - iteration and prompt kind;
  - output character and candidate bounds.
- `ModelElicitationResponse`
  - matching request ID;
  - raw JSON text conforming to the existing elicitation schema.
- `ResultInspectionRequest`
  - request ID;
  - category, stage, exact query, provider, and locale;
  - bounded normalized items containing position, URL, title, and snippet.
- `ResultInspectionResponse`
  - matching request ID;
  - exactly one classification and concise reason per position.

Request IDs must be deterministic for the run step. Resume must reject missing,
extra, duplicate, previously consumed, or wrong-run responses.

## Checkpoint safety

The checkpoint is operational state, not a persistent report.

- Require an explicitly supplied absolute path outside the repository.
- Reject repository-relative, symlink-escaping, device, and non-local paths.
- Write atomically with restrictive user-only permissions where supported.
- Limit checkpoint and response sizes.
- Store UTF-8 JSON only.
- Store normalized result metadata, never raw response bodies.
- Store no credentials, cookies, authorization headers, environment values,
  private URLs, browser state, local cache paths, model rationale, or fetched
  page text.
- Record a canonical digest of immutable run parameters and the prior
  checkpoint revision to detect accidental replacement or stale resume.
- Delete checkpoint and response files at terminal completion or failure.

The repository must continue excluding checkpoints and temporary responses
from wheel, sdist, commits, and pull-request payloads.

## Implementation phases

### Phase 1: Contract and decision record

1. Document why Python callback injection cannot cross the CLI boundary.
2. Fix the division of responsibility:
   - CLI owns deterministic retrieval, parsing, bounds, and state;
   - hosting agent owns model elicitation and semantic item inspection.
3. Define request, response, checkpoint, exit-code, cleanup, and final-result
   contracts.
4. Confirm that expected `needs_input` responses do not weaken existing
   failure semantics.

**Gate:** approve the protocol before changing orchestration.

### Phase 2: Production-safe deterministic adapters

1. Add a bounded vocabulary transport adapter over
   `fetch_public_metadata`.
2. Add a provider adapter that:
   - accepts only `SearchRequest`;
   - invokes the checked-in pinned transport immediately per request and
     redirect;
   - enforces metadata content and byte policies;
   - returns `ProviderResponse` with explicit pagination metadata;
   - deletes temporary raw content after parsing.
3. Keep controlled-browser/Crawl4AI use separately gated and optional. Missing
   browser capability must produce an explicit gap, not prevent bounded HTTP
   discovery from starting.
4. Add a runtime dependency factory for deterministic dependencies only. It
   must not accept import paths, arbitrary callables, or secret-bearing
   configuration.

**Gate:** offline synthetic transport tests prove resolver calls, IP pinning,
redirect revalidation, payload rejection, byte limits, cleanup, and pagination.

### Phase 3: Resumable orchestration

1. Refactor the synchronous callback flow into explicit deterministic phases.
2. At a model or inspection boundary, serialize a bounded request and return
   `needs_input`.
3. On resume, validate and consume responses once, then continue from the next
   phase.
4. Preserve current query ordering, lead bounds, pagination, feedback loops,
   completion gates, metrics, and final `DiscoveryRunResult`.
5. Ensure resume never repeats already confirmed retrieval attempts.

Prefer a small explicit state machine over serializing generators, coroutines,
closures, or arbitrary Python objects.

**Gate:** interruption/resume tests produce byte-equivalent final normalized
results to the current fully injected in-process fixture.

### Phase 4: CLI wiring

1. Extend `discover` arguments with mutually exclusive start/resume inputs.
2. Validate checkpoint paths before any external request.
3. Return JSON-only `needs_input`, `complete`, and failure responses.
4. Keep the existing `main(..., discovery_dependencies=...)` seam temporarily
   for unit tests, but route both paths through the same state machine.
5. Deprecate direct full dependency injection only after parity is proven.

**Gate:** a real subprocess test can complete discovery through repeated CLI
invocations without importing `main()` or passing callbacks.

### Phase 5: Custom agent and skill loop

Update the curator agent and discovery skill to:

1. create one uniquely named OS-temporary checkpoint;
2. invoke `discover`;
3. dispatch each emitted request to the correct bounded capability;
4. write only model-valid response JSON to an OS-temporary file;
5. resume using the latest checkpoint revision;
6. report progress at existing phase boundaries;
7. delete temporary files;
8. retain the final discovery object unchanged for curation handoff.

The prompt must never reconstruct checkpoint state, edit a request, invent a
provider response, or treat model/inspection output as evidence.

**Gate:** semantic contract tests prove the prompt loops on `needs_input`,
handles stale responses, cleans temporary files, and never bypasses the CLI.

### Phase 6: End-to-end validation

Run:

```powershell
uv run python -m histgerm.research validate `
  --ledger research\discovery-ledger.yaml `
  --format json
uv run python -m histgerm.research vocabulary-validate `
  --vocabulary research\discovery-vocabulary.yaml `
  --format json
uv run python -m histgerm.validation src\histgerm\data
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv build --no-sources
git diff --check
```

Inspect wheel and sdist members to confirm checkpoints, responses, ledger,
vocabulary, raw pages, browser state, and third-party payloads are absent.

## Required tests

### Protocol tests

- start emits one bounded elicitation request;
- valid response advances to the next request or phase;
- invalid JSON, extra fields, oversized output, wrong request ID, duplicate
  response, and stale checkpoint revision fail closed;
- responses are consumed exactly once;
- completion deletes checkpoint and response files;
- terminal failure cleans temporary state.

### Adapter tests

- original and redirect destinations are resolved immediately;
- mixed/private DNS is rejected;
- Host, TLS SNI, and certificate hostname are preserved;
- no hostname fallback occurs;
- missing `Content-Length` remains allowed under streaming limits;
- payload-like content, oversize content, authentication, challenge, consent,
  rate limit, and unsafe pagination become exact gaps;
- raw bodies are deleted and never serialized to checkpoints.

### Parity tests

- injected in-process and resumable subprocess runs produce the same normalized
  elicitation, queries, assessments, leads, metrics, and completion gaps;
- model follow-up termination and metadata feedback loops resume correctly;
- pagination does not repeat prior pages after resume;
- a synthetic MHG model is surfaced through architecture recall and
  cross-channel pivots.

### Agent tests

- exact pinned model is used for elicitation;
- inspection requests preserve every item and position;
- untrusted responses never become evidence;
- the agent cannot mark an incomplete request sequence complete;
- the final discovery handoff remains model-valid and lossless.

## Rejected alternatives

### Import-path plugins or environment-selected callables

Reject. Dynamic imports expand executable configuration, make provenance
unclear, and create an unsafe arbitrary-code boundary.

### Provider SDKs or direct model API credentials

Reject. They introduce credentials, new dependencies, provider coupling, and
a second network path outside the checked-in resolver-pinned transport.

### Calling the Copilot CLI recursively from Python

Reject. It is difficult to authenticate and bound, couples the package to one
host executable, risks recursive agent behavior, and does not solve safe item
inspection cleanly.

### Letting the prompt reproduce orchestration manually

Reject. It bypasses deterministic query, pagination, completion, revision, and
audit logic—the exact controls the CLI is intended to enforce.

### Serializing Python callbacks, generators, or pickles

Reject. These are non-portable executable payloads and unsafe to load.

### Making result inspection purely lexical

Do not use as the sole solution. Deterministic prefilters are useful, but
semantic relevance decisions still need a bounded judgment path and an
auditable reason.

## Rollout and compatibility

1. Land models and protocol behind an explicit CLI option.
2. Keep current injected tests until subprocess parity is complete.
3. Switch the custom agent to the resumable loop.
4. Run a dry synthetic MHG tool sweep with no live requests.
5. Run the mandatory local preflight.
6. Resume the real `tool/mhg` sweep only after capability proof succeeds.
7. Remove or narrow the old callback-only public path in a later cleanup.

No existing ledger or vocabulary migration should be required. Checkpoint
schema changes are ephemeral and versioned; unsupported versions fail with an
actionable error rather than being upgraded implicitly.

## Completion checklist

- [ ] Protocol design approved.
- [ ] Deterministic production adapters implemented and tested.
- [ ] Resumable state machine implemented.
- [ ] Subprocess CLI start/resume tests pass.
- [ ] Custom agent and discovery skill use the loop.
- [ ] Synthetic and real capability preflights pass.
- [ ] Full validation and packaging policy pass.
- [ ] MHG tool discovery starts without `capability_unavailable`.
- [ ] Temporary checkpoint and response files are removed.