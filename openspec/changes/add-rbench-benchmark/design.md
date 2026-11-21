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
  `/etc/claude-code/managed-settings.json` for Claude Code, and `/etc/codex/requirements.toml` for
  Codex. An agent with a shell can start the real binary by absolute path and skip any shim, but
  not the managed configuration.
- Codex managed files, tested with Codex CLI 0.171.0 (spike #20):
  - `requirements.toml` sets allowed values, and Codex enforces them over the user `config.toml`
    and `-c` flags. With `allowed_web_search_modes = ["disabled"]`, a user `web_search = "live"`
    and `-c web_search="live"` both fell back to `disabled`, with a startup warning that names the
    file. A `[features]` entry there pinned a feature against `--enable`.
  - `requirements.toml` also sets `cli_auth_credentials_store = "file"`. This was configured, not
    tested: no run gave it a conflicting user value. That it is enforced comes from the 0.171.0
    source, which applies this field over `config.toml` as an exact value
    (`codex-rs/core/src/config/requirements.rs`). `file` is also Codex's default.
  - `managed_config.toml` is still read, as a legacy file. Codex makes it the top config layer:
    its `model` beat both the user `config.toml` and `-c model=...`. It would therefore override
    the model and effort that engines set in the trial's `config.toml`, so the image does not ship
    it.
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
login home no other tool uses. That login home is a dedicated credential directory. It holds
`auth.json` and the files Codex makes next to it: `auth.json.lock` and transient `.auth-*` save
files. It holds nothing unrelated; files left by `codex login` (logs, `tmp/`) are removed. It is
mounted as a directory, read-write, into the trial, and the trial's `CODEX_HOME/auth.json` is a
symlink to the `auth.json` in it. Mounting the file alone does not work: Codex 0.171.0 replaces `auth.json` by a
temporary file and a rename in the target's directory, and a rename onto a mount point fails with
`EBUSY`. Codex follows the symlink, so the temporary file and its `auth.json.lock` both land in the
login home. Refreshed tokens therefore go back only to that home. Sessions, logs and state stay in
the trial's `CODEX_HOME` inside the container. The trial homes hold:
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
**Choice.** Usage is the CLI-reported usage, counted per session from the trial's on-disk
transcripts, not from stdout (spike on Claude Code 2.1.316 and Codex CLI 0.171.0, issue #24). The
transcript paths and record schemas below are those of the pinned versions; a CLI upgrade re-checks
them in the smoke stage.

Claude: the session file under `$CLAUDE_CONFIG_DIR/projects/`.
- Each message's usage is counted once, deduplicated by `message.id`, because the transcript
  repeats one message's usage on every content block, and split by `message.model`.
- In the spike's main session (one model, no subagents), the sum matched the CLI-reported
  `modelUsage` exactly on input, output, cache read and cache creation.
- The stdout `stream-json` assistant events are not used: they under-report output tokens for
  messages that end in a tool call.
- Session persistence stays on (no `--no-session-persistence`), or there is no transcript.
- Unverified, a smoke check: subagent transcripts and side calls on a different model (collection,
  deduplication, and whether `modelUsage` covers them).

Codex: the rollout file under `$CODEX_HOME/sessions/`.
- `cached_input_tokens` is a subset of `input_tokens` (`total_tokens` = input + output), so
  uncached input is `input_tokens - cached_input_tokens`.
- Primary: the rollout's `token_usage_record` entries, one per model response, counted once by
  `response_id` (with `thread_id` and `turn_id`) and summed per model. The model is the turn's
  `turn_context.model` only when it agrees with the effective model the CLI observed for the
  response; otherwise the trial's model attribution is flagged incomplete. The records come from
  the pinned source; the spike read only `token_count`, so their on-disk layout is a smoke check.
- Built-in review (#22) produced two rollout files with two distinct model response records in
  aggregate. In the smoke stage, check whether the `SubAgentSource::Review` thread's usage appears
  in the parent rollout (identified in D13), the child rollout, or both. Verify that
  `token_usage_record` counts each response once and whether the `token_count` fallback covers
  the review thread.
- Fallback when a rollout has no such records: its last `token_count` event's `total_token_usage`.
  `token_count` is a cumulative snapshot without a response id, and a rate-limit update can repeat
  it without a request, so it is used only when the thread starts from zero usage (not a fork), its
  counters are valid, no context-window reset occurred, and every request used the same verified
  model. A forked thread starts with its parent's usage: its total minus that baseline is its own.
  After a context-window reset (a snapshot with zeroed counters), cumulative accounting stops; later
  usage comes from unique response records or the trial's usage is flagged incomplete. How fork
  lineage (`session_meta.forked_from_id`) gives the baseline is a smoke check.
- Last fallback when the rollout is missing or unreadable: the last `turn.completed.usage` on
  stdout (`codex exec --json`), a cumulative thread total without a model split. It counts as
  complete only when it covers the thread's last launch in the shim log, its counters are valid,
  its starting usage is known (zero or a recovered fork baseline) and the thread used one verified
  model; otherwise the trial's usage is flagged incomplete.
- When none of these recovers a thread's own usage, the trial's usage is flagged incomplete.
- Provisional: `cache_write_input_tokens` is counted as cache creation. Whether it overlaps
  `input_tokens` is unknown (the spike saw only zeros); a nonzero case is a smoke check before
  ordinary input and cache creation are split.

`modelUsage` and Codex's `turn.completed.usage` are cumulative over a resumed session, so neither is
summed across the processes of one session.

The quality-against-cost plot uses uncached input plus output, per provider, with cache tokens
shown apart: cache reads are cheap and would otherwise dominate the axis.

The shim log gives the expected set of sessions. A session with no usable source (no transcript and
no covering result) flags the trial's usage as incomplete.

The cross-check for a Claude session is its cumulative `modelUsage`: the transcript's last
`cost-state` record (undocumented), or the result event of the session's last process where it is
visible. A snapshot is used only when it covers the same work: it comes after the session's last
assistant message and belongs to the session's latest launch in the shim log (a `cost-state`
`startTime` or result event from that launch). On a mismatch with such a snapshot, its
`modelUsage` is used and the trial is flagged. Without a covering snapshot, including when the
latest launch wrote none, the cross-check is unavailable; it never replaces the message counts.

**Alternative considered:** capture each process's final result event. Engines own stdout, so it is
not always visible.

### D8. Ledger on the filesystem
**Choice.** `runs/<run-id>/` holds:
- `plan.json`;
- `ledger.lock`;
- one directory per trial with `status.json`, `findings.json`, `usage.json` and `raw/`;
- `judge/`.

Writes are atomic (temporary file, then rename).

Limit detection is per CLI. It reads the error record that ends a turn in the CLI's session file,
not stdout (engines own stdout, D7):
- **Usage limit.**
  - Claude Code 2.1.316: the transcript's synthetic assistant message has `error: "rate_limit"`,
    `apiErrorStatus: 429` and `apiError: "usage_limit_reached"`.
  - Codex CLI 0.171.0: the rollout's `task_complete` has `error.codex_error_info:
    "usage_limit_exceeded"`. Inferred from the pinned source, not observed in the spike: Codex
    also maps `usage_not_included` and `insufficient_quota` to this code.
- **Terminal rate limit.**
  - Claude: `error: "rate_limit"` with `apiErrorStatus: 429` and no `apiError` ("not your usage
    limit"). This holds even when the record says `apiErrorIsTransient: true`: the record ends the
    turn, so the CLI has given up.
  - Codex: `codex_error_info.response_too_many_failed_attempts.http_status_code: 429`, recorded
    after retries run out. `codex_error_info: "rate_limit_exceeded"` (a rate limit inside the
    response stream) is inferred from the pinned source's error enum, not observed in the spike.

Both kinds are a limit under spec `run-ledger` (Limit detection and stop; Waiting for the limit to
reset). The trial becomes `interrupted` and keeps its tokens as wasted. By default the runner drains
and exits 75. With `--wait`, only the reporting provider pauses. A throttle that the CLI retries and
recovers from is not a limit: the turn does not end on such a record.

**Precedence.** The typed classification wins over the message text. Exit status 1 alone selects
nothing, because both CLIs exit 1 on any failed turn. The text decides only when no typed record
can be read: Claude's own usage-limit prefixes, and Codex's "You’ve hit your usage limit" and
"exceeded retry limit, last status: 429" (`messages.code.json` in the fixtures).

**Reset time for `--wait`.**
- *Claude:* the structured epoch comes first: `quotaLimits.resetsAt`, or
  `apiErrorParams.rate_limit_info.resetsAt`. It is used when it is an integer in the future and no
  further away than the limit's window. The rendered text is not parsed.
- *Codex:* it records only rendered text. "Try again at 3:32 AM." is a time on the CLI process's
  local date in its time zone: the trial container's `TZ`, UTC unless the image sets one. That date
  is taken at the record's `timestamp`. "Try again at Nov 22nd, 2026 3:32 AM." gives the full date
  in the same time zone.
- *No reset time:* when the time is absent ("try again later"), unparseable or invalid, or the
  limit is a terminal rate limit without one, the spec's backoff applies: exponential, at most one
  hour apart.

Messages, typed fields and reset time formats are test fixtures in `tests/fixtures/limits/`. Each
is marked as captured live, produced by the real CLI against a local 429 stub, or taken from the
CLI's code.

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

- `codex-review` runs the built-in review from the `reviewed` checkout:

  ```sh
  codex exec -m "$model" -c "review_model=\"$model\"" -c "model_reasoning_effort=\"$effort\"" \
    --sandbox danger-full-access \
    --json -o "$review_text" review --base base
  ```

  The D5 shim supplies the hermetic flags. Inside Docker, the container is the sandbox (D6).
  Set both model options because the review task gives `review_model` precedence over the
  session model. The 0.171.0 help lists `codex review` as a headless command, and the pinned
  source routes both headless forms and the TUI through `review/start`. The live spike exercised
  `codex exec review`, which exposes both capture options.
- Capture stdout JSONL and the final text file for diagnostics. The final
  `item.completed` event with `item.type = "agent_message"` contains the same rendered review as
  `-o`. Keep session persistence on. Use the session ID in the D5 shim log to select the
  parent rollout in `$CODEX_HOME/sessions/` for findings normalization. Its `event_msg` record has
  `payload.type = "item_completed"` and `payload.item.type = "ExitedReviewMode"`. The
  `payload.item.review_output` holds the findings with priority and file/line ranges.
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
threshold value. Checkpoint and resume stay as the safety net, and a
limit stops the run by default; waiting for the reset is opt-in (`--wait`).

Where each CLI reports its used share (spike #23, fixtures in `tests/fixtures/limits/`):
- **Codex CLI 0.171.0: yes, in the session rollout only.** `codex exec --json` stdout has no
  `rate_limits`. `event_msg` `token_count` events in `$CODEX_HOME/sessions/.../rollout-*.jsonl`
  can carry a `rate_limits` snapshot:
  - `limit_id`;
  - optional windows `primary` (seen as 300 minutes) and `secondary` (10080 minutes), each with
    `used_percent` (0 to 100, can exceed 100), `window_minutes` and an optional `resets_at` (epoch
    seconds).

  A snapshot can have both windows null: the stub runs show one.
- **Claude Code 2.1.316: yes, on stdout of stream-json only.** `claude -p --output-format
  stream-json --verbose` emits a `rate_limit_event` after the first API response:
  `rate_limit_info.unifiedWindows.five_hour` and `seven_day`, each with `utilization` (0 to 1, can
  exceed 1), `resetsAt` and `status`, plus a top-level `status`, `rateLimitType` and `utilization`
  for the limiting window. It is not in the session transcript. `/usage` under `-p` reports only the
  session's cost, and the statusline input (`rate_limits`) exists only in interactive sessions.

**Choice for Codex: read the trial's rollouts.** After a Codex trial ends, and before its container
is removed, the runner extracts the `rate_limits` observations from the trial's rollouts, with the
transcripts it keeps for D7. D3 and D5 do not change. The reading:
- *Per window:* the runner keeps one reading for each window of the `codex` limit: the newest
  observation where that window is present and `resets_at` has not passed. A later snapshot with
  the window null does not erase an earlier reading. Other `limit_id`s are recorded but not
  checked.
- *Expiry:* a reading expires at its `resets_at`. A window without `resets_at` expires N minutes
  after it was observed (as for Claude below).
- *Before the first Codex trial of a run:* there is no reading. The trial starts on the round
  budget alone, and the run log says the Codex share is unknown.
- *Expired reading or a window never reported:* the soft threshold is not checked for that window
  until a new reading arrives, and the run log says so.

**Choice for Claude: runner-owned readings with a throttled probe.** Engines own stdout (D7), so the
runner reads Claude's share only from stream-json output of its own calls:
- *Free readings:* first those from calls it makes anyway (the judge, any runner-owned
  `claude -p`). The validity rule under *Outcomes* applies to them too.
- *When it probes:* before a Claude trial starts and there is no reading yet, or the newest is
  older than N minutes (default 10, configurable), the runner makes one minimal probe call
  (`haiku`, "Reply OK"). Probes are throttled per login by the last attempt, successful or not: no
  second probe on that login within N minutes.
- *How the probe runs:* it is runner-owned and runs like a trial under D3 and D5: the managed
  policy, the benchmark login, the proxy and an empty workspace with no task input. The runner owns
  its stdout. Its tokens are run overhead, never attributed to a trial.
- *Outcomes:* a `rate_limit_event` refreshes the reading only when it carries a valid window
  utilization (a number in `unifiedWindows.five_hour` or `seven_day`). Any of these leaves the soft
  threshold unenforced for Claude for that check, and the run log says so (the fallback in Risks):
  - a failed probe;
  - a probe with no event;
  - an event without a valid utilization. A typed hard limit in the probe (D8: usage limit or terminal rate limit) goes to the limit
  handler like a trial's.

The probe was not run as a runner step in the spike; one equivalent manual call produced the event.

**Alternatives considered:**
- *The `claude` shim tees stdout and logs `rate_limit_event` lines.* It breaks D5's rule that the
  shim execs the real binary, and it sees the event only for engines that use stream-json.
- *Not enforcing the soft threshold for Claude.* The share is available at the cost of one small
  call per check, so this stays only as the fallback.

## Risks / Trade-offs

- **[Claude OAuth token returns 401 in containers (reported upstream).]** → Spiked (#19): not
  reproduced. A `claude setup-token` token passed by `--env-file` as `CLAUDE_CODE_OAUTH_TOKEN`
  authenticates `claude -p` in a Linux container on Docker Desktop (Claude Code 2.1.316,
  linux/arm64) with an empty `CLAUDE_CONFIG_DIR`, also with D5's managed settings and shim flags.
  D3 stands; the `sbx` and separate-user fallbacks are not needed. Credential source (decided): on
  macOS the operator keeps the token in the Keychain (generic password, service
  `rbench-claude-oauth`). rbench reads it at trial start and passes it to `docker` by variable name
  only (`-e CLAUDE_CODE_OAUTH_TOKEN`, value in the child's environment), so the value never appears
  on an argv, in a file in the run dir or in an image (configuration or layer). The env file the
  spike used (`<env-file>`, outside the repository, mode 600) is a fallback for the spike only. Not
  yet checked: whether the managed settings are enforced (the spike checked authentication only),
  the run through the proxy (D3's smoke run) and token expiry over a long run.
- **[Codex `auth.json` refresh races between processes.]** → One login file per trial, at most one
  Codex trial per login, and a login only the benchmark uses, so Orca and the operator's own Codex
  never share its refresh-token chain. The spike (#20, Codex CLI 0.171.0) ran 3 concurrent
  `codex exec` in one container on one directory-mounted login home, and all 3 finished. Then 3
  concurrent processes started on a copy whose access token had expired, and all 3 finished. The
  copy got new tokens, and a later refresh from them succeeded. Codex re-reads `auth.json` before
  it refreshes, and saves by compare-and-save under `auth.json.lock`. Two points are inferred, not
  observed:
  - that the 3 processes sent overlapping refresh requests (no request log was kept);
  - that the copy's refresh invalidated the original's refresh token. It is inferred from Codex's
    `refresh_token_reused` handling. No refresh from the original was tried; the original login
    was redone instead.

  So rbench never copies `auth.json`. There is one login file in a directory-mounted login home,
  and Codex refreshes it in place.
- **[Headless `codex-review` execution and capture.]** → Confirmed in #22 with Codex CLI 0.171.0
  on Linux arm64 only. `codex exec review` ran without a TTY, used D5's directory-mounted login
  and returned one structured finding on the changed line of a synthetic two-commit repository.
  The D3 proxy remains a smoke check.
- **[The inner Linux sandbox may fail inside Docker.]** → In #22 it could not create a user
  namespace. In the smoke stage, check review-council, which launches Codex with
  `--sandbox read-only` (D5), and the Codex-side plugin review engine in D13. D6 uses the
  container as the sandbox for rreview.
- **[Claude Code reports its used share only on stream-json stdout (spike #23).]** → The runner
  reads it from its own calls and a throttled probe (D14). When no reading is available, the soft
  threshold is not enforced for Claude for that check, the run log says so, and the round budget is
  the only stop.
- **[Codex's `rate_limits` lives in an undocumented rollout format, and Claude's
  `unifiedWindows` is marked internal in the CLI's schema.]** → Both may change with a CLI version.
  The parsers validate against the fixtures for the pinned versions, and an unreadable reading
  counts as no reading rather than as zero use.
- **[Limit hits were not provoked live.]** → No real limit was approached. The messages and typed
  codes come from the pinned CLIs against a local 429 stub and from their code; the real servers'
  headers and bodies are unverified.
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
- **[Transcript usage counts differ from CLI-reported usage.]** → The spike (#24) found the on-disk
  Claude transcript equal to `modelUsage` for a main session on one model. Subagents, side calls on
  another model, a nonzero Codex `cache_write_input_tokens` and nonzero reasoning tokens were not
  seen; the smoke stage checks them. A mismatch against a covering snapshot uses `modelUsage` and
  flags the trial; without one the cross-check is unavailable (D7).
- **[Transcript paths and schemas change between CLI versions.]** → A CLI upgrade reruns smoke
  checks for D7's paths and usage records and D13's `ExitedReviewMode` item and `review_output`
  layout. A session with no usable source flags the trial's usage as incomplete.
- **[Codex rollouts carry the ChatGPT account id (`session_meta`).]** → Transcripts stay in the run
  directory and are never published; only aggregate scores leave it.

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
    R3 from the front).
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
