"""Static contracts for the inventory curator agent and its four skills."""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
AGENT = ROOT / ".github" / "agents" / "histgerm-inventory-curator.agent.md"
CURATOR_DOC = ROOT / "docs" / "inventory-curator.md"
README = ROOT / "README.md"
VOCABULARY = "research/discovery-vocabulary.yaml"
SKILL_ROOT = ROOT / ".github" / "skills"
SKILLS = (
    "discover-histgerm-resources",
    "curate-histgerm-resource",
    "validate-histgerm-inventory",
    "publish-histgerm-batch",
)
PERMISSIONS = (
    "model_training",
    "original_data_redistribution",
    "processed_data_redistribution",
    "trained_weight_publication",
)
AGENT_FRONTMATTER = "\n".join(
    (
        "name: histgerm-inventory-curator",
        "description: Discovers, verifies, adds, refreshes, validates, and "
        "publishes evidence-backed Historical German inventory metadata.",
        "model: gpt-5.6-sol",
        'tools: ["read", "search", "edit", "execute", "web", "agent", "github/*"]',
        "disable-model-invocation: true",
        "user-invocable: true",
    )
)


def read_contract(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    _, frontmatter, body = text.split("---", maxsplit=2)
    return frontmatter.strip(), " ".join(body.split())


@pytest.fixture(scope="module")
def contracts() -> dict[str, str]:
    return {
        "agent": read_contract(AGENT)[1],
        **{name: read_contract(SKILL_ROOT / name / "SKILL.md")[1] for name in SKILLS},
    }


def assert_phrases(policy: str, phrases: tuple[str, ...]) -> None:
    for phrase in phrases:
        assert phrase in policy


def assert_concepts(policy: str, *concepts: tuple[str, ...]) -> None:
    """Require each policy concept while allowing clear equivalent wording."""

    folded = policy.casefold()
    for alternatives in concepts:
        assert any(term.casefold() in folded for term in alternatives), alternatives


def assert_sentence_concepts(
    policy: str, anchor: str, *concepts: tuple[str, ...]
) -> None:
    """Require related concepts to coexist in the same contract sentence."""

    sentences = re.split(r"(?<=[.!?;])\s+", policy)
    matching = [
        sentence.casefold()
        for sentence in sentences
        if anchor.casefold() in sentence.casefold()
    ]
    assert matching, anchor
    assert any(
        all(
            any(term.casefold() in sentence for term in alternatives)
            for alternatives in concepts
        )
        for sentence in matching
    ), (anchor, concepts)


def test_exact_agent_and_skill_inventory_contract() -> None:
    """The manually invoked agent pins its model and wires exactly four skills."""

    agents = sorted((ROOT / ".github" / "agents").glob("*.agent.md"))
    assert agents == [AGENT]
    frontmatter, body = read_contract(AGENT)
    assert frontmatter == AGENT_FRONTMATTER
    assert len(body) < 30_000

    required = body.partition("## Required skills")[2].partition("## ")[0]
    assert tuple(re.findall(r"\d+\. `([^`]+)`", required)) == SKILLS
    for name in SKILLS:
        skill_frontmatter, _ = read_contract(SKILL_ROOT / name / "SKILL.md")
        assert skill_frontmatter.startswith(f"name: {name}\n")
        assert set(re.findall(r"^([a-z-]+):", skill_frontmatter, re.MULTILINE)) == {
            "name",
            "description",
        }


def test_worker_and_deterministic_write_boundaries(
    contracts: dict[str, str],
) -> None:
    """Workers return validated JSON while only the coordinator mutates state.

    The safety invariant is the read-only-worker / coordinator-only-write split,
    not any single sentence, so it is expressed as concepts that tolerate
    equivalent wording rather than frozen phrase-presence assertions.
    """

    agent = contracts["agent"]
    discover = contracts[SKILLS[0]]
    curate = contracts[SKILLS[1]]
    validate = contracts[SKILLS[2]]
    publish = contracts[SKILLS[3]]
    assert_concepts(
        agent,
        ("workers are read-only", "worker is read-only", "read-only worker"),
        ("each worker exactly one candidate", "one candidate per worker"),
        ("same worker for one correction", "one correction attempt"),
        ("after a second invalid response", "second invalid response"),
        (
            "only the coordinator may write trusted resource yaml",
            "only the coordinator writes trusted",
        ),
    )
    assert_concepts(
        discover,
        ("workers are strictly read-only", "strictly read-only"),
        (
            "only the coordinator may mutate the ledger",
            "coordinator may mutate the ledger",
        ),
        ("must not write trusted resource yaml", "not write trusted resource yaml"),
        (
            "do not duplicate selection, schema, counter, completion",
            "not duplicate selection",
        ),
    )
    assert_concepts(
        curate,
        ("research exactly one supplied candidate", "exactly one supplied candidate"),
        (
            "do not change the ledger, resource yaml, git state",
            "writes no repository state",
        ),
        ("return only one json object", "one json object"),
    )
    assert_concepts(
        validate,
        ("never edits source, test, ledger, inventory", "never edits"),
    )
    assert_concepts(publish, ("coordinator-only write operation", "coordinator-only"))


def test_lossless_discovery_curation_apply_handoff(
    contracts: dict[str, str],
) -> None:
    """Discovery keeps its schema while the coordinator retains worker evidence."""

    agent = contracts["agent"]
    discover = contracts[SKILLS[0]]
    curate = contracts[SKILLS[1]]
    # Retained exact: the serialized empty-discovery output is a byte-for-byte
    # JSON schema contract that the coordinator parses, so its wording is fixed.
    assert '{"candidate_entries":[],"search_passes":[],"ledger_revision":0}' in discover
    assert_concepts(
        discover,
        ("complete and exact discovery output contract", "discovery output contract"),
        (
            "does not invoke candidate research workers",
            "not invoke candidate research workers",
        ),
        (
            "custom-agent coordinator dispatches every eligible",
            "coordinator dispatches every eligible",
        ),
        ("returned candidate",),
        ("applies the validated `candidateresearchresult`", "validated result"),
        ("retains it for trusted yaml and review", "retain"),
    )
    assert_concepts(
        agent,
        ("treat the discovery/curation handoff as lossless", "handoff as lossless"),
        ("retain each raw response", "retain the raw"),
        ("apply that exact result", "apply the exact result"),
        (
            "never reconstruct either from the resulting `candidateentry`",
            "never reconstruct",
        ),
        ("mandatory for refresh results", "refresh results"),
    )
    assert_concepts(
        curate,
        ("return the full validated result", "full validated result"),
        ("apply this same object with `apply-result`", "apply-result"),
        ("retain its `evidence` and `proposed_record`", "evidence and proposed_record"),
        (
            "refresh mode has the identical result and retention contract",
            "identical result and retention contract",
        ),
    )


def test_native_discovery_run_journal_is_complete(
    contracts: dict[str, str],
) -> None:
    """The skill and guide describe native orchestration recorded in a journal."""

    documented = " ".join(CURATOR_DOC.read_text(encoding="utf-8").split())
    discover = contracts[SKILLS[0]]
    for policy in (discover, documented):
        assert_concepts(
            policy,
            ("run journal",),
            ("*.journal.jsonl",),
            ("outside the repository",),
            ("journal-append",),
            ("journal-status",),
            ("journal-validate",),
            ("journal-compact",),
            ("--expected-last-sequence",),
            ("idempotent",),
            ("(run_id, sequence)",),
            ("never repeats a confirmed retrieval",),
            ("machine-driven",),
            ("query_planned",),
            ("query_executed",),
            ("provider_gap",),
            ("lead_found",),
            ("candidate_researched",),
            ("candidate_blocked",),
            ("run_completed",),
            ("never evidence",),
        )
    assert_concepts(
        discover,
        ("native copilot orchestration", "native orchestration"),
        ("intent_id",),
        ("user-visible progress",),
        ("stale ledger revision", "stale revision"),
        ("stop",),
    )


def test_discovery_journal_recovery_is_actionable(
    contracts: dict[str, str],
) -> None:
    """Journal-driven recovery is machine-driven with an actionable stop report."""

    documented = " ".join(CURATOR_DOC.read_text(encoding="utf-8").split())
    for policy in (contracts["agent"], contracts[SKILLS[0]], documented):
        assert_concepts(
            policy,
            ("journal-append",),
            ("journal-status",),
            ("journal-validate",),
            ("idempotent",),
            ("(run_id, sequence)",),
            ("--expected-last-sequence",),
            ("torn trailing line",),
            ("mid-file corruption",),
            ("retried once",),
            ("model_response_invalid",),
            ("candidate_blocked",),
            ("machine-driven",),
            ("stop report",),
            ("run identifier",),
            ("content hash",),
            ("validator",),
            ("expected versus received", "expected and received"),
            ("mutation status",),
            ("resume", "resuming"),
            ("never commit", "never committed"),
        )


def test_seed_and_resource_identity_contracts_are_semantic(
    contracts: dict[str, str],
) -> None:
    """Seeds remain lossless leads and related project artifacts stay distinct."""

    documented = " ".join(CURATOR_DOC.read_text(encoding="utf-8").split())
    for policy in (
        contracts["agent"],
        contracts[SKILLS[0]],
        contracts[SKILLS[1]],
        documented,
    ):
        assert_concepts(
            policy,
            ("dedicated historical-language resource",),
            ("generic or modern-language component",),
            ("training/evaluation corpus",),
            ("downstream application or pipeline",),
            ("shared authors",),
            ("shared corpus", "shared corpora"),
            ("integration",),
            (
                "does not prove identity",
                "do not prove identity",
                "does not establish identity",
                "do not establish identity",
            ),
            ("generic component applied to mhg",),
            ("canonical component-level evidence",),
            ("retain it as a lead", "remains a lead", "block exact scope/identity"),
        )

    for policy in (contracts["agent"], contracts[SKILLS[0]], documented):
        assert_concepts(
            policy,
            ("every distinct",),
            ("named lead",),
            ("alias",),
            ("exact source wording",),
            ("seed url",),
            ("public resource url",),
            ("losslessly",),
            (
                "does not narrow",
                "do not narrow",
                "never narrow",
                "let the seed narrow",
                "not permission to narrow",
            ),
            ("no model exists",),
            ("query-gap lead",),
            (
                "never evidence of absence",
                "not evidence of absence",
                "never treat them as evidence of absence",
            ),
            ("bounded",),
            ("task-family", "task family"),
            ("follow-up",),
        )

    assert_concepts(
        documented,
        ("reaching that follow-up bound",),
        ("incomplete",),
        ("explicit gap",),
    )


def test_discovery_is_bilingual_complete_and_refreshes_matches(
    contracts: dict[str, str],
) -> None:
    """Every language/channel is covered and existing matches enter refresh."""

    policy = contracts[SKILLS[0]]
    # Bilingual completeness: German *and* English stage vocabulary must both
    # appear, so each stage name is required as its own concept.
    assert_concepts(
        policy,
        ("althochdeutsch",),
        ("old high german",),
        ("mittelhochdeutsch",),
        ("middle high german",),
        ("frühneuhochdeutsch",),
        ("early new high german",),
    )
    assert_concepts(policy, ("korpus", "corpus"), ("wörterbuch", "dictionary"))
    # Channel coverage across both search locales and every required provider.
    assert_concepts(
        policy,
        ("german-language web search", "german web search"),
        ("english-language web search", "english web search"),
        ("clarin",),
        ("olac",),
        ("zenodo",),
        ("institutional catalogs", "institutional catalog"),
        ("github repository search", "github"),
        ("hugging face",),
    )
    # No-false-completion contract for incomplete sweeps and pending candidates.
    assert_concepts(
        policy,
        ("explicit policy reason", "policy reason"),
        ("completed: false", "completed:false"),
        ("semantically unrelated", "unrelated"),
        ("must not be reported as zero candidates", "zero candidates"),
        ("selected sweep remains incomplete", "sweep remains incomplete"),
        ("never silently present that handoff", "silently present"),
        (
            "two consecutive complete passes with no new candidates",
            "two consecutive complete passes",
        ),
        ("an incomplete pass never advances", "incomplete pass never advances"),
        ("no pass may end with a pending candidate", "pending candidate"),
        (
            "sent immediately through curation in refresh mode",
            "curation in refresh mode",
        ),
    )
    # Structural enum contract: the four dispositions are parsed as backticked
    # tokens rather than asserted as raw phrase presence.
    tokens = set(re.findall(r"`([a-z_]+)`", policy))
    assert {"added", "duplicate", "out_of_scope", "blocked"} <= tokens


def test_focused_queries_cover_broad_bilingual_concepts(
    contracts: dict[str, str],
) -> None:
    """Discovery separates concepts while covering broader terms and tagsets."""

    discover = contracts[SKILLS[0]]
    assert_concepts(
        discover,
        ("one concept at a time", "one resource/task concept"),
        ("never combine unrelated task families",),
        ("tagging", "wortartenannotation"),
        ("morphology", "morphologische annotation"),
        ("lemmatization", "lemmatisierung"),
        ("normalization", "normalisierung"),
        ("parsing", "syntaxanalyse"),
        ("segmentation", "tokenisierung"),
        ("tokenizer", "tokenisierung"),
        ("language model", "sprachmodell"),
        ("pretrained language model", "vortrainiertes sprachmodell"),
        ("masked language model", "maskiertes sprachmodell"),
        ("word embedding", "worteinbettung", "wortrepräsentation"),
        ("bert architecture", "bert-architektur"),
        ("bert family", "bert-modellfamilie"),
        ("pipeline", "sprachverarbeitung"),
        ("stts",),
        ("hits",),
        ("corpus and dictionary", "corpus and dictionary terms"),
    )
    assert_concepts(discover, ("german and english queries", "german and english"))


def test_repository_metadata_signals_force_bounded_follow_up(
    contracts: dict[str, str],
) -> None:
    """README-only architecture, stage, and platform signals cannot be dropped."""

    synthetic_readme_signal = (
        ("only inspected source",),
        ("stage wording",),
        ("architecture family",),
        ("canonical cross-platform link",),
        ("untrusted leads",),
        ("requires bounded follow-up discovery",),
    )
    documented = " ".join(CURATOR_DOC.read_text(encoding="utf-8").split())
    for policy in (contracts["agent"], contracts[SKILLS[0]], documented):
        assert_concepts(
            policy,
            ("readme metadata", "repository readme"),
            ("model cards",),
            ("topics",),
            ("aliases",),
            ("authors",),
            ("institutions",),
            ("canonical cross-platform links",),
            ("solely as untrusted leads",),
            ("bounded",),
            ("follow-up",),
            ("cross-channel identity pivots",),
            ("supported provider pagination", "supports pagination"),
            ("unsupported-pagination", "unsupported pagination"),
            ("provider-limit", "provider limits"),
            ("completeness gate",),
            ("required german/english tool/model architecture family",),
        )
        assert_sentence_concepts(
            policy,
            "README",
            *synthetic_readme_signal,
        )
        assert_sentence_concepts(
            policy,
            "completeness gate",
            ("provider page",),
            ("metadata lead",),
            ("follow-up",),
            ("cross-channel identity pivot",),
            ("architecture family",),
        )


def test_exact_query_progression_is_precision_first_and_provider_aware(
    contracts: dict[str, str],
) -> None:
    """Quoted stages improve precision without turning search syntax into evidence."""

    documented = " ".join(CURATOR_DOC.read_text(encoding="utf-8").split())
    for policy in (contracts["agent"], contracts[SKILLS[0]], documented):
        assert_concepts(
            policy,
            ('"middle high german" parser',),
            ("multiword stage phrase",),
            ("precision-first",),
            ("single-word stage",),
            ("unquoted",),
            (
                "never quote the entire query",
                "entire query is never quoted",
                "do not quote the whole query",
            ),
            ("weak-coverage variant", "remains weakly covered"),
            ("stage abbreviation", "stage-abbreviation"),
            (
                "controlled recall",
                "controlled-recall",
                "controlled stage-abbreviation recall",
            ),
            ("provider-specific",),
            ("uncertain quote semantics", "quote semantics are uncertain"),
            ("untrusted",),
            ("never evidence", "never establish"),
        )

    discover = contracts[SKILLS[0]]
    assert_concepts(
        discover,
        ("exact authored query",),
        ("provider",),
        ("locale",),
        ("retrieval mode",),
        ("request-specific status",),
        ("inspect every returned item",),
    )
    curate = contracts[SKILLS[1]]
    assert_concepts(
        curate,
        ("quotation marks in an authored search query",),
        ("provider syntax only",),
        ("not factual evidence", "nor the resulting match is factual evidence"),
    )
    publish = contracts[SKILLS[3]]
    assert_concepts(
        publish,
        ("exact authored query",),
        ("locale",),
        ("retrieval mode",),
        ("request-specific status",),
        ("item-level unrelated-result samples",),
    )


def test_model_and_persistent_vocabulary_leads_are_bounded_and_untrusted(
    contracts: dict[str, str],
) -> None:
    """Lead generation precedes search but cannot become factual evidence."""

    for name in ("agent", SKILLS[0]):
        policy = contracts[name]
        assert_concepts(
            policy,
            ("before external search", "before any external query"),
            ("model-led elicitation",),
            ("explicitly exclude",),
            ("no new distinct",),
            ("iteration bound", "iteration limit"),
            ("all three trusted inventory categories", "all trusted corpora"),
            ("tagsets",),
            ("boilerplate",),
            ("untrusted leads", "untrusted discovery lead"),
            ("never appear as an evidence source", "never evidence by itself"),
            ("delete temporary content", "delete non-cache temporary content"),
            (VOCABULARY, VOCABULARY.replace("/", "\\")),
            ("expected revision", "expected_revision"),
            ("stale revision",),
            ("coordinator",),
        )
    curate = contracts[SKILLS[1]]
    assert_concepts(
        curate,
        ("model elicitation",),
        (VOCABULARY, VOCABULARY.replace("/", "\\")),
        ("never cite the model",),
        ("canonical or primary public sources",),
    )


def test_single_vocabulary_and_external_cache_contract(
    contracts: dict[str, str],
) -> None:
    """Approve one lead registry and one external cache without weakening safety."""

    documented = " ".join(CURATOR_DOC.read_text(encoding="utf-8").split())
    readme = " ".join(README.read_text(encoding="utf-8").split())
    for policy in (contracts["agent"], contracts[SKILLS[0]], documented):
        assert_concepts(
            policy,
            (VOCABULARY, VOCABULARY.replace("/", "\\")),
            ("terms",),
            ("contexts", "observation contexts"),
            ("classifications", "accepted/rejected classification decisions"),
            ("untrusted",),
            ("never inventory", "never satisfy"),
            ("exactly one configured", "exactly one persistent cache root"),
            ("outside the repository",),
            ("30-day ttl",),
            ("512 mib",),
            ("exactly one selected canonical url", "one canonical url"),
            ("no deep", "do not configure deep"),
            (
                "never schedule extracted links",
                "does not schedule links",
                "follow extracted links",
            ),
        )
    for policy in (contracts["agent"], contracts[SKILLS[0]], documented, readme):
        assert_concepts(
            policy,
            ("cached",),
            ("generated markdown",),
            ("browser profiles",),
            ("sqlite",),
            ("fetched",),
            ("never", "excluded"),
        )
    for policy in (documented, readme):
        assert_concepts(
            policy,
            ("%localappdata%\\histgerm\\crawl4ai\\.crawl4ai",),
            ("xdg_cache_home",),
        )
    assert_concepts(
        contracts["agent"],
        ("additional generic cache", "generic cache/registry/snapshot framework"),
        ("crawl snapshot",),
        ("persistent report",),
    )
    vocabulary_files = sorted((ROOT / "research").glob("*vocabulary*.yaml"))
    assert vocabulary_files == [ROOT / VOCABULARY]


def test_vocabulary_reporting_validation_and_publication_allowlists(
    contracts: dict[str, str],
) -> None:
    """Vocabulary changes retain metrics and enter gates only when changed."""

    metrics = (
        ("vocabulary revision",),
        ("refreshed source", "refreshed and reused source counts"),
        ("reused source", "refreshed and reused source counts"),
        ("new terms",),
        ("reused decisions",),
        ("inactive associations",),
        ("vocabulary access gaps",),
    )
    for name in ("agent", SKILLS[0], SKILLS[3]):
        assert_concepts(contracts[name], *metrics)
    assert_concepts(
        contracts[SKILLS[2]],
        (VOCABULARY, VOCABULARY.replace("/", "\\")),
        ("changed-path allowlist",),
        ("independent",),
    )
    assert_concepts(
        contracts[SKILLS[3]],
        (VOCABULARY, VOCABULARY.replace("/", "\\")),
        ("only when it changed",),
        ("validated",),
        ("coordinator-only",),
        ("independent",),
    )


def test_provider_audit_result_inspection_and_iterative_metrics(
    contracts: dict[str, str],
) -> None:
    """Provider outcomes remain contextual and coverage stays in existing records."""

    discover = contracts[SKILLS[0]]
    assert_concepts(
        discover,
        ("google",),
        ("brave",),
        ("bing",),
        ("independent",),
        ("inspect every returned item",),
        ("item-level rejection reasons",),
        ("provider",),
        ("locale",),
        ("retrieval mode",),
        ("http 429 through bounded_http",),
        ("captcha",),
        ("consent",),
        ("iterative exclusion", "beyond known resources"),
        ("weakly covered",),
        ("new-candidate yield",),
        ("existing `searchqueryrecord`/pass fields", "fields already available"),
        ("no generic metrics framework",),
        ("persistent report",),
    )


@pytest.mark.parametrize("name", (SKILLS[1],))
def test_controlled_browser_is_opt_in_robots_first_and_fail_closed(
    contracts: dict[str, str], name: str
) -> None:
    """Browser fallback preserves request, robots, payload, and access controls."""

    assert_concepts(
        contracts[name],
        ("opt-in",),
        ("bounded http",),
        ("robots.txt", "robots policy"),
        ("every origin", "every encountered origin", "encountered origin's"),
        ("404 or 410",),
        ("fail-closed",),
        ("redirect",),
        ("frame",),
        ("worker",),
        ("subresource",),
        ("mixed",),
        ("tls",),
        ("byte limit", "byte budgets", "aggregate-session byte"),
        ("temporary",),
        ("captcha", "challenge"),
        ("consent",),
        ("paywall",),
        ("websockets",),
        ("webrtc",),
        ("controlled_browser",),
        ("failure stage",),
    )


def test_evidence_uncertainty_and_refresh_contracts(
    contracts: dict[str, str],
) -> None:
    """Legal claims need quotes, unknowns remain unclear, and facts survive refresh."""

    for name in ("agent", SKILLS[0], SKILLS[1], SKILLS[2]):
        policy = contracts[name]
        # Structural enum contract: the four legal permission keys are parsed as
        # backticked tokens; the quote/unclear/legal_conflict semantics are
        # concept checks tolerant of equivalent wording.
        assert set(PERMISSIONS) <= set(re.findall(r"`([a-z_]+)`", policy))
        assert_concepts(
            policy,
            ("quote", "quotation"),
            ("unclear",),
            ("legal_conflict",),
        )

    curate = contracts[SKILLS[1]]
    assert_concepts(
        curate,
        (
            "never guess factual, provenance, availability, access, overlap",
            "never guess",
        ),
        (
            "silence about historical stage coverage means `blocked`",
            "silence about historical stage",
        ),
        ("requires a direct quotation", "direct quotation"),
        ("identical quote, url", "identical quote"),
        ("preserve previously verified facts", "previously verified facts"),
        (
            "mark unavailable or discontinued resources rather than deleting",
            "rather than deleting",
        ),
        (
            "never delete or merge a resource automatically",
            "never delete or merge",
        ),
    )


@pytest.mark.parametrize("name", ("agent", SKILLS[0], SKILLS[1]))
def test_url_payload_auth_baseline_hygiene(
    contracts: dict[str, str],
    name: str,
) -> None:
    """Research surfaces keep low-cost baseline hygiene: public-only fetches,
    bounded payloads, untrusted external data, and no code execution.

    These are inexpensive hygiene concepts, not a security protocol, so they are
    asserted as concepts that accept equivalent wording.
    """

    assert_concepts(
        contracts[name],
        ("untrusted data, never instructions", "untrusted data", "never instructions"),
        ("public `http://` or `https://`", "public http", "https"),
        ("credentials",),
        ("localhost",),
        ("loopback",),
        ("link-local",),
        ("private-network", "private network"),
        ("robots",),
        ("terms",),
        ("authentication",),
        ("rate limit",),
        ("10 mib",),
        ("histgerm.research.fetching",),
        ("missing `content-length`", "missing content-length"),
        ("never generate a helper script", "helper script"),
        ("never download",),
        ("never execute",),
        ("eval",),
        ("exec",),
        ("dynamic imports", "dynamic import"),
    )


def test_agent_reports_bounded_progress(contracts: dict[str, str]) -> None:
    """Progress reporting stays bounded; concept checks tolerate rewording."""

    agent = contracts["agent"]
    assert_concepts(
        agent,
        ("after preflight and sweep selection", "preflight and sweep selection"),
        ("after seed retrieval", "seed retrieval"),
        ("group of at most two channels", "at most two channels"),
        ("candidate-worker batch of at most three", "batch of at most three"),
        ("current ledger revision", "ledger revision"),
        ("never remain silent for more than ten minutes", "ten minutes"),
        ("never dump full evidence", "dump full evidence"),
    )


@pytest.mark.parametrize("name", ("agent", SKILLS[0], SKILLS[1]))
def test_every_request_uses_runtime_ip_pinning(
    contracts: dict[str, str],
    name: str,
) -> None:
    """Every request and redirect resolves once and connects only to its pinned IP."""

    assert_concepts(
        contracts[name],
        (
            "immediately before every external request attempt",
            "before every external request",
        ),
        (
            "`histgerm.research.resolve_request_destination`",
            "resolve_request_destination",
        ),
        ("every redirect",),
        ("mixed public/private", "mixed public/private"),
        ("returned `connect_ip`", "connect_ip"),
        ("returned `hostname`", "hostname"),
        ("http `host`", "http host"),
        ("tls sni",),
        ("certificate-validation hostname", "certificate validation"),
        ("never fall back to hostname resolution", "never fall back"),
        ("make no request", "makes no request"),
    )


def test_validation_and_publication_gates(contracts: dict[str, str]) -> None:
    """Only validated allowlisted work reaches a non-default review branch.

    Retained exact assertions cover true textual contracts: the machine-gate
    command strings, the literal publication branch-name templates, and the
    publication-action prohibitions. Surrounding orchestration wording is
    concept-checked so equivalent phrasing keeps passing.
    """

    agent = contracts["agent"]
    validate = contracts[SKILLS[2]]
    publish = contracts[SKILLS[3]]
    # Retained exact: publication prohibitions the machine workflow must never
    # violate are literal safety contracts.
    assert_phrases(
        agent,
        (
            "Never commit to the default branch",
            "Stop after opening the pull request",
            "Never execute the future MHG tools pilot",
        ),
    )
    assert_concepts(
        agent,
        (
            "`gate-curator` has explicit project owner approval",
            "explicit project owner approval",
        ),
        ("all required checks must run", "required checks must run"),
        ("failure is `failed`, never draft", "never draft"),
        ("including ledger-only progress", "ledger-only progress"),
        ("stage only allowlisted validated paths", "allowlisted validated paths"),
        ("normal non-force push to `origin`", "non-force push"),
    )
    # Retained exact: the validation gate names the exact commands it runs, so
    # the machine gate depends on these command strings verbatim.
    assert_phrases(
        validate,
        (
            "uv run pytest",
            "uv run ruff check .",
            "uv run ruff format --check .",
            "uv run mypy src tests",
            "uv build --no-sources",
            "git diff --check",
        ),
    )
    # Retained exact: publication branch-name templates and gate prohibitions are
    # literal format/action contracts consumed by publication tooling.
    assert_phrases(
        publish,
        (
            "copilot/inventory-<category>-<stage>-<run-id>",
            "`copilot/inventory-refresh-<run-id>`",
            "Never publish from the default branch",
            "Open `ready` only when every required validation passes",
            "Open `draft` only when",
        ),
    )
    assert_concepts(
        publish,
        ("stage only the explicit changed paths", "explicit changed paths"),
        (
            "ledger-only or vocabulary-only progress",
            "ledger-only or vocabulary-only",
        ),
        ("human review and merge are mandatory", "human review and merge"),
        ("never execute the future mhg tools pilot", "future mhg tools pilot"),
    )


def test_pr_report_and_no_persistent_report_contract(
    contracts: dict[str, str],
) -> None:
    """The PR body is the complete report and contains the required evidence."""

    policy = contracts[SKILLS[3]]
    assert_concepts(
        policy,
        ("category, stage, and search brief", "category, stage"),
        (
            "passes completed and the exact completion state",
            "passes completed",
        ),
        ("resources added",),
        ("existing resources refreshed", "resources refreshed"),
        (
            "duplicate, out-of-scope, and blocked dispositions",
            "duplicate, out-of-scope",
        ),
        ("source urls and the material supporting excerpts", "supporting excerpts"),
        ("exact evidence gaps", "evidence gaps"),
        ("legal and availability changes", "legal and availability"),
        ("schema or enum changes", "schema or enum"),
        ("risk flags and a high-risk explanation", "risk flags"),
        ("every validation command result", "validation command result"),
        (
            "no third-party payload was retrieved or committed",
            "no third-party payload",
        ),
        (
            "there is no persistent per-run report file",
            "no persistent per-run report",
        ),
        ("remove it immediately", "removed immediately"),
    )


def test_publication_reports_discovery_coverage_and_transport(
    contracts: dict[str, str],
) -> None:
    """The existing PR report carries metrics, retrieval truth, and pilot boundary."""

    publish = contracts[SKILLS[3]]
    assert_concepts(
        publish,
        ("focused queries attempted/completed",),
        ("new-candidate yield",),
        ("providers attempted",),
        ("item-level unrelated-result samples",),
        ("model-elicited lead counts",),
        ("untrusted leads rather than evidence",),
        ("bounded http versus controlled-browser",),
        ("exact failure stages",),
        ("browser binaries",),
        ("future mhg tools pilot",),
        ("never execute",),
    )


def test_playwright_packaging_boundary_is_library_safe(
    contracts: dict[str, str],
) -> None:
    """Playwright remains scoped research tooling and distributions exclude state."""

    validate = contracts[SKILLS[2]]
    assert_concepts(
        validate,
        ("research/development dependency",),
        ("deterministic local/cloud curator setup",),
        (
            "not a distributable `histgerm` runtime dependency",
            "neither is a distributable `histgerm` runtime dependency",
        ),
        ("browser executable",),
        ("browser cache",),
        ("fetched/rendered page", "fetched/rendered or cached page"),
        ("source distribution",),
        ("recorded synthetic fixtures",),
        ("do not launch a live browser",),
    )
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime = " ".join(project["project"].get("dependencies", ())).casefold()
    assert "playwright" not in runtime


def test_required_vocabulary_and_crawl4ai_source_exclusions_are_declared() -> None:
    """Packaging configuration must exclude all approved research state."""

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    exclusions = {
        entry.casefold()
        for entry in project["tool"]["uv"]["build-backend"]["source-exclude"]
    }
    assert VOCABULARY in exclusions
    for required in (
        "**/.crawl4ai/**",
        "**/crawl4ai-cache/**",
        "**/*.sqlite",
        "**/*.sqlite3",
        "**/generated-markdown/**",
    ):
        assert required in exclusions
    assert exclusions & {
        "**/.config/**",
        "**/.playwright/**",
        "**/browser-cache/**",
        "**/browser-profiles/**",
    }
    assert exclusions & {
        "**/browser-pages/**",
        "**/fetched-pages/**",
        "**/generated-pages/**",
    }


def test_contract_tests_cannot_open_real_pull_requests() -> None:
    """Contract tests are static and cannot execute network or GitHub clients."""

    forbidden_imports = {"subprocess", "requests", "httpx", "urllib"}
    for path in (ROOT / "tests" / "agent").glob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = {name for node in ast.walk(tree) for name in _import_roots(node)}
        assert imported.isdisjoint(forbidden_imports), path


def _import_roots(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name.partition(".")[0] for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return ((node.module or "").partition(".")[0],)
    return ()
