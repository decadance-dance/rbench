# Spec Delta

## Purpose

Lets a benchmark run spend a whole subscription limit safely. It checkpoints every trial, pauses when
a provider's limit runs out, and resumes after a limit stop or a crash without losing or
double-counting work.

## ADDED Requirements

### Requirement: Frozen plan
A run SHALL freeze its plan when it starts:
- the configurations;
- the task set and its hash;
- engine versions, CLI versions and image digests.

A run's plan SHALL NOT change. Changing the matrix SHALL require a new run.

#### Scenario: Matrix edited mid-run
- **WHEN** the operator edits the matrix file and resumes the run
- **THEN** the run continues with its frozen plan and warns that the file changed

### Requirement: Trial states are checkpointed
Every trial SHALL have one recorded state: `pending`, `running`, `done`, `failed`, `interrupted` or
`contaminated`. State changes SHALL be written atomically. A trial SHALL be `done` only once its
findings and usage are both stored.

#### Scenario: Crash while writing
- **WHEN** the runner is killed while storing a trial's results
- **THEN** the trial is not `done`, and no partial result file is read as complete

### Requirement: Balanced queue order
Trials SHALL be queued by attempt first, then by task, then by configuration. Configurations on the
same provider SHALL never differ by more than one completed attempt.

#### Scenario: Run stops early
- **WHEN** a run stops after 40% of its trials
- **THEN** every configuration on the same provider has completed the same number of attempts,
  give or take one

### Requirement: Limit detection and pause
When a CLI reports a rate limit or a usage limit:
- the trial SHALL become `interrupted`, keeping its token usage as wasted tokens;
- the provider SHALL be paused;
- trials that need only other providers SHALL continue;
- a trial that needs a paused provider SHALL wait.

#### Scenario: Claude limit runs out
- **WHEN** a Claude process reports that the usage limit is reached
- **THEN** the trial becomes `interrupted`, Claude trials stop being started, and Codex-only trials
  keep running

### Requirement: Waiting for the limit to reset
While a provider is paused, the runner SHALL probe it with a minimal call:
- at the reported reset time, when the CLI gave one;
- otherwise with exponential backoff, at most one hour apart.

The runner SHALL resume when a probe succeeds. With `--no-wait`, it SHALL instead exit with code 75
and print the resume command.

#### Scenario: Reset time known
- **WHEN** the limit message gives a reset time
- **THEN** the runner waits until that time plus a margin, probes, and resumes on success

#### Scenario: No waiting requested
- **WHEN** a limit is hit in a run started with `--no-wait`
- **THEN** the runner exits with code 75 and prints the exact resume command

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
- An engine error, timeout or invalid output SHALL make the trial `failed`. A failed trial counts as
  a result, and SHALL rerun only when the operator asks for it.
- An infrastructure error SHALL be retried up to 3 times, after which the runner SHALL stop with the
  error. Infrastructure errors are container, image or proxy failures.

#### Scenario: Engine times out
- **WHEN** an engine exceeds its time limit
- **THEN** the trial is `failed` with reason `timeout` and is not retried on resume

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
- the task;
- the configuration hash;
- the engine version;
- the CLI versions;
- the image digest.

#### Scenario: Engine version differs
- **WHEN** an earlier trial ran a different rreview ref
- **THEN** it is not reused
