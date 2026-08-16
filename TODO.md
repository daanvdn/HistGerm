# Autonomous curator architecture migration

Launch this once to execute the corrected migration plan at
`plans/histgerm-curator-architecture-migration.md`.

- [x] Feed this launch entry to GitHub Copilot:

> Execute `plans/histgerm-curator-architecture-migration.md` autonomously.
> On startup, resume from `migration-state.json`: if it records an in-progress
> run, verify the recorded branch and HEAD and continue from `current_task`
> rather than restarting at `TASK-MIG-001`; otherwise create one integration
> branch `copilot/histgerm-curator-migration-<run-id>`, bootstrap
> `migration-state.json`, and open one continuously updated draft migration
> pull request. Run `TASK-MIG-001` through `TASK-MIG-013` strictly in order with
> no parallelism: one branch, one draft PR, and one conventional commit per task,
> updating `migration-state.json` after each task. Evaluate every gate solely
> through its machine-verifiable criteria and proceed automatically without
> pausing for approval or sign-off. Do not mutate live ledger or vocabulary
> content before `GATE-PILOT`. After `GATE-PILOT`, select exactly one incomplete
> pilot target with the checked-in next-sweep selection command, record it once
> in `migration-state.json`, run the pilot on a unique inventory branch, and open
> a separate inventory pull request. Never merge or auto-merge either pull
> request. Stop only on a failed machine gate, a rollback trigger, or an
> unresolved contradiction that prevents a correct implementation.

# HistGerm collection runs

Run these tasks **sequentially in the listed order** with the
`histgerm-inventory-curator` agent. Each task is one independent category/stage
sweep and may update the shared discovery ledger.

Before starting each task:

- [ ] The previous task's pull request is reviewed and merged, or explicitly
      closed without changes.
- [ ] The local checkout is clean, on the default branch, and synchronized
      exactly with `origin`.
- [ ] GitHub authentication, public-web access, and the repository's locked
      research dependencies are available.

For every task, let the curator use the repository's discovery, curation,
validation, and publication skills. Do not manually narrow its bilingual,
multi-channel search protocol. Review and merge the resulting pull request
manually; the agent must not merge or enable auto-merge.

## Old High German (OHG)

### 1. Collect OHG corpora

- [ ] Feed this task to the curator:

> This assignment is explicit project-owner approval for `GATE-CURATOR`.
> Perform the first complete `corpus` / `ohg` inventory sweep. Use the
> repository's full bilingual and multi-channel discovery protocol, research
> and disposition every candidate, update the discovery ledger and trusted
> inventory as warranted by evidence, run the required validation, and publish
> the result on a unique non-default branch in a pull request. Do not rank
> resources, retrieve third-party payloads, merge the pull request, or work on
> any other category/stage sweep. Stop after reporting the pull request and any
> explicit unresolved gaps.

### 2. Collect OHG tools

- [ ] Feed this task to the curator:

> This assignment is explicit project-owner approval for `GATE-CURATOR`.
> Perform the first complete `tool` / `ohg` inventory sweep. Use the
> repository's full bilingual and multi-channel discovery protocol, including
> every required tool, model, architecture, pipeline, tagset, and standard
> query family. Research and disposition every candidate, update the discovery
> ledger and trusted inventory as warranted by evidence, run the required
> validation, and publish the result on a unique non-default branch in a pull
> request. Do not rank resources, retrieve third-party payloads, merge the pull
> request, or work on any other category/stage sweep. Stop after reporting the
> pull request and any explicit unresolved gaps.

# Search provider improvements

- [x] Add `www.laudatio-repository.org` as a search provider, including provider
      integration, safe bounded retrieval, result normalization, and tests.

### 3. Collect OHG dictionaries

- [ ] Feed this task to the curator:

> This assignment is explicit project-owner approval for `GATE-CURATOR`.
> Perform the first complete `dictionary` / `ohg` inventory sweep. Use the
> repository's full bilingual and multi-channel discovery protocol, research
> and disposition every candidate, update the discovery ledger and trusted
> inventory as warranted by evidence, run the required validation, and publish
> the result on a unique non-default branch in a pull request. Do not rank
> resources, retrieve third-party payloads, merge the pull request, or work on
> any other category/stage sweep. Stop after reporting the pull request and any
> explicit unresolved gaps.

## Early New High German (ENHG)

### 4. Collect ENHG corpora

- [ ] Feed this task to the curator:

> This assignment is explicit project-owner approval for `GATE-CURATOR`.
> Perform the first complete `corpus` / `enhg` inventory sweep. Use the
> repository's full bilingual and multi-channel discovery protocol, research
> and disposition every candidate, update the discovery ledger and trusted
> inventory as warranted by evidence, run the required validation, and publish
> the result on a unique non-default branch in a pull request. Do not rank
> resources, retrieve third-party payloads, merge the pull request, or work on
> any other category/stage sweep. Stop after reporting the pull request and any
> explicit unresolved gaps.

### 5. Collect ENHG tools

- [ ] Feed this task to the curator:

> This assignment is explicit project-owner approval for `GATE-CURATOR`.
> Perform the first complete `tool` / `enhg` inventory sweep. Use the
> repository's full bilingual and multi-channel discovery protocol, including
> every required tool, model, architecture, pipeline, tagset, and standard
> query family. Research and disposition every candidate, update the discovery
> ledger and trusted inventory as warranted by evidence, run the required
> validation, and publish the result on a unique non-default branch in a pull
> request. Do not rank resources, retrieve third-party payloads, merge the pull
> request, or work on any other category/stage sweep. Stop after reporting the
> pull request and any explicit unresolved gaps.

### 6. Collect ENHG dictionaries

- [ ] Feed this task to the curator:

> This assignment is explicit project-owner approval for `GATE-CURATOR`.
> Perform the first complete `dictionary` / `enhg` inventory sweep. Use the
> repository's full bilingual and multi-channel discovery protocol, research
> and disposition every candidate, update the discovery ledger and trusted
> inventory as warranted by evidence, run the required validation, and publish
> the result on a unique non-default branch in a pull request. Do not rank
> resources, retrieve third-party payloads, merge the pull request, or work on
> any other category/stage sweep. Stop after reporting the pull request and any
> explicit unresolved gaps.

## Middle High German (MHG)

### 7. Collect MHG tools (live pilot)

- [x] Feed this task to the curator:

> This assignment is explicit project-owner approval for both `GATE-CURATOR`
> and the separate live MHG tools pilot. Perform the first complete `tool` /
> `mhg` inventory sweep. Use the repository's full bilingual and multi-channel
> discovery protocol, including every required tool, model, architecture,
> pipeline, tagset, and standard query family. Research and disposition every
> candidate, update the discovery ledger and trusted inventory as warranted by
> evidence, run the required validation, and publish the result on a unique
> non-default branch in a pull request. Do not treat setup or implementation
> validation as completion of the pilot. Do not rank resources, retrieve
> third-party payloads, merge the pull request, or work on any other
> category/stage sweep. Stop after reporting the pull request and any explicit
> unresolved gaps.

### 8. Collect MHG dictionaries

- [ ] Feed this task to the curator:

> This assignment is explicit project-owner approval for `GATE-CURATOR`.
> Perform the first complete `dictionary` / `mhg` inventory sweep. Use the
> repository's full bilingual and multi-channel discovery protocol, research
> and disposition every candidate, update the discovery ledger and trusted
> inventory as warranted by evidence, run the required validation, and publish
> the result on a unique non-default branch in a pull request. Do not rank
> resources, retrieve third-party payloads, merge the pull request, or work on
> any other category/stage sweep. Stop after reporting the pull request and any
> explicit unresolved gaps.