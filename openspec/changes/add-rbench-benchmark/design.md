# Design

## Context

See proposal.md for motivation; the requirements are in the specs.

Constraints that shape the approach:
- **Subscriptions, not API keys.** Runs use the operator's subscriptions: a Claude OAuth token from
  `claude setup-token`, and a ChatGPT login for Codex. Claude Code's `--bare` mode needs an API key
  and changes the agent, so it is not used.
- **Engines launch the CLIs themselves.** rreview, review-council and ponytail-review start
  `claude` and `codex` on their own, and several processes can run per trial. The benchmark cannot
  pass flags to those launches directly.
- **Host settings leak in.** Managed settings and the operator's user config cannot be bypassed on a
  host. In an earlier comparison, the Codex default home silently picked a different model, and a
  user prompt hook ran inside `codex exec`.
- **Two reported incidents set the threat model:**
  - SWE-bench: agents read later fix commits via `git log --all`.
  - The Cursor audit of June 2026: 57% of runs of one model found fixes on the web.
- **Host:** macOS 14 with 16 GB RAM and Docker Desktop. Apple's `container` needs macOS 26 and is
  ruled out.

## Goals / Non-Goals

**Goals:**
- Every requirement in the specs is met with the least new code: reuse ReviewBench's data, format
  and matcher.
- A run can be stopped at any moment and resumed, and its partial report is still meaningful.
- Another person can rerun a public matrix with their own credentials and three commands.

**Non-Goals:**
- Making scores independent of the provider. A model id can serve different weights over time, and
  neither CLI exposes a seed or temperature. The report states this instead.
- Preventing a model from having memorized public PRs. The report shows the three task sets
  separately instead.

## Decisions

### D1. Reuse ReviewBench's data and matcher; our own runner
**Choice.** ReviewBench (MIT) supplies:
- the external task set (its 25-PR test subset first);
- the findings and golden format;
- the matcher (same underlying issue, many-to-many, grounded and augmented metrics).

ReviewBench is pinned to a commit; importing from a newer commit makes a new task-set version.
Trials run on our own `docker compose` runner (D3, D5, D8), not on Harbor (O3, decided).

**Pinned commit.** `review-bench/ReviewBench` at `082f65f6746c39b6ac25b0a62c1b0d5131a3803a`
(2026-11-18, MIT), the head of its `main` branch when the spike ran.
At that commit:
- `corpus/manifest.json` lists 219 PRs; `corpus/test/test.json` lists the 25-PR test subset.
- `golden/<pr_key>.json` holds each PR's findings, labelled `tp` or `fp`. The test subset has 342
  `tp` items (35 high, 128 medium, 179 low) and 213 `fp` items. `corpus/test/findings.json` is the
  same `tp` items with fewer fields, so the import reads `golden/`.
- All 25 source repositories were MIT on their default branch on 2026-11-20 (the import still
  checks the license at the reviewed head, spec `review-tasks`, Source license). Each has a mirror
  `review-bench/<owner>_<repo>` holding its base and head commits; the import fetches from the
  mirror, as ReviewBench does.

**Field mapping to spec `review-tasks`, Task contents.**

| Task field | ReviewBench source |
| --- | --- |
| source repository, PR number | manifest `repo` (or `nwo`), `pr_number` |
| base and head commits | manifest `base`, `head`; head is the state before review follow-ups |
| SHA-256 of the source archive | computed by our import from the bundle built from the mirror |
| language | manifest `language` |
| set | `reviewbench` |
| PR title and description | rebuilt by our import at the reviewed head (import gap below); not the manifest's `title`, `body` |
| ground truth | the `tp` items of `golden/<pr_key>.json`, mapped below |

*Import gap: title and description at the reviewed head.* The manifest's `title` and `body` are
not the values at the reviewed head. Example: it records `cmik/apilix#12` as
`implement data storages`, a rename made at 2026-04-09T05:06:14Z, after the merge at 05:05:18Z;
the title before the merge was `Add jsonwebtoken and bcryptjs to server dependencies`. Handling
(maintainer's decision): the import takes a title and a body only with evidence that they were the
versions while the pinned head was the PR's head, and never rewinds to an earlier point such as the
first review. The evidence: the timeline's `renamed` events and the body's edit history (GraphQL
`userContentEdits`) show no change to that field from the head commit's committer date to the PR's
merge or close; the value then is the one in force over that whole window. The committer date is no
later than the push, so the window only errs toward a skip. When the evidence is missing (a change
inside the window, or no readable history), the task is skipped by the existing path: it is not
imported, and the set's changelog names it. In the example, the window runs from 23:21:05Z to the
merge, with no edit inside it, so the title is the pre-rename one. The judge then sees these values,
where ReviewBench's classifier sees the manifest's. Six PRs of the full set
have an empty body, none in the test subset; an empty description is a value, not a missing field.

**Field mapping to spec `review-tasks`, Ground truth items.**

| Item field | ReviewBench source |
| --- | --- |
| id | `<pr_key>#<n>`, `n` the item's 0-based index in the golden file, `fp` items included, so ids stay stable when the import filter changes |
| file, line range | `file`, `start_line`–`end_line` |
| message | `message` |
| category | `category` (`correctness`, `reliability`, `maintainability`, `testing`, `security`, `documentation`, `performance`, `api-architecture`, `accessibility`, `other`) |
| severity | `high` → `must`, `medium` → `should`, `low` → `nit` (ReviewBench has no `style` severity) |
| producer | `human_review` → `human`; `<engine>:<model>` values are kept verbatim as engine and model (`llm_review:claude-sonnet-4.6`, `ccr:gpt-5.5[ReasoningEffort=medium]`); `deterministic_tool:<tool>` → `unknown` (import gap below) |
| canary | generated by our import, one per task |

`scope`, `difficulty`, `context_required`, `source` and the three justifications are dropped from
the item.

*Import gap: producers without a model.* ReviewBench's `deterministic_tool:semgrep` names a tool,
not an engine and model, `human` or `unknown`, the producer forms spec `review-tasks` allows; one
`tp` item of the test subset has it. Proposed handling, within the spec: map it to `unknown`. No
engine under test is a static analyzer, so the self-produced marking (spec `review-scoring`) loses
nothing; the tool's name stays findable through the item id in the pinned golden file. Recording
the tool in the task itself would need a new producer form, a spec change not proposed here.

*Where the golden file goes.* Never into the source archive, the trial's repository or any other
trial input (spec `trial-isolation`, One fresh sandbox per trial). The importer reads
`golden/<pr_key>.json` from a checkout of the pinned commit outside the task directory and writes
only the mapped ground truth, which lives with the task's metadata under `tasks/reviewbench/` and is
read only by the importer, the judge and the scorer. The raw golden file is not stored; the set's
manifest records its path and SHA-256 at the pinned commit for provenance.

Only `tp` items become ground truth: the spec's ground truth has no label for a known false finding.
**Deviation**, attributed to this choice in the report: in ReviewBench a finding that claims a golden
`fp` item counts against grounded precision and adds nothing to augmented precision's numerator.
Here, with no `fp` items to match, it goes to the unmatched-finding label and can be labelled `valid`.
Dropping `fp` items can also change which item a finding claims when it matched both a `tp` and an
`fp` item. Neither grounded nor augmented precision is comparable to ReviewBench's.

**Matcher path: port the prompts to the CLI judge.** ReviewBench's judging pipeline
(`scripts/eval/`) calls models through the pi SDK with a provider API key (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, ...; `docs/JUDGING.md`), outside our hermetic container and shims. So the judge
runs ReviewBench's prompts (MIT) through `claude -p` or `codex exec` as spec `review-scoring`
requires, and the report states the deviations below. Matcher version = the pinned commit plus our
prompt hash.
- *Matching* (`scripts/eval/matcher.ts`, `MATCHER_SYSTEM_PROMPT`, plus the untrusted-data notice of
  `prompt-format.ts`): the prompt is ported as is, with no tools as in ReviewBench. Upstream groups
  findings by exact file path, never compares findings in different files, and compares each
  file's findings in chunks of 10 candidates × 10 golden items. The port keeps both the file grouping
  (O7, decided) and the chunk cap. The answer is JSON
  `{reasoning, candidateFindings: [{index, matchedGoldenIndices}]}`, validated whole. ReviewBench
  reuses one session per PR across chunks; the port makes each chunk one stateless call.
- *Resolving matches for scoring* (`scripts/lib/match-types.ts`, METHODOLOGY 6.2.4): recall covers
  every item any finding matched; for grounded precision each finding, in order, claims the first
  item it matched that no earlier finding claimed. Upstream sends a finding that matched only
  already-claimed items to the classifier with the unmatched ones. The port does not: such a
  *duplicate* matches ground truth, so it is not an unmatched finding (spec `review-scoring`, Labels
  for unmatched findings). **Decided by the maintainer:** a duplicate, a finding that matches only
  ground-truth items already claimed by another finding of the same trial, counts in neither the
  numerator nor the denominator of grounded or augmented precision, never gets a label, and the
  report shows each trial's duplicate count separately. Repeating a found issue then neither raises
  nor lowers precision. **Deviation:** upstream classifies duplicates and counts them in augmented
  precision. Spec `review-scoring` gets the duplicate rule in the metrics implementation issue;
  this change does not add it to the specs.
- *Labels for unmatched findings* (`scripts/classifier/prompts.ts`): ReviewBench's classifier
  explores the repository at head with `read`, `grep`, `find` and `ls`. Spec `review-scoring` gives
  the judge no tools, so the port has none. Its input is only the normalized unmatched finding and
  the rendered PR context: the diff, the title, the description and the content of each file the
  finding cites, resolved inside the immutable reviewed tree of the task's verified archive, never
  an engine's workspace. It gets no matcher transcript, no golden item, no canary and no engine or
  configuration identity. Repository text is untrusted data, quoted as such in the prompt like the
  finding. The rendered context is part of the judge's cache key. The prompt's directions to
  explore the repository with tools become directions to verify against that context, and
  upstream's rule that an unverifiable claim is `fp` applies to it. The port asks for the spec's labels directly, with this rubric:
  - `false`: upstream's FP criteria (incorrect, unverifiable, generic, sub-threshold, harmful);
  - `nit`: a TP whose fix only changes naming, formatting, spelling or comment wording, with no
    effect on behavior, security, reliability, performance, tests or a public API (upstream's
    "pure naming, formatting, spelling, and comment cleanup");
  - `valid`: any other TP, whatever its upstream severity, so a real defect upstream would rate
    `low` is still `valid`.

  **Deviation:** without the repository the labels can differ from ReviewBench's, so augmented
  precision is not comparable.
- ReviewBench's own judge settings (leaderboard: Claude Opus 5; matcher thinking `low`, classifier
  `medium`) differ from ours (O1), another reason our numbers are not its leaderboard's.
- Matching stays within one file, as upstream (O7, decided), so this part of the matcher stays
  comparable with ReviewBench.

**Alternatives considered:**
- *Martian code-review bench (MIT, 50 PRs).* Optional later, as a second external set.
- *Our own matcher.* More work, and it loses comparability.
- *Inspect AI.* A strong harness, but more glue than our ledger needs.

### D2. Python 3.12 with `uv`; one static HTML report
**Choice.** Python fits the runner, which mostly drives child processes and reads and writes JSON.
The report is one HTML file with the results embedded as JSON and plain JavaScript. It needs no
server or build step, and opens from disk as the spec requires.

### D3. Trial = one `docker compose` project: agent container + squid proxy
**Choice.**
- The agent container sits on an `internal: true` network. Its only route out is a squid proxy that
  allowlists the providers' API and auth hosts and logs every CONNECT. The host list comes from a
  logged smoke run and is fixed in `images/proxy/allowlist`.
- Images are built per engine version and CLI versions, not per task. The scrubbed repository is
  unpacked from the task's archive (D11) and copied in with `docker cp` when the container is
  created, never mounted.

**Alternatives considered:**
- *Anthropic's devcontainer firewall (iptables).* It allows GitHub and port 22, and resolves domains
  once.
- *Claude's or Codex's own sandboxes.* They read most of the host.
- *A separate macOS user.* Fragile, and others cannot reproduce it.
- *Docker Sandboxes (`sbx`).* Codex support and macOS 14 support are unverified; it is kept as a
  fallback.

### D4. Repository scrub
**Choice.**
- The repository is built once, at import, from the source repository: `base` is the PR's base
  tree, and `reviewed` is the head tree squashed onto it. It is stored as a git bundle (D11).
- A fresh `git init` has no remotes, reflog, tags or packed objects.
- A `gh` stub on `PATH` answers `pr view` and `pr diff` from `pr.json` and the repository. It
  reports the PR as `OPEN` with no comments or reviews, so engines that skip closed or reviewed PRs
  still run. `gh pr comment` writes its body to the trial's raw output, which normalization reads
  like any other engine output. Anything else fails.

### D5. Managed configuration enforces the hermetic policy; shims log
**Choice.**
- The policy lives in each CLI's managed configuration inside the image:
  `/etc/claude-code/managed-settings.json` for Claude Code, and `/etc/codex/managed_config.toml` or
  `requirements.toml` for Codex (whichever the pinned Codex version enforces; spike item). An agent
  with a shell can start the real binary by absolute path and skip any shim, but not the managed
  configuration.
- Engines launch CLIs themselves, so `claude` and `codex` shims still sit first on `PATH`. Each
  shim:
  - repeats the policy flags as a second layer: Claude `--strict-mcp-config` and denied
    `WebSearch`/`WebFetch`; Codex `--ignore-rules --strict-config -c web_search="disabled"`;
  - adds the model and effort flags for engines whose adapter passes them by flag;
  - logs the argv and session id;
  - execs the real binary.
- The audit compares the sessions in the transcripts with the shim log; a session that bypassed the
  shims marks the trial contaminated.
- The Codex shim does not add `--ignore-user-config`. review-council 0.14.0 launches
  `codex exec --sandbox read-only --skip-git-repo-check <prompt>` with no `-m`, and sets the Codex
  model and effort only through `config.toml` (`model`, `model_reasoning_effort`). Ignoring the
  config silently falls back to Codex's default model, which is the confound of the first 5-PR
  comparison. Such adapters write the role's model and effort into the trial's `config.toml`.

The shims never change the prompt.

The trial has one `CLAUDE_CONFIG_DIR` and one `CODEX_HOME`. All processes share them. Codex uses a
login made only for the benchmark (its own `codex login`, so its own refresh-token chain), kept in a
login home no other tool uses. Its `auth.json` is bind-mounted into the trial, and refreshed tokens
go back only to that home. The trial homes hold:
- the policy settings;
- `ENABLE_CLAUDEAI_MCP_SERVERS=false`;
- auto-memory and background tasks off, non-essential traffic off;
- telemetry, error reporting and the autoupdater off;
- `enableWeakerNestedSandbox`;
- the engine's declared plugins.

`CLAUDE_CODE_EFFORT_LEVEL` is unset. The reviewed repository's `CLAUDE.md` and `AGENTS.md` stay.

**Alternative considered:** pass flags through each engine's configuration. Not every engine exposes
that, and it would make the policy differ between engines.

### D6. rreview runs with `--sandbox external`
**Choice.** rreview's inner sandbox does not nest in a container: `codex -s read-only` may not
start. A new opt-in rreview flag skips it, so all engines run the same way. rreview's own isolation
stays covered by rreview's tests on the host.

**Alternative considered:** nested sandboxes with weaker settings. If Codex cannot start, rreview
cannot run at all.

### D7. Token accounting from transcripts
**Choice.** Usage is summed per session from the trial's transcripts:
- Claude: per-message usage, deduplicated by `message.id`, because the transcript repeats one
  message's usage on every streamed chunk;
- Codex: `turn.completed.usage`. `cached_input_tokens` is probably a subset of `input_tokens`; the
  spike checks it, and uncached input is `input_tokens - cached_input_tokens` if so.

The quality-against-cost plot uses uncached input plus output, per provider, with cache tokens
shown apart: cache reads are cheap and would otherwise dominate the axis.

The shim log gives the expected set of sessions. A Claude result event's `modelUsage` is the check,
for runs where it is visible.

**Alternative considered:** capture each process's final result event. Engines own stdout, so it is
not always visible.

### D8. Ledger on the filesystem
**Choice.** `runs/<run-id>/` holds:
- `plan.json`;
- `ledger.lock`;
- one directory per trial with `status.json`, `findings.json`, `usage.json` and `raw/`;
- `judge/`.

Writes are atomic (temporary file, then rename). Limit detection is per CLI, and the exact messages
are captured as test fixtures.

**Alternative considered:** SQLite. It is not needed at this size, and per-trial files make partial
copies and inspection trivial.

### D9. Markdown normalization by a pinned extraction step
**Choice.** Engines that output Markdown (review-council, `/code-review`, ponytail-review) are
converted to findings by a pinned model with a versioned prompt. The step copies findings and does
not judge them. It runs hermetic, like the judge: the same container setup and shims, no tools.

**Alternative considered:** ask each engine for JSON. That changes the engines being measured.

`plain` is the exception: it is our own baseline, not an engine under test, so it asks for JSON
directly (D12) and skips this step.

### D10. Staged matrix
**Choice.** Smoke, then screening, then full, as in the `engine-configurations` spec. The full
stage's configurations are picked by the maintainer from the screening Pareto front. A full
cartesian product of models, efforts and roles would exhaust the budget before giving 3 attempts
anywhere.

### D11. Source archives and task-set versions
**Choice.**
- Each task set has `tasks/<set>/manifest.json` with the set version, the per-task content hashes
  and the changelog. A unit test recomputes the hashes, so CI fails on an edit without a new
  version.
- Archives of public sets are GitHub release assets, one release per set version.
  `rbench tasks fetch` downloads them and checks each SHA-256 against the manifest.
- Archives of hidden tasks live next to the tasks in the hidden directory.

**Alternatives considered:**
- *Bundles committed to git.* They bloat every clone, and history keeps old versions forever.
- *Git LFS.* The free quota is small, and it adds a dependency for anyone who reruns a matrix.
- *Fetch from the source repository on every trial.* Breaks when the repository disappears or
  rewrites history, and the trial would need network access to it.

### D12. `plain` through each CLI's own options
**Choice.**
- The prompt and schema live in `engines/plain/` (`prompt.md`, `findings.schema.json`).
- Claude: `claude -p --tools Read,Grep,Glob,Bash --json-schema <schema>`.
- Codex: `codex exec --output-schema <schema> -c web_search=disabled -c features.goals=false
  -c features.view_image=false -c model_catalog_json=<catalog>`; the container is the sandbox
  (as in D6).
- Codex has no flag to drop its `apply_patch` edit tool: the tool comes from the model catalog's
  `apply_patch_tool_type`. The benchmark's catalog is `codex debug models` at the pinned CLI
  version, with that field set to null for every model in the matrix and nothing else changed. It
  is generated per CLI version, and its hash is part of the configuration's settings.
- Checked with Codex CLI 0.167.0 and Claude Code against a local endpoint that records requests:
  - Codex by default sends `apply_patch`, `exec_command`, `write_stdin`, `view_image`,
    `web_search`, the goal tools, `tool_search` and `request_user_input`. With the options above it
    sends only `exec_command`, `write_stdin`, `tool_search` and `request_user_input`; the rest of
    the request is unchanged.
  - Claude with `--tools Read,Grep,Glob,Bash` sends only those four tools.
- Model and effort come from the matrix, and the hermetic shims (D5) apply as for every engine.

**Alternative considered:** give `plain` only the diff, with no tools. It is cheaper, but it would
measure a different task from the engines, which all read the repository.

### D13. Screening matrix
**Choice.**
- Models: `claude-fable-5-2`, `claude-opus-5-5`, `claude-sonnet-5-5`, `gpt-6.2-sol`,
  `gpt-6.2-astra`.
- Efforts: `medium`, `high`, `xhigh`.
- One round is one effort level for every role, in the order of O6: R1 `high`, R2 `medium`, R3
  `xhigh`. Mixed-effort combinations and mixed rreview panels are later rounds, after R3.
- Configurations of the full matrix, which R1 and R2 each run:

  | Engine | Roles and models | Configurations |
  | --- | --- | --- |
  | `plain` | one reviewer, every model | 5 |
  | `code-review` | Claude models | 3 |
  | `codex-review` | GPT models | 2 |
  | `ponytail-review` | Claude models on Claude Code, GPT models on Codex | 3 + 2 |
  | `review-council` | Claude side × Codex side | 3 × 2 |
  | `rreview` | panel × coordinator × judge | 2 × 3 × 2 |
  | **Total** | | **33** |

- review-council: the Claude side is the main session (orchestrator and judge) plus the subagents
  `reviewer-claude`, `reviewer-security` and the Claude verifier, all `model: inherit`, so the
  session's `--model` and `--effort` set them. The Codex side is the Codex reviewer and verifier,
  set only through `config.toml`. The Google and Perplexity seats are disabled in
  `.review-council/config.local.yml`.
- rreview: the panel roles `intent_contracts`, `reliability` and `security` run on Codex with one
  model for the whole panel; the coordinator runs on Claude Code; the judge runs on Codex, because
  rreview refuses a coordinator and judge of the same family.
- At 10 core tasks and one attempt, one round of all 33 configurations is 330 trials. R1 and R2 run
  all 33; R3 runs only the engine and role-model combinations on the quality-against-cost Pareto
  front after R1 and R2, each once at `xhigh` (O6).

**Alternative considered:** a full cartesian product of models, efforts and roles in one run. It
does not fit any budget the maintainer is willing to spend.

### D14. Rounds with budgets instead of spending the limits
**Choice.** The maintainer does not want a run to use up the subscription limits; ideally no run
hits one. The matrix is split into rounds by importance, each with a token budget per provider. The
runner starts a trial only if its estimate fits, and stops at a soft threshold of the subscription
window where the CLI reports its used share. O6 sets the round order, the budget rule and the
threshold value. Codex reports `rate_limits` in its events; whether
Claude Code reports anything is a spike item. Checkpoint and resume stay as the safety net, and a
limit stops the run by default; waiting for the reset is opt-in (`--wait`).

## Risks / Trade-offs

- **[Claude OAuth token returns 401 in containers (reported upstream).]** → First spike item. If it
  fails, try the `sbx` microVM, then run Claude on a separate host user with the proxy as fallback.
- **[Codex `auth.json` refresh races between processes.]** → One login file per trial, at most one
  Codex trial per login, and a login only the benchmark uses, so Orca and the operator's own Codex
  never share its refresh-token chain. The spike runs several Codex processes in one trial.
- **[`codex-review` may not run non-interactively.]** → Codex's built-in review is a TUI command;
  the spike finds a non-interactive way to run it. Without one, `codex-review` leaves the matrix.
- **[Claude Code may not report the used share of its subscription window.]** → The spike checks.
  Without it, the soft threshold is not enforced for Claude, and the round budget is the only stop.
- **[Shim flags conflict with an engine's own flags.]** → The spike runs each engine's default once
  with the shims and checks the argv log.
- **[The judge prefers its own model family.]** → The judge is blind to the engine, and a second
  judge from the other family labels a sample (O1).
- **[Ground truth for our PRs is incomplete or produced by an engine under test.]** → Augmented
  precision, and each item's recorded producer and source (specs `review-tasks`,
  `review-scoring`). The ground truth is mined from reviewed historical PRs (O5, decided).
- **[ReviewBench's matcher needs an API key.]** → Confirmed at the pinned commit. Its prompts (MIT)
  are ported to the CLI judge, and the report records the deviations (D1).
- **[Publishing source archives of third-party repositories.]** → The license check on import
  (spec `review-tasks`, Source license). The allowed list starts with MIT, Apache-2.0, BSD-2-Clause,
  BSD-3-Clause, ISC, MPL-2.0 and the GPL/LGPL/AGPL family, all of which allow redistribution of
  source with the license kept.
- **[Codex's base instructions still mention `apply_patch` after the tool is removed.]** → The
  model may try to call it and get an error, or write files through the shell. Neither reaches the
  findings. The smoke stage counts such calls in `plain` transcripts.
- **[A remote model catalog may override `model_catalog_json` under a ChatGPT login.]** → The local
  check used a custom provider, so the remote catalog was not fetched. The spike repeats the check
  with the real login and reads the tool list from the session transcript.
- **[ReviewBench tasks are public and likely memorized.]** → The report shows task sets apart; `ours`
  and `hidden` carry the weight of the engine decision.
- **[Transcript usage counts differ from billed usage.]** → The spike compares them with
  `modelUsage`. If they differ, prefer `modelUsage` where it is visible.

## Migration Plan

New repository; nothing to migrate. Rollout:
1. Run the spike.
2. Build the smoke stage.
3. Build screening and the report.
4. Build the full stage and publishing.

## Open decisions (need the maintainer before tasks.md)

O1, O3, O5 and O7 were decided by the maintainer on 2026-11-20 and O6 on 2026-11-21; they are
marked so in place. O2 and O4 are open.

- **O1. Judge model.** **Decided (maintainer, 2026-11-20):**
  - `claude-opus-5-5` at high effort is the judge.
  - A second judge, `gpt-6.2-sol`, labels a 20% sample.
  - The report shows the agreement between the two judges (spec `review-scoring`, Agreement between
    judges).
- **O2. Size of the full stage.** Proposal: up to 8 configurations from the screening Pareto front,
  plus each engine's default.
- **O3. Harbor or our own runner.** **Decided (maintainer, 2026-11-20):** our own `docker compose`
  runner from the start; ReviewBench's data and matcher are kept (D1). Harbor is not used.
  - D3, D5 and D8 already define the trial, the policy, the raw output and the ledger, so Harbor's
    runner adds little the design relies on.
  - Using Harbor would cost a spike item and a second code path.
  - This reverses the earlier choice of Harbor.
- **O4. Calibration of the judge against human labels.** Proposal: the maintainer labels the 5
  calibration PRs once, and the report shows judge-human agreement on them.
- **O5. Source of ground truth for `ours` and `hidden`.** **Decided (maintainer, 2026-11-20):**
  option A.
  - Ground truth is mined from historical PRs of active, permissively licensed repositories with a
    human review culture, merged after the models' training cutoffs: review comments, plus
    follow-up commits that change the commented lines.
  - Each item records the comment or commit it was taken from (spec `review-tasks`, Recorded origin
    of ground truth for our PRs).
  - Not taken, option B: pool the findings of all engines on our PRs, and the maintainer confirms
    them.
- **O6. Round details.** **Decided (maintainer, 2026-11-21):**
  - Round order of screening: R1 effort `high`, R2 `medium`, R3 `xhigh`; each round is one effort
    level for every role (D13). Mixed-effort and mixed rreview panels come after R3, as D13 says.
  - R3 (`xhigh`) runs only the configurations on the quality-against-cost Pareto front after R1 and
    R2, not all 33 (a round lists its own configurations, spec `engine-configurations`, Rounds).
    R3 takes the engine and role-model combinations on the front and runs each at `xhigh`, once
    even if both R1 and R2 put it on the front; its configuration ids differ from those of R1 and
    R2 because the effort is part of the settings (spec `engine-configurations`, Screening round
    selected from the front).
  - Budget rule: a round's budget per provider is the `rbench plan` estimate for that round (trial
    count × token medians from the smoke stage) × 1.2. After the smoke stage the coordinator brings
    the numbers per round to the maintainer, who approves them before the round runs. `rbench plan`
    refuses a round whose estimate exceeds its budget.
  - Soft threshold: 80% of every subscription window the CLI reports (for Codex both the 5-hour and
    the weekly window). Where a CLI reports no used share, the threshold is not enforced for that
    provider, and the plan and the report say so (spec `run-ledger`, Soft threshold of the
    subscription window).
- **O7. File-scoped matching.** **Decided (maintainer, 2026-11-20):** option A, as in ReviewBench
  (METHODOLOGY 6.2.1).
  - A finding is compared only with ground-truth items in the same file; within a file, location is
    context only. The judge sees one file's findings per call, which keeps the calls small.
  - A match is a match: there is no separate score for a finding that names the root cause rather
    than a symptom.
  - Spec `review-scoring`, Matching findings to ground truth, states the rule.
  - This keeps matching comparable with ReviewBench.
  - Cost: a finding that names the right issue in another file (a caller, a test) is not matched.
    The ground-truth item is a recall miss unless another finding of the trial matches it (such as
    one in the item's file); the finding goes to the unmatched-finding labels and, if it describes a
    real defect, is labelled `valid` and counts toward augmented precision (spec `review-scoring`,
    Labels for unmatched findings).
  - How often the ground truth reports one issue in different files: a manual count on
    ReviewBench's 25-PR test subset at the pinned commit (`082f65f`, D1) found 15 of the 342 `tp`
    items (6 issues in 5 PRs). This is the prevalence in the ground truth, not the number of lost
    matches, which depends on where engines report each issue.
  - Not taken, option B: compare across files, all of a PR's findings in chunks. Closer to the
    spec's earlier wording, but it compares more pairs, can need more calls and is a further
    deviation from ReviewBench.
