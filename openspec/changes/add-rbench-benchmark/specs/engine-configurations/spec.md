# Spec Delta

## Purpose

Defines which review engines the benchmark runs, how each engine's output becomes comparable
findings, and how a matrix file expands into the configurations a run executes.

## ADDED Requirements

### Requirement: Engine adapters
The benchmark SHALL support these engines:
- `rreview`, pinned by git ref;
- `review-council`, pinned by plugin version;
- `code-review`, pinned by Claude Code version;
- `ponytail-review`, pinned by plugin version;
- `plain`, one fixed review prompt run through either CLI.

Each engine SHALL declare:
- its roles;
- how a model and an effort reach each role;
- its default configuration.

#### Scenario: Engine default
- **WHEN** a matrix entry selects an engine with `default: true`
- **THEN** the configuration uses the engine's declared default for every role

### Requirement: Findings normalization
Every completed trial SHALL produce findings in one format. Each finding SHALL have a file, a line
range, a message and the engine's own severity label, when the engine gives one. Text the engine
reports as dropped, refuted or praise SHALL NOT become a finding. Any model step used for
normalization SHALL be pinned and versioned, and its tokens SHALL be recorded apart from the engine's.

#### Scenario: Markdown report with a dropped section
- **WHEN** an engine's Markdown report lists two findings and one item under "dropped"
- **THEN** the trial has two findings, and the dropped item is not among them

#### Scenario: Structured output
- **WHEN** an engine returns structured findings
- **THEN** they are converted without a model step

### Requirement: Matrix expansion
A matrix file SHALL expand into configurations as follows:
- A list under `model` or `effort` expands as a cartesian product within its entry, and is never
  crossed with other entries.
- `models` names one set of models for one role and does not expand.
- A role that is not listed keeps the engine's default.
- Models SHALL be given by full id; an alias SHALL be rejected.

#### Scenario: Effort list expands
- **WHEN** an entry lists 4 models and 4 effort levels
- **THEN** it expands to 16 configurations

#### Scenario: Reviewer set does not expand
- **WHEN** an rreview entry gives its reviewers `models: [A, B]`
- **THEN** it expands to one configuration whose reviewers use both A and B

#### Scenario: Alias rejected
- **WHEN** a matrix uses a model alias such as `opus`
- **THEN** planning fails and asks for the full model id

### Requirement: Stable configuration ids
Every configuration SHALL have an id made of a readable name and a hash of its full settings. The
same settings SHALL always give the same id.

#### Scenario: Same settings in two matrices
- **WHEN** two matrix files expand to a configuration with identical settings
- **THEN** both give it the same id

### Requirement: Applied effort is recorded
Every trial SHALL record the effort that actually applied to each role, as reported by the CLI. When
it differs from the requested effort, the trial SHALL be flagged.

#### Scenario: Silent fallback
- **WHEN** a model does not support the requested effort and the CLI uses a lower one
- **THEN** the trial records both levels and the report shows the flag

### Requirement: Plan with estimate
Before running, the benchmark SHALL print the number of trials and a token estimate for the plan.
The estimate SHALL cover engine, normalization and judge tokens, and SHALL be based on measured
medians when a smoke run exists.

#### Scenario: Plan before run
- **WHEN** the operator plans a screening matrix
- **THEN** the trial count and the token estimate per provider are printed, and nothing runs

### Requirement: Stages
The benchmark SHALL provide three stages as matrix files:
- **smoke:** one core task, each engine's default, one attempt;
- **screening:** the core tasks, one attempt;
- **full:** all tasks, five attempts, for configurations the maintainer selects.

#### Scenario: Full stage attempts
- **WHEN** the full stage runs
- **THEN** every selected configuration gets five attempts on every task
