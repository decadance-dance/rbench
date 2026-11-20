# Spec Delta

## Purpose

Defines which review engines the benchmark runs, how each engine's output becomes comparable
findings, and how a matrix file expands into the configurations a run executes.

## ADDED Requirements

### Requirement: Engine adapters
The benchmark SHALL support these engines:
- `rreview`, pinned by commit or tag;
- `review-council`, pinned by plugin version;
- `code-review`, Claude Code's built-in review, pinned by Claude Code version;
- `codex-review`, Codex's built-in review, pinned by Codex CLI version;
- `ponytail-review`, pinned by plugin version, on Claude Code and on Codex;
- `plain`, the benchmark's own baseline, pinned by its prompt hash (see the Plain baseline
  requirements).

#### Scenario: Engine default
- **WHEN** a matrix entry selects an engine with `default: true`
- **THEN** the configuration uses the engine's declared default for every role

### Requirement: Engine declarations
Each engine SHALL declare:
- its roles;
- how a model and an effort reach each role: flags the shim adds, or settings in the trial's CLI
  configuration file;
- its default configuration.

#### Scenario: Model reaches a role through the config file
- **WHEN** an engine launches Codex with no model flag and reads the model from Codex's config file
- **THEN** its adapter writes the role's model and effort into the trial's Codex config file

### Requirement: Plain baseline prompt and role
The `plain` engine SHALL run every model on the same review prompt and findings schema, stored in
the repository and identical for both CLIs. Their hashes SHALL be part of the configuration's
settings. It SHALL have exactly one role, `reviewer`, with a model and an effort; the model's
provider picks the CLI: `claude` for Claude models, `codex` for OpenAI models.

#### Scenario: Same prompt through both CLIs
- **WHEN** a matrix runs `plain` with one Claude model and one OpenAI model
- **THEN** both trials receive the same prompt and schema, with the same hashes recorded

#### Scenario: Prompt edited
- **WHEN** the `plain` prompt changes and the same matrix is planned again
- **THEN** every `plain` configuration gets a new id, and earlier `plain` trials are not reused

### Requirement: Plain baseline tools
Through either CLI, `plain` SHALL get the same tools: reading files and running shell commands in
the trial's repository. It SHALL have no file-editing, image, web, goal or subagent tools, and no MCP
servers or plugins. Changes it makes to files SHALL NOT be scored.

#### Scenario: Claude run of plain
- **WHEN** a `plain` trial runs through `claude`
- **THEN** the argv log shows only the file-reading and shell tools enabled

#### Scenario: Codex run of plain
- **WHEN** a `plain` trial runs through `codex`
- **THEN** the argv log shows the benchmark's model catalog without the file-editing tool, and the
  web, image and goal tools disabled

### Requirement: Plain baseline output
`plain` SHALL return its findings as structured output that follows the findings schema, through
the CLI's own structured-output option. No normalization step SHALL run for it, and output that
does not follow the schema SHALL make the trial `failed`.

#### Scenario: Output off schema
- **WHEN** a `plain` trial's final output does not validate against the findings schema
- **THEN** the trial is `failed` and no model step repairs it

### Requirement: Pinned engines and CLIs
Every run SHALL use exact versions of:
- each engine, as listed under Engine adapters;
- the Claude Code and Codex CLIs, installed in the images;
- the images, referenced by digest;
- the normalization model and the version of its prompt.

Planning SHALL reject a floating reference: a branch name, `latest`, a version range or a model
alias.

#### Scenario: Floating engine ref
- **WHEN** a matrix pins rreview to the branch `main` instead of a commit or tag
- **THEN** planning fails and names the engine and the floating ref

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

### Requirement: Applied model and effort are recorded
Every trial SHALL record the model and the effort that actually applied to each CLI session, as
reported by the CLI. When the effort differs from the requested one, the trial SHALL be flagged.
When the model differs, the trial SHALL become `misconfigured` (see run-ledger, Misconfigured
configurations).

#### Scenario: Silent fallback
- **WHEN** a model does not support the requested effort and the CLI uses a lower one
- **THEN** the trial records both levels and the report shows the flag

#### Scenario: Wrong model
- **WHEN** a Codex session runs a different model from the one its role requested
- **THEN** the trial records both models and becomes `misconfigured`

### Requirement: Plan with estimate
Before running, the benchmark SHALL print the number of trials and a token estimate for the plan.
The estimate SHALL cover engine, normalization and judge tokens, and SHALL be based on measured
medians when a smoke run exists.

#### Scenario: Plan before run
- **WHEN** the operator plans a screening matrix
- **THEN** the trial count and the token estimate per provider are printed, and nothing runs

### Requirement: Rounds
A matrix SHALL be split into rounds, in the order of their importance. Each round SHALL list its
configurations and its token budget per provider, and a run executes one round (see run-ledger,
Round budget).

#### Scenario: Plan of one round
- **WHEN** the operator plans a round
- **THEN** the trial count and token estimate of that round alone are printed and compared with its
  budget

### Requirement: Stages
The benchmark SHALL provide three stages as matrix files:
- **smoke:** one core task, each engine's default, one attempt;
- **screening:** the core tasks, one attempt;
- **full:** all tasks, three attempts, for configurations the maintainer selects.

Screening SHALL only select the configurations for the full stage. The full stage SHALL run fresh
attempts and SHALL NOT count screening trials in its scores.

#### Scenario: Full stage attempts
- **WHEN** the full stage runs
- **THEN** every selected configuration gets three attempts on every task

#### Scenario: Screening trials kept out of the full stage
- **WHEN** a configuration selected from screening runs in the full stage
- **THEN** its full-stage scores use only full-stage trials, even where screening ran the same task
