# rbench design

Status: draft for the maintainer's approval (2026-11-20). Nothing is built until this design is approved.

## 1. Purpose

rbench measures AI code-review engines on real pull requests. It has two uses:

1. **Compare engines and configurations.** Which engine, model and effort finds the most real
   problems, with the fewest false ones, for how many tokens and how much time. The result decides
   which engine the rreview Orca panel is built on.
2. **Catch regressions.** Compare two versions of one engine (for example rreview at two git refs) on
   the same tasks and see whether quality or cost moved beyond noise.

The benchmark is public and reproducible by other people with their own subscriptions. A hidden task
set lives in a separate private repository and is never published; only its aggregate scores are.

## 2. Requirements

| # | Requirement | Where it is met |
|---|---|---|
| R1 | No leaks: an agent under test cannot see ground truth, local transcripts, notes, later clones or the PR's later history, on disk or over the network | §6 |
| R2 | Hermetic runs: no influence from the operator's prompts, skills, plugins, hooks, MCP servers or memory | §6.3 |
| R3 | Configuration matrix over models (Fable 5.2, Opus 5.5, Sonnet 5.5, Haiku 5.5, GPT-6.2-Sol, GPT-6.2-Astra, GPT-6.1-Terra), effort (low, medium, high, xhigh) and per-role settings for multi-model engines | §5 |
| R4 | Reproducible by others; the benchmark is public | §9 |
| R5 | Public and hidden data split; hidden data never reaches the public repo | §4.3 |
| R6 | Metrics: Must fix / Should fix / Nit, recall and precision, false findings, tokens per model including cache, wall time, variance | §8 |
| R7 | A readable, usable report | §10 |
| R8 | Budget is the whole subscription limit; a run checkpoints and resumes when a limit runs out mid-run | §7 |

## 3. Approach and what is reused

rbench is a thin layer over existing open-source parts:

- **Harbor** (Apache-2.0) runs agent CLIs in Docker, repeats trials and records trajectories. rbench
  uses it as the trial executor, if the spike in §12 passes.
- **ReviewBench** (MIT) supplies an external task set (the 25-PR test subset with 342 findings, and
  the 219-PR public set for later), its findings JSON format, and its LLM matcher ("same underlying
  issue", many-to-many, grounded and augmented precision and recall).

rbench builds:
- the configuration matrix;
- the run ledger with checkpoints, resume and limit handling;
- the isolation layer: network allowlist proxy, scrubbed repos, canaries and audit;
- the engine adapters;
- the conversion of our own reviewed PRs into tasks;
- the Must/Should/Nit mapping;
- the report.

**Fallback (option B).** If the spike shows that Harbor cannot run behind our proxy, cannot use
subscription auth in its containers, or cannot run one trial at a time under our ledger, rbench
replaces Harbor with its own `docker compose` runner. It keeps the ReviewBench data and matcher
either way.

Implementation language: Python 3.12 with `uv`, matching Harbor. The report is one static HTML file.

## 4. Tasks

### 4.1 Task format

One directory per task:

```
tasks/public/<task-id>/
  task.toml           # source repo URL, PR number, base SHA, head SHA, language, set (reviewbench|ours), tags
  pr.json             # PR title, body, author, base/head refs, as they were at the reviewed head; no review comments
  ground_truth.json   # findings: id, file, line range, message, severity, category, source; plus a canary
```

`ground_truth.json` follows ReviewBench's golden format and adds two fields:

- `severity`: `must`, `should` or `nit`.
- `producer`: which engine and model first produced the item, when known. The report marks results
  where an engine is scored on ground truth it produced itself.
- `canary`: one string per task, of the form `RBENCH-CANARY-<uuid>`. Hidden tasks use
  `RBENCH-HIDDEN-CANARY-<uuid>`.

### 4.2 Sources

| Set | Source | Ground truth | Notes |
|---|---|---|---|
| `reviewbench` | ReviewBench 25-PR test subset (later more) | ReviewBench golden findings; high/medium/low mapped to must/should/nit | Public; high contamination risk; few Rust PRs |
| `ours` | Our PRs on public repos (rreview, rrecall, orca-plane), starting with the 5 calibration PRs | Items the maintainer confirms from the first cross-model review at the reviewed head; each item records which engine and model first produced it | Post-training-cutoff PRs; ground truth is incomplete, so augmented precision matters |
| `hidden` | PRs from private and internal repos, in `rbench-private` | Same conversion | Never published; aggregate scores only |

Severity mapping:

| Must fix | Should fix | Nit |
|---|---|---|
| ReviewBench high, our M | ReviewBench medium, our S | ReviewBench low and style, our N |

ReviewBench counts sub-threshold nits as false positives. rbench reports nits separately instead.

**Core tasks** (about 10) are the screening subset: 5 from the ReviewBench test subset and the
5 calibration PRs. The ReviewBench five are chosen for language mix (Rust, TypeScript, Python, Go,
C#) and diff size. The list is fixed in `tasks/core.txt`.

### 4.3 Public and hidden split

- Hidden tasks live in the private repository `decadance-dance/rbench-private`. They use the same
  format, with task ids prefixed `h-`. The runner takes the path from `--hidden <dir>` or
  `RBENCH_HIDDEN_DIR`.
- Results of hidden tasks are written under `runs/` (git-ignored). `rbench publish` exports only
  aggregates for the hidden set: one row per configuration, with no per-task rows, finding text or
  task ids.
- The public CI guard fails on any of these in the public repo: a path or id matching `h-*`, the
  string `RBENCH-HIDDEN-CANARY-`, or a file under `runs/`.
- A local pre-push hook, installed by `rbench init`, also checks the diff against the real hidden
  task ids and canaries read from the private directory.

## 5. Engines and the configuration matrix

### 5.1 Engines

Each engine is an adapter:

```
engines/<name>/
  engine.toml         # pinned version; image install steps (incl. plugin install and enable);
                      # invocation template; roles; how model and effort reach each role (flag, env,
                      # settings or the engine's own config); the `default` configuration
  normalize.py        # its output -> findings.json
```

| Engine | Pinned as | Roles | Output |
|---|---|---|---|
| `rreview` | git ref (two refs for a regression comparison), built in the image | reviewers (a set of models), coordinator, judge; each has a model and effort | `--json` result, converted directly |
| `review-council` | plugin version (0.14.0 at first) | its own roles, read from its config when the adapter is written and listed in `engine.toml` | Markdown report |
| `code-review` | Claude Code's built-in `/code-review` at the pinned CLI version | single | Markdown report |
| `ponytail-review` | plugin version (5.0.0 at first) | single | Markdown report |
| `plain` | one fixed review prompt in this repo, run with `claude -p` or `codex exec` | single | JSON requested by the prompt |

The `plain` engine is the baseline. It is the only way to rank each of the seven models on equal
terms, because the other engines either fix their CLI or mix models.

**Markdown normalization.** Markdown reports become findings through an extraction step. A pinned
model copies each reported finding with its file, lines, message and the engine's own severity label.
It does not judge anything. Text the engine puts under "dropped", "refuted" or "done well" is not a
finding. Extraction tokens are recorded apart from the engine's tokens. The extraction prompt is
versioned and hashed like the judge prompt.

**rreview dependency.** rreview gets an explicit opt-in flag, `--sandbox external` (default off). It
tells rreview to skip its inner sandbox because an outer container already isolates the run. This is
tracked as its own issue and PR in the rreview repo. The bench measures rreview's review quality;
rreview's own isolation stays covered by rreview's tests on the host.

### 5.2 Configurations

A configuration is an engine plus a model and effort for each of its roles. Configurations are listed
in matrix files:

```yaml
# matrices/screening.yaml
tasks: core
attempts: 1
configs:
  - engine: plain
    cli: claude
    model: [claude-fable-5-2, claude-opus-5-5, claude-sonnet-5-5, claude-haiku-5-5]
    effort: [low, medium, high, xhigh]
  - engine: plain
    cli: codex
    model: [gpt-6.2-sol, gpt-6.2-astra, gpt-6.1-terra]
    effort: [low, medium, high, xhigh]
  - engine: rreview
    ref: develop
    roles:
      reviewers: {models: [claude-opus-5-5, gpt-6.2-sol], effort: high}   # `models`: one set, not expanded
      coordinator: {model: claude-opus-5-5, effort: [medium, high]}       # `model`/`effort` lists expand
      judge: {model: claude-opus-5-5, effort: high}
  - engine: review-council
    default: true
  - engine: code-review
    model: [claude-opus-5-5, claude-fable-5-2]
    effort: high
  - engine: ponytail-review
    model: claude-opus-5-5
    effort: high
```

- A list under `model` or `effort` expands as a cartesian product within one entry. Lists are never
  crossed across entries. `models` (plural) names a set of models for one role and does not expand.
- `default: true` takes the engine's `default` configuration from `engine.toml`. A role not listed
  keeps its default.
- Every expanded configuration gets a stable id: a short readable name plus a hash of its full
  settings.
- Models are always written as full ids, never aliases.
- The effort that actually applied is recorded per trial, because both CLIs fall back silently when a
  model does not support a level.

### 5.3 Stages

1. **Smoke:** 1 core task, the default configuration of each engine, 1 attempt. This checks the
   pipeline and gives the token medians that `rbench plan` uses for its estimate. The estimate
   covers engine, extraction and judge tokens, since all come from the same subscriptions.
2. **Screening:** the core tasks with every configuration in `screening.yaml`, 1 attempt.
3. **Full:** the top configurations from screening, chosen by the maintainer from the Pareto front of
   Must-fix recall against tokens, plus the defaults of every engine. Run on all tasks with 5 attempts.

`rbench plan` prints the number of trials and a token estimate before anything runs.

## 6. Isolation and hermetic runs

### 6.1 One trial

Each trial runs in a fresh `docker compose` project with two containers:

- **Agent container.** It sits on an `internal: true` network, so its only route out is the proxy.
  Its image holds:
  - the pinned CLIs (Claude Code, Codex);
  - the engine at its pinned version;
  - no task data: images are per engine version and CLI versions, not per task. The task's scrubbed
    repo is copied in with `docker cp` when the container is created (never mounted), and its hash
    is recorded;
  - a `gh` stub that answers `pr view` and `pr diff` from `pr.json` and the repo.

  Ground truth is never in this image.
- **Proxy container** (squid). It allows only the model providers' API and auth hosts and logs every
  CONNECT. The exact host list is taken from a logged smoke run and fixed in `images/proxy/allowlist`.

Repo scrub. The repo is built on the host from the source repository:

- two commits, `base` and `reviewed` (the squashed PR head), with branches of the same names;
- no remotes, reflog, tags, packed refs or other objects.

Before the agent starts, the trial checks that `git rev-list --all --count` is 2 and that
`git remote` is empty.

Other leak paths:

- **Server-side web tools.** These run on the provider's side, so the proxy cannot see them. They are
  disabled: Claude's `WebSearch` and `WebFetch`, and Codex's `web_search`.
- **Model memory of public PRs.** Isolation cannot remove this. The report shows the `reviewbench`,
  `ours` and `hidden` sets separately, so contamination can be judged.

### 6.2 Auth

- **Claude:** `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`, passed as an environment variable.
- **Codex:** the login's `auth.json`, bind-mounted read-write as a single file so that token refreshes
  reach the host copy. Refresh tokens rotate, so the runner runs at most one Codex trial at a time per
  login (§7.3).
- **Credentials:** read from `rbench.local.toml`, which is git-ignored. They never enter images,
  logs or results.

### 6.3 Hermetic configuration

Every CLI process inside the container runs with:

- **Clean homes:** one fresh `CLAUDE_CONFIG_DIR` and one `CODEX_HOME` per trial, shared by every
  process of that trial, so all Codex processes use the one mounted `auth.json`. They contain rbench's
  hermetic policy, which is the same for every engine, plus the engine's own plugin install and
  settings from `engine.toml`.
- **Claude settings:**
  - `--strict-mcp-config` and `ENABLE_CLAUDEAI_MCP_SERVERS=false`;
  - `WebSearch` and `WebFetch` denied;
  - `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` and `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`;
  - `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`;
  - `DISABLE_TELEMETRY=1`, `DISABLE_ERROR_REPORTING=1` and `DISABLE_AUTOUPDATER=1`;
  - `CLAUDE_CODE_EFFORT_LEVEL` unset;
  - `enableWeakerNestedSandbox: true` in case an engine starts Claude's sandbox.
- **Codex settings:** `--ignore-user-config --ignore-rules --strict-config` and
  `-c web_search="disabled"`.
- **CLI shims.** Engines launch the CLIs themselves, so rbench puts `claude` and `codex` shims first in
  `PATH`. Each shim adds the flags above and logs the
  argv and session id. The shims exist to enforce one uniform policy; they never change the engine's
  prompt, model or effort.
- **The reviewed repo's own `CLAUDE.md` / `AGENTS.md` stay.** They are part of the task, as they
  would be in real use.
- **Recorded per trial:** CLI versions, the image digest, the `system/init` event, the shim argv log
  and the applied effort.

### 6.4 Audit after each trial

A trial is marked `contaminated` and excluded from scores if any of these holds:

- the proxy log has a host outside the allowlist;
- a canary appears in the engine output, the transcripts or the proxy log;
- the scrubbed repo's object count changed.

The report lists contaminated trials with the reason.

## 7. Run ledger, checkpoints and resume

### 7.1 Layout

```
runs/<run-id>/
  plan.json                         # frozen: matrix expansion, task set hash, engine versions, CLI versions, image digests
  ledger.lock                       # one runner per run
  trials/<task>/<config>/<attempt>/
    status.json                     # pending | running | done | failed | interrupted | contaminated, with timestamps and reason
    findings.json                   # normalized findings
    usage.json                      # tokens per process and per model, wall time
    raw/                            # engine output, transcripts, shim log, proxy log
  judge/<task>/<config>/<attempt>.json
```

- Every write is atomic: write a temporary file, then rename it.
- A trial counts as `done` only after `findings.json` and `usage.json` exist.
- `plan.json` cannot change during a run. Changing the matrix means a new run, which can reuse
  matching done trials (§7.4).

### 7.2 Order

Trials are queued attempt-first, then by task, then by configuration. A run that stops anywhere
leaves the matrix balanced per provider: configurations on the same provider differ by at most one
attempt. While one provider is paused the other runs ahead, so the report shows each configuration's
coverage and compares configurations only on the tasks and attempts both completed.

### 7.3 Limits

- **Detection.** Each adapter recognises a rate-limit or usage-limit stop from its CLI's output:
  - Claude: a result event with a 429 or "usage limit" error, or a reset time;
  - Codex: an error event with "usage limit" or 429.
- **What happens to the trial.** It becomes `interrupted`, which is neither a result nor a failure,
  and its tokens stay in its `status.json`; the report sums them as wasted tokens.
- **What happens to the provider.** It is paused: Anthropic for Claude processes, OpenAI for Codex.
  Trials that need only the other provider keep running. A multi-model configuration waits for both.
- **Resuming.** The runner waits until the parsed reset time plus a margin. If no time was parsed, it
  backs off exponentially, up to 1 hour between probes. A probe is one minimal call. With `--no-wait`,
  the runner exits with code 75 and prints the resume command instead.
- **Concurrency.** Limited per provider: Claude N, from `rbench.local.toml`, default 2; Codex 1 per
  login.
- **Partial trials** are never resumed half-way. A review is one unit, so an interrupted trial reruns
  from the start.

### 7.4 Resume and reuse

- `rbench run --resume <run-id>` takes the lock and moves `running` trials left by a crash to
  `interrupted`. It then continues with `pending` and `interrupted` trials.
- `failed` trials do not rerun automatically; they are results. A failure is an engine error, a
  timeout or invalid output. `--retry-failed` reruns them.
- Infrastructure errors (Docker, image build, proxy start) are retried up to 3 times. Then the
  runner stops with a clear message.
- `rbench run --reuse <old-run-id>` copies done trials whose task, configuration hash, engine version,
  CLI versions and image digest all match.

### 7.5 Judging

- Judging is a separate, resumable phase: `rbench judge <run-id>`.
- Results are cached by the hash of the findings, the ground truth, the judge model and the judge
  prompt.
- Judge trials go through the same limit handling.
- The judge runs in its own hermetic container, on the host side of the boundary. It is the only
  component that reads ground truth.

## 8. Metrics

### 8.1 Matching

- Each trial's findings are matched to ground truth by ReviewBench's matcher: same underlying issue,
  many-to-many, location as context only.
- The matcher runs at a pinned commit, with the pinned judge model and a prompt hash stored with
  every score.
- Unmatched findings are labelled by the augmented classifier as valid-but-missing, false, or nit.

### 8.2 Per trial

- **Recall** by ground-truth severity: Must fix, Should fix and Nit, each grounded.
- **Precision:**
  - grounded: matched findings ÷ reported findings;
  - augmented: matched plus valid-but-missing, ÷ reported findings.
- **False findings:** the count of findings labelled false.
- **Nits reported:** counted on their own, not as false positives.
- **Tokens per model:**
  - input, output, cache read and cache creation, summed over every CLI process;
  - this includes Claude's side calls to Haiku;
  - extraction and judge tokens are kept apart from the engine's.
  - Counts are summed per session from the trial's transcripts: Claude per-message usage (checked
    against `modelUsage` by spike item 6), Codex `turn.completed.usage`.
  - The number of sessions with usage must equal the number of shim launches; otherwise the token
    figure is flagged.
- **Wall time** of the engine run.

### 8.3 Per configuration

- **Headline numbers:** the mean of each metric with a 95% CI. The CI is a cluster bootstrap over
  tasks, with attempts nested.
- **Tokens and time:** median and interquartile range.
- **Stability:** the hit rate of each ground-truth item across attempts ("found in 4 of 5").
- **Comparisons:** paired, on the same tasks. The delta is shown with its CI.
- **Regression flag:** for a regression comparison, a change counts only when its CI excludes 0.

The headline ranking metric is Must-fix recall, shown with augmented precision and total tokens.

## 9. Reproducibility

The public repository contains:

- the public tasks;
- the matrix files;
- the Dockerfiles with pinned CLI and engine versions;
- the proxy allowlist;
- the extraction and judge prompts with their hashes;
- the published result sets (`results/<run-id>/`: results JSON and the report).

Another person can reproduce a run with three commands and their own credentials in
`rbench.local.toml`:

1. `rbench init`
2. `rbench run matrices/screening.yaml`
3. `rbench report <run-id>`

The report header shows what cannot be pinned: the provider can change what a model id serves, and
neither CLI exposes a seed or temperature.

## 10. Report

`rbench report <run-id>` writes `report.html`, one self-contained file that opens without a server,
and `results.json`. It can be regenerated at any time from a partial run. Sections:

1. **Overview:**
   - what was run (stages, task sets, number of trials);
   - completion percentage;
   - the judge model and prompt hash;
   - a three-line "how to read this" note.
2. **Leaderboard.** One row per configuration:
   - engine and per-role model and effort;
   - Must, Should and Nit recall with CI;
   - augmented precision;
   - false findings per PR;
   - median tokens, split by model;
   - median wall time;
   - number of trials.

   It is sortable, filterable by engine, model, effort and task set (`reviewbench`, `ours`, `hidden`,
   all), and the task-set filter is shown prominently.
3. **Quality against tokens.** A scatter plot of Must-fix recall against median total tokens, with the
   Pareto front marked. Points are coloured by engine.
4. **Compare.** Pick configurations A and B to see:
   - the paired deltas per metric, with CI;
   - the tasks where they differ most.

   This is the regression view.
5. **Task drill-down.** One task per page section:
   - the PR summary;
   - a grid of ground-truth items against trials (hit, partial or miss);
   - each finding next to its matched ground-truth item, with the judge's reasoning;
   - unmatched findings with their label.
6. **Run health:**
   - failed, interrupted and contaminated trials, with their reasons;
   - wasted tokens;
   - token capture warnings.

Hidden tasks appear only in aggregates (sections 2 and 3, and the deltas in section 4). A published
report never shows them per task: not in the drill-down, and not in Compare's list of tasks.

## 11. CLI

| Command | Does |
|---|---|
| `rbench init` | checks Docker, the credentials and the hidden path; installs the pre-push guard |
| `rbench tasks build` | builds scrubbed repos and task images for the task set |
| `rbench plan <matrix>` | expands the matrix; prints the trial count and token estimate |
| `rbench run <matrix>` / `--resume <run-id>` / `--reuse <run-id>` / `--no-wait` | runs trials under the ledger |
| `rbench judge <run-id>` | matches and labels findings |
| `rbench report <run-id>` | writes `report.html` and `results.json` |
| `rbench publish <run-id>` | copies public results and hidden aggregates into `results/` for a commit |

## 12. Spike before building

Each item here gates the design.

1. **Claude auth in Docker.** `claude -p` with `CLAUDE_CODE_OAUTH_TOKEN` works inside a container.
   (A 401 has been reported.)
2. **Codex in Docker.** `codex exec` runs with a mounted `auth.json` and the GPT-6.x models inside a
   container, including several Codex processes of one trial sharing one `CODEX_HOME`.
3. **Harbor fit.** Harbor runs one trial behind the proxy sidecar on an internal network, with our
   auth, and leaves enough raw output for the ledger. If it does not, use option B.
4. **ReviewBench matcher on a subscription.** The matcher runs with the subscription, through
   `claude -p` or a provider shim. If it needs an API key, port its prompt (MIT) to run through the
   CLI, and record that as a deviation.
5. **Limit messages.** The exact rate-limit and usage-limit outputs of both CLIs are captured as test
   fixtures.
6. **Token totals.** Tokens summed per session from the trial's transcripts (the shim log gives the
   session ids) match `modelUsage` for a Claude run that uses subagents.

## 13. Out of scope for v1

- A hosted leaderboard. The report file is the product.
- Running on GitHub CI. Runs need subscriptions; CI runs only the unit tests and the publication
  guard.
- An API-key mode and dollar costs. Tokens are the cost measure.
- Measuring engines' own sandboxes.
- The rreview Orca panel and the Orca bot. Those are separate designs that use this benchmark's
  result for the engine decision.

## 14. Decisions to confirm

1. **Judge model.** Default: `claude-opus-5-5`, effort high. A second judge, `gpt-6.2-sol`, labels a
   20% sample, and the report shows the agreement between the two, to expose self-preference.
2. **Number of top configurations in the full stage.** Default: up to 8, plus each engine's default.
3. **Harbor or own runner.** §6 and §7 already define the `docker compose` trial, the ledger and the
   raw output, so Harbor's runner adds little that the design relies on and costs a second code path.
   Recommendation: build option B (own runner) from the start, keep ReviewBench, and drop spike
   item 3. This changes the choice of option A made during design, so it needs the maintainer's yes.
4. **Calibration of the judge against human labels.** Default: the maintainer labels the 5 calibration
   PRs once, and the report shows judge-human agreement on them.
