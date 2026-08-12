---
name: publish-histgerm-batch
description: "Independently publish one prepared and validated HistGerm inventory batch on a uniquely named non-default branch, then open its required ready or draft pull request. Use for resource, refresh, schema, mixed, or ledger-only batches; never use for research, validation repair, merge, or auto-merge."
---

# Publish a HistGerm Batch

Accept the environment (`local` or `cloud`), repository root, category, stage,
run ID, search brief, validated batch summary, explicit changed paths, and risk
flags. The summary must include every required validation result and the
candidate, pass, resource, evidence, legal, availability, schema, payload, and
completion facts needed for the pull-request report. Publication is a
coordinator-only write operation.

Return only one JSON object, without Markdown or commentary:

```json
{
  "ok": true,
  "commit_hash": "",
  "pushed_branch": "",
  "pull_request_url": "",
  "state": "ready"
}
```

`state` is exactly `ready` or `draft`. Do not emit success-shaped output until
the commit exists, the named branch is pushed to `origin`, and the pull-request
URL has been verified. On failure, return `ok: false`, the failed operation and
diagnostic, and any commit hash, pushed branch, or pull-request URL that really
exists. Never claim an operation that was skipped.

## Required preflight evidence

Local runs must perform and retain a successful pre-research preflight before
research or repository mutation:

1. `git status --porcelain` was empty;
2. HEAD was attached;
3. an `origin` remote existed;
4. `gh auth status` succeeded for the target repository;
5. `git fetch origin` succeeded without modifying working files;
6. the repository default branch was resolved, and the checked-out base commit
   exactly matched `origin/<default>`;
7. required Python, `uv`, public-web, worker, Git, repository-write, and
   pull-request capabilities were available.

A local dirty worktree, detached HEAD, absent `origin`, failed authentication
or fetch, unresolved default branch, stale base, or missing capability must
stop before research and make no repository change. This publication skill
refuses a local batch that does not carry that preflight evidence.

Cloud runs must prove before a search pass that repository write,
branch-creation, push, and pull-request capabilities are available; required
research and worker tools exist; the platform-created branch is non-default or
a non-default branch can be created; and its base commit is the current
default-branch head. Cloud runs make no default-branch commits. Missing proof
or capability stops before research.

Immediately before staging or committing in either environment, fetch
`origin` again, resolve the current default branch and its remote head, and
verify that the prepared branch started exactly at that commit. Stop as stale
if the default head moved. Verify HEAD is attached, `origin` still identifies
the target repository, GitHub authentication and write/PR capability still
work, and the current branch is not the default branch.

At publication time `git status --porcelain` may contain the prepared batch,
but every changed, deleted, renamed, copied, and untracked path must be in the
explicit changed-path allowlist. Stop on any extra path. An empty batch also
stops.

## Branch identity

For a category/stage batch, the branch name is exactly:

```text
copilot/inventory-<category>-<stage>-<run-id>
```

Use lowercase kebab-case components. A refresh-only or mixed batch may instead
use `copilot/inventory-refresh-<run-id>`. Never publish from the default branch
or commit directly to it. Reject any other branch name.

Before committing, use the remote refs to prove either that the named branch
does not exist on `origin`, or that it belongs to this same run and has the
expected ancestry and commits. Never reuse an unrelated remote branch. Never
force-push, rewrite, reset, or delete a remote branch to make publication
succeed.

## Validation, staging, and payload boundary

Require a complete result from `validate-histgerm-inventory`. The validation
summary must name every required command, preserve its actual result, confirm
that every candidate is dispositioned, make all risks explicit, and confirm
that no third-party payload was retrieved, generated, staged, committed, or
packaged. Never download or inspect corpus text, dictionary content,
annotations, model weights, binaries, archives, database dumps, software
packages, or other third-party payloads during publication.

Open `ready` only when every required validation passes and all schema and
representation decisions are resolved. Open `draft` only when a schema or
representation decision remains unresolved, or the branch intentionally
demonstrates schema work that cannot yet pass required validation. Ordinary
implementation, test, lint, typing, build, inventory, packaging, or Git
failures must be fixed by the proper owner, never relabeled as draft.

Stage only the explicit changed paths, using pathspec-safe Git arguments. Then
compare the staged path set with the allowlist and compare the staged diff with
the validated batch. Stop before committing on a missing, extra, unsafe, or
unvalidated path. Do not stage credentials, tokens, cookies, private URLs,
local payload paths, temporary files, build artifacts, or a persistent run
report.

Create one coherent conventional commit whose subject describes the
user-visible inventory or workflow change and does not mention internal phase
numbers. Record and verify the resulting commit hash. If signing or hooks fail,
stop rather than bypassing repository policy.

Push only with a normal non-force upstream push to `origin`; for example,
`git push --set-upstream origin HEAD`. Do not use `--force`,
`--force-with-lease`, history rewriting, or credential material in commands,
logs, commits, or reports.

## Pull-request report

Open the pull request against the resolved default branch with the pushed
branch as its head. Its description must contain all of:

- category, stage, and search brief;
- passes completed and the exact completion state;
- resources added;
- existing resources refreshed;
- duplicate, out-of-scope, and blocked dispositions;
- source URLs and the material supporting excerpts used in trusted YAML;
- exact evidence gaps;
- legal and availability changes;
- schema or enum changes;
- risk flags and a high-risk explanation;
- every validation command result;
- explicit confirmation that no third-party payload was retrieved or
  committed.

Ledger-only progress is durable reviewed work and must open a pull request even
when no trusted resource YAML changed. State clearly that it is ledger-only.

There is no persistent per-run report file. The pull-request description and
this skill's JSON response are the complete run report. If the GitHub client
requires a body file, create it outside the repository, never stage it, and
remove it immediately after the create attempt.

Use draft creation only for the two allowed draft cases. Do not create or
depend on issues or labels. Never merge, squash, rebase-merge, enable
auto-merge, approve, or otherwise bypass human review. Human review and merge
are mandatory.

## Stop behavior

Stop without further mutation when any preflight, default-head, status,
branch-identity, ancestry, capability, validation, payload, allowlist, staging,
commit, push, or pull-request check fails. A push or pull-request failure is a
terminal publication failure: report the exact operation and any durable
commit or remote branch truthfully, but do not force-push, merge, auto-merge,
delete history, create an issue, or silently retry with weaker rules.

Normal research evidence gaps are not publication errors when they are
evidence-backed `blocked` dispositions and every candidate is dispositioned.
If research advanced, publish that ledger-bearing batch once validation and
all publication rules pass. Stop after opening the pull request for human
review; never execute pilot work or merge as part of this skill.
