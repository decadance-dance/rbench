# Proposal

## Why

There is no trustworthy way to choose between AI code-review engines (rreview, review-council, Claude
Code's `/code-review`, ponytail-review) or between models and effort levels inside them. A first
5-PR comparison had three problems:

- It was confounded: two engines silently ran a different Codex model than intended.
- Nothing prevented agents from reading the ground truth on the host.
- It could not be repeated.

The rreview Orca panel is waiting on this choice of engine. rreview's own changes also need a
regression check.

## What Changes

- **New public repository `rbench`.** It benchmarks code-review engines on real pull requests
  against ground truth, with Must fix / Should fix / Nit severities.
- **Task sets:**
  - a subset of ReviewBench, an external MIT-licensed set;
  - our own PRs on public repos;
  - a hidden set in a separate private repository. The hidden set is never published; only its
    aggregate scores are.
- **Engine adapters** for rreview, review-council, `/code-review`, ponytail-review, and a `plain`
  baseline that runs every model on equal terms.
- **A configuration matrix** with per-role model and effort settings, run in three stages: smoke,
  screening and full.
- **Isolated, hermetic trials:**
  - one container per trial;
  - network only through an allowlist proxy;
  - a scrubbed two-commit repository;
  - server-side web tools disabled;
  - clean CLI configuration, the same for every engine;
  - canaries and a post-trial audit.
- **A run ledger** that checkpoints every trial. When a subscription limit runs out it pauses, and
  it resumes after a crash or a limit stop.
- **Scoring:**
  - recall by severity, grounded and augmented precision, false findings;
  - tokens per model, including cache;
  - wall time;
  - variance across attempts;
  - paired comparisons for regression checks.
- **A self-contained HTML report** and a publication step that keeps hidden data out of the
  public repository.
- **A dependency in rreview** (tracked there): an opt-in `--sandbox external` flag, so rreview
  can run inside the benchmark's container without its inner sandbox.

## Capabilities

### New Capabilities

- `review-tasks`: the task format, ground truth with severities, producers and canaries, the task
  sets, and the public/hidden split with its publication guard.
- `engine-configurations`: engine adapters, normalization of engine output into findings, the
  configuration matrix and its expansion, and the run stages with their estimates.
- `trial-isolation`: how one trial runs isolated and hermetic, how credentials reach it, and the
  audit that excludes contaminated trials.
- `run-ledger`: checkpointed trial state, queue order, limit detection and pausing, resume,
  retries and reuse of earlier results.
- `review-scoring`: matching findings to ground truth with a pinned judge, and the per-trial and
  per-configuration metrics with their uncertainty.
- `benchmark-report`: the HTML report, its views, and what a published report may show about
  hidden tasks.

### Modified Capabilities

None. The repository is new.

## Impact

- **New repository** `decadance-dance/rbench`: Python CLI, Docker images, task data, matrices and
  published results.
- **New private repository** `decadance-dance/rbench-private` for hidden tasks.
- **rreview:** a new opt-in flag, in its own issue and PR.
- **Runtime dependencies:**
  - Docker;
  - Claude Code and Codex CLIs at pinned versions;
  - the operator's subscriptions: a Claude OAuth token and a ChatGPT login for Codex;
  - ReviewBench's data and matcher at a pinned commit.
- **Out of scope:**
  - a hosted leaderboard;
  - runs on GitHub CI (CI runs only unit tests and the publication guard);
  - API-key mode and dollar costs;
  - measuring the engines' own sandboxes;
  - the rreview Orca panel and the Orca bot.
