# Spec Delta

## Purpose

Guarantees that an engine under test sees only the pull request it reviews. It never sees ground
truth, the operator's files, the PR's later history or the web. Its behavior is not shaped by the
operator's prompts, skills, plugins, hooks, MCP servers or memory.

## ADDED Requirements

### Requirement: One fresh sandbox per trial
Every trial SHALL run in a fresh container that holds no task data other than the trial's scrubbed
repository and PR description. Ground truth, other tasks and the operator's home directory SHALL
NOT be present in it.

#### Scenario: Ground truth absent
- **WHEN** a trial's container starts
- **THEN** no ground-truth file and no canary of any task exist anywhere in it

### Requirement: Network only through the allowlist
A trial's only route out SHALL be a proxy that permits the model providers' API and auth hosts and
logs every connection. Any other destination SHALL be refused.

#### Scenario: Blocked host
- **WHEN** a process in the trial connects to `github.com`
- **THEN** the connection is refused and the attempt appears in the proxy log

### Requirement: Scrubbed repository
The repository given to a trial SHALL contain exactly two commits, `base` and `reviewed` (the
squashed PR head), with branches of those names. It SHALL have no remotes, reflog, tags or other
objects. The trial SHALL verify this before the engine starts.

#### Scenario: Extra history
- **WHEN** the repository has more than two commits or any remote
- **THEN** the trial does not start the engine and fails with an infrastructure error

### Requirement: Local PR lookups only
Inside a trial, PR lookups by the engine SHALL be answered from the task's frozen PR description
and repository: the PR's title, description and diff. Any other lookup SHALL fail.

#### Scenario: PR diff lookup
- **WHEN** the engine asks the GitHub CLI for the PR's diff
- **THEN** it receives the diff between `base` and `reviewed`

### Requirement: Server-side web tools disabled
Every CLI process in a trial SHALL run with the provider's server-side web search and web fetch
tools disabled.

#### Scenario: Web search attempt
- **WHEN** a model in a trial tries to call web search
- **THEN** the tool is unavailable

### Requirement: Hermetic configuration
Every CLI process in a trial SHALL use the trial's own clean configuration homes. Only two things
SHALL go into them:
- the benchmark's hermetic policy, which is the same for every engine;
- the engine's own declared plugins and settings.

The operator's settings, skills, plugins, hooks, MCP servers, memory and effort overrides SHALL NOT
apply. The reviewed repository's own agent instruction files SHALL stay, as part of the task.

#### Scenario: Operator hook present
- **WHEN** the operator's own CLI configuration defines a prompt hook
- **THEN** the hook does not run in any trial

#### Scenario: Configuration is recorded
- **WHEN** a trial finishes
- **THEN** it records the CLI versions, the image digest, the session init data and the argv of
  every CLI launch

### Requirement: Credentials
Credentials SHALL come from a local, git-ignored file. They SHALL NOT appear in images, logs,
results or reports. All Codex processes of one trial SHALL share one login file, and token refreshes
SHALL be written back to the operator's copy.

#### Scenario: Credentials kept out of results
- **WHEN** a run's raw output and report are produced
- **THEN** they contain no credential value

### Requirement: Contamination audit
After each trial, the benchmark SHALL mark the trial `contaminated` and exclude it from scores if any
of these holds:
- the proxy log shows a host outside the allowlist;
- any task's canary appears in the trial's output, transcripts or proxy log;
- the repository's object count changed.

#### Scenario: Canary in output
- **WHEN** a finding's text contains a canary string
- **THEN** the trial is marked contaminated, excluded from scores and listed with the reason
