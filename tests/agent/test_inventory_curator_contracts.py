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
    """Workers return validated JSON while coordinators own all mutations."""

    agent = contracts["agent"]
    discover = contracts[SKILLS[0]]
    curate = contracts[SKILLS[1]]
    validate = contracts[SKILLS[2]]
    publish = contracts[SKILLS[3]]
    assert_phrases(
        agent,
        (
            "Workers are read-only",
            "Give each worker exactly one candidate",
            "same worker for one correction attempt",
            "After a second invalid response",
            "Only the coordinator may write trusted resource YAML",
        ),
    )
    assert_phrases(
        discover,
        (
            "Candidate research workers are strictly read-only",
            "Only the coordinator may mutate the ledger",
            "must not write trusted resource YAML",
            "do not duplicate selection, schema, counter, completion",
        ),
    )
    assert_phrases(
        curate,
        (
            "Research exactly one supplied candidate",
            "do not change the ledger, resource YAML, Git state",
            "Return only one JSON object",
        ),
    )
    assert "never edits source, test, ledger, inventory" in validate
    assert "coordinator-only write operation" in publish


def test_lossless_discovery_curation_apply_handoff(
    contracts: dict[str, str],
) -> None:
    """Discovery keeps its schema while the coordinator retains worker evidence."""

    agent = contracts["agent"]
    discover = contracts[SKILLS[0]]
    curate = contracts[SKILLS[1]]
    assert '{"candidate_entries":[],"search_passes":[],"ledger_revision":0}' in discover
    assert_phrases(
        discover,
        (
            "complete and exact discovery output contract",
            "does not invoke candidate research workers",
            "custom-agent coordinator dispatches every eligible",
            "returned candidate",
            "applies the validated `CandidateResearchResult`",
            "retains it for trusted YAML and review",
        ),
    )
    assert_phrases(
        agent,
        (
            "Treat the discovery/curation handoff as lossless",
            "Retain each raw response",
            "Apply that exact result",
            "Never reconstruct either from the resulting `CandidateEntry`",
            "mandatory for refresh results",
        ),
    )
    assert_phrases(
        curate,
        (
            "Return the full validated result",
            "apply this same object with `apply-result`",
            "retain its `evidence` and `proposed_record`",
            "Refresh mode has the identical result and retention contract",
        ),
    )


def test_discovery_is_bilingual_complete_and_refreshes_matches(
    contracts: dict[str, str],
) -> None:
    """Every language/channel is covered and existing matches enter refresh."""

    policy = contracts[SKILLS[0]]
    assert_phrases(
        policy,
        (
            "Althochdeutsch",
            "Old High German",
            "Mittelhochdeutsch",
            "Middle High German",
            "Frühneuhochdeutsch",
            "Early New High German",
            "Korpus",
            "corpus",
            "Wörterbuch",
            "dictionary",
            "German-language web search",
            "English-language web search",
            "CLARIN",
            "OLAC",
            "Zenodo",
            "institutional catalogs",
            "GitHub repository search",
            "Hugging Face",
            "explicit policy reason",
            "completed: false",
            "semantically unrelated",
            "must not be reported as zero candidates",
            "selected sweep remains incomplete",
            "Never silently present that handoff",
            "two consecutive complete passes with no new candidates",
            "An incomplete pass never advances",
            "no pass may end with a pending candidate",
            "sent immediately through curation in refresh mode",
        ),
    )
    for disposition in ("`added`", "`duplicate`", "`out_of_scope`", "`blocked`"):
        assert disposition in policy


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
        ("language model", "sprachmodell"),
        ("pipeline", "sprachverarbeitung"),
        ("stts",),
        ("hits",),
        ("corpus and dictionary", "corpus and dictionary terms"),
    )
    assert "German and English queries" in discover


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
        assert set(PERMISSIONS) <= set(re.findall(r"`([a-z_]+)`", policy))
        assert "quote" in policy
        assert "unclear" in policy
        assert "legal_conflict" in policy

    curate = contracts[SKILLS[1]]
    assert_phrases(
        curate,
        (
            "Never guess factual, provenance, availability, access, overlap",
            "Silence about historical stage coverage means `blocked`",
            "requires a direct quotation",
            "identical quote, URL",
            "Preserve previously verified facts",
            "mark unavailable or discontinued resources rather than deleting",
            "Never delete or merge a resource automatically",
        ),
    )


@pytest.mark.parametrize("name", ("agent", SKILLS[0], SKILLS[1]))
def test_url_payload_auth_terms_and_rate_safety(
    contracts: dict[str, str],
    name: str,
) -> None:
    """Research surfaces refuse private access, payloads, and hostile instructions."""

    assert_phrases(
        contracts[name],
        (
            "untrusted data, never instructions",
            "public `http://` or `https://`",
            "credentials",
            "localhost",
            "loopback",
            "link-local",
            "private-network",
            "robots",
            "terms",
            "authentication",
            "rate limit",
            "10 MiB",
            "histgerm.research.fetching",
            "missing `Content-Length`",
            "Never generate a helper script",
            "Never download",
            "Never execute",
            "eval",
            "exec",
            "dynamic imports",
        ),
    )


def test_agent_reports_bounded_progress(contracts: dict[str, str]) -> None:
    agent = contracts["agent"]
    assert_phrases(
        agent,
        (
            "after preflight and sweep selection",
            "after seed retrieval",
            "group of at most two channels",
            "candidate-worker batch of at most three",
            "current ledger revision",
            "never remain silent for more than ten minutes",
            "never dump full evidence",
        ),
    )


@pytest.mark.parametrize("name", ("agent", SKILLS[0], SKILLS[1]))
def test_every_request_uses_runtime_ip_pinning(
    contracts: dict[str, str],
    name: str,
) -> None:
    """Every request and redirect resolves once and connects only to its pinned IP."""

    assert_phrases(
        contracts[name],
        (
            "Immediately before every external request attempt",
            "`histgerm.research.resolve_request_destination`",
            "every redirect",
            "mixed public/private",
            "returned `connect_ip`",
            "returned `hostname`",
            "HTTP `Host`",
            "TLS SNI",
            "certificate-validation hostname",
            "never fall back to hostname resolution",
            "make no request",
        ),
    )


def test_validation_and_publication_gates(contracts: dict[str, str]) -> None:
    """Only validated allowlisted work reaches a non-default review branch."""

    agent = contracts["agent"]
    validate = contracts[SKILLS[2]]
    publish = contracts[SKILLS[3]]
    assert_phrases(
        agent,
        (
            "`GATE-CURATOR` has explicit project owner approval",
            "Never execute the future MHG tools pilot",
            "All required checks must run",
            "failure is `failed`, never draft",
            "including ledger-only progress",
            "Stage only allowlisted validated paths",
            "Never commit to the default branch",
            "normal non-force push to `origin`",
            "Stop after opening the pull request",
        ),
    )
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
    assert_phrases(
        publish,
        (
            "copilot/inventory-<category>-<stage>-<run-id>",
            "`copilot/inventory-refresh-<run-id>`",
            "Never publish from the default branch",
            "Stage only the explicit changed paths",
            "Open `ready` only when every required validation passes",
            "Open `draft` only when",
            "Ledger-only or vocabulary-only progress",
            "Human review and merge are mandatory",
            "never execute the future MHG tools pilot",
        ),
    )


def test_pr_report_and_no_persistent_report_contract(
    contracts: dict[str, str],
) -> None:
    """The PR body is the complete report and contains the required evidence."""

    policy = contracts[SKILLS[3]]
    assert_phrases(
        policy,
        (
            "category, stage, and search brief",
            "passes completed and the exact completion state",
            "resources added",
            "existing resources refreshed",
            "duplicate, out-of-scope, and blocked dispositions",
            "source URLs and the material supporting excerpts",
            "exact evidence gaps",
            "legal and availability changes",
            "schema or enum changes",
            "risk flags and a high-risk explanation",
            "every validation command result",
            "no third-party payload was retrieved or committed",
            "There is no persistent per-run report file",
            "remove it immediately",
        ),
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
