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
logs every connection and every refused attempt. Any other destination SHALL be refused.

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
and repository: the PR's title, description and diff. The PR SHALL be reported as open, with no
comments or reviews. A comment the engine posts to the PR SHALL be captured as engine output and
SHALL NOT leave the trial. Any other lookup SHALL fail.

#### Scenario: PR diff lookup
- **WHEN** the engine asks the GitHub CLI for the PR's diff
- **THEN** it receives the diff between `base` and `reviewed`

#### Scenario: Engine posts its review as a PR comment
- **WHEN** the engine runs `gh pr comment` with its review
- **THEN** the comment body is stored as the trial's engine output, and nothing is sent anywhere

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
- the engine's own declared plugins and settings, including its model and effort settings.


The operator's settings, skills, plugins, hooks, MCP servers, memory and effort overrides SHALL NOT
apply. The reviewed repository's own agent instruction files SHALL stay, as part of the task.

#### Scenario: Operator hook present
- **WHEN** the operator's own CLI configuration defines a prompt hook
- **THEN** the hook does not run in any trial

#### Scenario: Configuration is recorded
- **WHEN** a trial finishes
- **THEN** it records the CLI versions, the image digest, the session init data and the argv of
  every CLI launch

### Requirement: Engine model settings reach the CLI
The CLIs SHALL NOT be told to ignore the trial's configuration homes, because some engines set the
model and effort only there.

#### Scenario: Model set only in the Codex config file
- **WHEN** an engine sets the Codex model in the trial's Codex config file and passes no model flag
- **THEN** Codex runs that model, and the trial records it as the applied model

### Requirement: Policy enforced below the shims
The hermetic policy SHALL be set in each CLI's managed configuration inside the image, so that a
process that starts a CLI binary directly, without the benchmark's shims, still gets it. Every CLI
session found in a trial's transcripts SHALL appear in the shim log.

#### Scenario: Real binary started by absolute path
- **WHEN** a process in a trial runs the real `claude` binary by its absolute path
- **THEN** the session still runs under the hermetic policy, and the audit finds a session missing
  from the shim log

### Requirement: Credentials
Credentials SHALL come from the operator's storage outside the repository: on macOS, the Keychain
for the Claude token, and for Codex the benchmark-only login home, a directory. The benchmark SHALL
pass them to the trial when it starts, without the value appearing on a command line, in a file in
the run directory or in logs. They SHALL NOT appear in images (image configuration such as
`config.Env`, or any layer), results or reports.

#### Scenario: Credentials kept out of results
- **WHEN** a run's raw output and report are produced
- **THEN** they contain no credential value

#### Scenario: Claude token passed at trial start
- **WHEN** a trial starts a container that needs the Claude token
- **THEN** the benchmark reads the token from the operator's storage and passes it by environment
  variable name only, so no command line, run-directory file, log or image (configuration or
  layer) holds its value

### Requirement: Benchmark-only Codex login
Codex SHALL use a login made only for the benchmark, in a login home no other tool uses. The
benchmark SHALL mount that credential-only login directory into the trial and Codex SHALL use its
`auth.json` in place; the file SHALL NOT be copied. All Codex processes of one trial SHALL share
that login file, and token refreshes SHALL be written back to that home only, never to the
operator's other Codex logins.

#### Scenario: Token refresh during a trial
- **WHEN** Codex refreshes its token in a trial
- **THEN** the benchmark's login home gets the new token, and the operator's own Codex login is
  unchanged

### Requirement: Contamination audit
After each trial, the benchmark SHALL mark the trial `contaminated` and exclude it from scores if any
of these holds:
- the proxy log shows a successful connection to a host outside the allowlist;
- any task's canary appears in the trial's output or transcripts;
- the repository's object count changed;
- a CLI session in the transcripts is missing from the shim log.

A refused connection attempt SHALL NOT make a trial contaminated; it SHALL be listed in run health.

#### Scenario: Canary in output
- **WHEN** a finding's text contains a canary string
- **THEN** the trial is marked contaminated, excluded from scores and listed with the reason

#### Scenario: Refused connection
- **WHEN** a process in a trial tries to reach `github.com` and the proxy refuses it
- **THEN** the trial is not contaminated, and run health lists the attempt
