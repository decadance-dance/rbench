# Spec Delta

## Purpose

Lets a benchmark run spend a planned share of the operator's subscription limits, in rounds with
their own budgets, and stop before a limit runs out. It checkpoints every trial, stops cleanly when a
budget, a soft threshold or a limit is reached, and resumes after a stop or a crash without losing or
double-counting work.

## ADDED Requirements

### Requirement: Frozen plan
A run SHALL freeze its plan when it starts:
- the configurations;
- the task set's version and the content hash of every planned task;
- engine versions, CLI versions and image digests.

A run's plan SHALL NOT change. Changing the matrix SHALL require a new run.

#### Scenario: Matrix edited mid-run
- **WHEN** the operator edits the matrix file and resumes the run
- **THEN** the run continues with its frozen plan and warns that the file changed

### Requirement: Trial states are checkpointed
Every trial SHALL have one recorded state: `pending`, `running`, `done`, `failed`, `interrupted`,
`contaminated` or `misconfigured`. State changes SHALL be written atomically. A trial SHALL be `done` only once its
findings and usage are both stored.

#### Scenario: Crash while writing
- **WHEN** the runner is killed while storing a trial's results
- **THEN** the trial is not `done`, and no partial result file is read as complete

### Requirement: Balanced queue order
Trials SHALL be queued by attempt first, then by task, then by configuration. Configurations that
use the same set of providers SHALL never differ by more than one completed attempt. Configurations
with different sets of providers, such as Claude-only and Claude-with-Codex, are balanced
separately.

#### Scenario: Run stops early
- **WHEN** a run stops after 40% of its trials
- **THEN** every configuration in a group with the same set of providers has completed the same
  number of attempts, give or take one

### Requirement: Round budget
A run SHALL execute one round of the matrix, with that round's token budget per provider. The runner
SHALL NOT start a trial whose token estimate does not fit the round's remaining budget for a
provider it uses. When nothing more fits, the runner SHALL let running trials finish, then exit with
code 75 and print the exact resume command.

#### Scenario: Budget nearly spent
- **WHEN** the next trial's estimate exceeds the round's remaining Codex budget
- **THEN** no further Codex trial starts, and the runner exits with code 75 once running trials end

### Requirement: Soft threshold of the subscription window
When a CLI reports the share of its subscription window already used, the runner SHALL start no new
trial on that provider once the share reaches the configured soft threshold, and SHALL stop as it
does when the budget is spent. When a CLI cannot report that share, the plan and the report SHALL
say the threshold is not enforced for that provider.

#### Scenario: Soft threshold reached
- **WHEN** Codex reports that 60% of its window is used and the soft threshold is 60%
- **THEN** no new Codex trial starts

#### Scenario: Share not reported
- **WHEN** the plan includes a provider whose CLI does not report its used share
- **THEN** the plan says the soft threshold is not enforced for that provider

### Requirement: Limit detection and stop
When a CLI reports a rate limit or a usage limit, the trial SHALL become `interrupted` and keep its
token usage as wasted tokens. By default the runner SHALL then start no new trial, let running
trials finish, and exit with code 75, printing the exact resume command.

#### Scenario: Claude limit runs out
- **WHEN** a Claude process reports that the usage limit is reached in a run without `--wait`
- **THEN** the trial becomes `interrupted`, and the runner exits with code 75 and the resume command
  once running trials end

### Requirement: Waiting for the limit to reset
With `--wait`, a limit SHALL pause only the provider that reported it:
- trials that need only other providers SHALL continue;
- the runner SHALL probe the paused provider with a minimal call at the reported reset time, or
  otherwise with exponential backoff at most one hour apart;
- the runner SHALL resume the provider when a probe succeeds.

#### Scenario: Reset time known
- **WHEN** a run started with `--wait` hits a limit whose message gives a reset time
- **THEN** Codex-only trials keep running, and the runner probes Claude at that time plus a margin
  and resumes it on success

### Requirement: Resume
Resuming a run SHALL:
- take the run's lock, refusing if another runner holds it;
- move trials left `running` to `interrupted`;
- continue with `pending` and `interrupted` trials.

An interrupted trial SHALL rerun from the start.

#### Scenario: Second runner
- **WHEN** a runner tries to resume a run that another live runner holds
- **THEN** it refuses and names the holder

#### Scenario: Resume after crash
- **WHEN** the machine restarted during a run and the operator resumes it
- **THEN** trials that were running start again from the beginning, and done trials are not rerun

### Requirement: Failures and retries
- An engine error, timeout or invalid output SHALL make the trial `failed`. So SHALL an engine's
  exit that says the PR is not eligible for review. A failed trial counts as a result, and SHALL
  rerun only when the operator asks for it.
- An infrastructure error SHALL be retried up to 3 times, after which the runner SHALL stop with the
  error. Infrastructure errors are container, image or proxy failures.

#### Scenario: Engine times out
- **WHEN** an engine exceeds its time limit
- **THEN** the trial is `failed` with reason `timeout` and is not retried on resume

#### Scenario: PR not eligible
- **WHEN** an engine exits saying the PR is closed, a draft or already reviewed
- **THEN** the trial is `failed` with that reason, not `done` with zero findings

### Requirement: Misconfigured configurations
A `misconfigured` trial SHALL be excluded from scores. The runner SHALL then block its configuration
for the rest of the run: no further trial of that configuration starts, including on resume, and the
reason is recorded. Other configurations SHALL continue.

#### Scenario: Wrong model in the first attempt
- **WHEN** the first trial of a configuration becomes `misconfigured`
- **THEN** none of that configuration's remaining trials start, other configurations keep running,
  and the configuration is listed as blocked with the requested and applied models

### Requirement: Concurrency per provider
The runner SHALL limit concurrent trials per provider:
- Claude: a configurable number, default 2;
- Codex: one per login.

#### Scenario: Two Codex trials ready
- **WHEN** two trials that use the same Codex login are ready
- **THEN** they run one after the other

### Requirement: Reuse of earlier trials
When the operator names an earlier run to reuse, the new run SHALL take its done trials, but only
those where all of these match:
- the task's content hash;
- the configuration hash;
- the engine version;
- the CLI versions;
- the image digest.

#### Scenario: Engine version differs
- **WHEN** an earlier trial ran a different rreview ref
- **THEN** it is not reused

#### Scenario: Task changed between set versions
- **WHEN** the new run uses a set version that changed one task's ground truth
- **THEN** trials of that task are not reused, and trials of unchanged tasks are
