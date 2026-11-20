# Proposal

## Why

There is no trustworthy way to choose between AI code-review engines (rreview, review-council, Claude
Code's `/code-review`, Codex's `/review`, ponytail-review) or between models and effort levels
inside them. A first 5-PR comparison had three problems:

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
  - every task set is versioned, with a content hash per task, and every task's source is kept as
    an archive, so a task never depends on its source repository after import. Public tasks come only from
    repositories whose license allows redistribution.
- **Engine adapters** for rreview, review-council, `/code-review`, Codex's `/review`,
  ponytail-review on both CLIs, and a `plain`
  baseline that runs every model on equal terms: one prompt, one role, the same tools and
  structured output through either CLI. Engines, CLIs and ReviewBench are pinned to exact versions.
- **A configuration matrix** with per-role model and effort settings, run in three stages: smoke,
  screening and full.
- **Isolated, hermetic trials:**
  - one container per trial;
  - network only through an allowlist proxy;
  - a scrubbed two-commit repository;
  - server-side web tools disabled;
  - clean CLI configuration, the same for every engine;
  - canaries and a post-trial audit.
- **Rounds with budgets and a run ledger.** The matrix runs in rounds by importance, each with a
  token budget per provider, and the runner stops at a soft threshold of the subscription window,
  so a run should never use up a limit. The ledger checkpoints every trial; a stop at a budget, a
  threshold or a limit exits with the resume command, and a run resumes after a stop or a crash.
- **Scoring:**
  - recall by severity, grounded and augmented precision, false findings;
  - tokens per model, including cache;
  - wall time;
  - the spread of recall across attempts and the failure rate;
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
- `run-ledger`: checkpointed trial state, queue order, round budgets, the soft threshold, limit
  stops, resume, retries and reuse of earlier results.
- `review-scoring`: matching findings to ground truth with a pinned judge, and the per-trial and
  per-configuration metrics with their uncertainty.
- `benchmark-report`: the HTML report, its views, and what a published report may show about
  hidden tasks.

### Modified Capabilities

None. The repository is new.

## Impact

- **New repository** `decadance-dance/rbench`: Python CLI, Docker images, task data, matrices and
  published results. Source archives of public tasks are its release assets.
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
