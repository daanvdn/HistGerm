"""Static contracts for the inventory curator agent and its four skills."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
AGENT = ROOT / ".github" / "agents" / "histgerm-inventory-curator.agent.md"
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
            "Never execute the MHG-corpora pilot",
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
            "Ledger-only progress",
            "Human review and merge are mandatory",
            "never execute pilot work",
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
