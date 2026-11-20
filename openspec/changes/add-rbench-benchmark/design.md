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

### D1. Reuse ReviewBench; trial executor pending (see Open decisions, O3)
**Choice.** ReviewBench (MIT) supplies:
- the external task set (its 25-PR test subset first);
- the findings and golden format;
- the matcher (same underlying issue, many-to-many, grounded and augmented metrics).

ReviewBench is pinned to a commit.

**Alternatives considered:**
- *Martian code-review bench (MIT, 50 PRs).* Optional later, as a second external set.
- *Our own matcher.* More work, and it loses comparability.
- *Inspect AI.* A strong harness, but more glue than our ledger needs.

### D2. Python 3.12 with `uv`; one static HTML report
**Choice.** Python fits ReviewBench and Harbor tooling. The report is one HTML file with the results
embedded as JSON and plain JavaScript. It needs no server or build step, and opens from disk as the
spec requires.

### D3. Trial = one `docker compose` project: agent container + squid proxy
**Choice.**
- The agent container sits on an `internal: true` network. Its only route out is a squid proxy that
  allowlists the providers' API and auth hosts and logs every CONNECT. The host list comes from a
  logged smoke run and is fixed in `images/proxy/allowlist`.
- Images are built per engine version and CLI versions, not per task. The scrubbed repository is
  copied in with `docker cp` when the container is created, never mounted, and its hash is recorded.

**Alternatives considered:**
- *Anthropic's devcontainer firewall (iptables).* It allows GitHub and port 22, and resolves domains
  once.
- *Claude's or Codex's own sandboxes.* They read most of the host.
- *A separate macOS user.* Fragile, and others cannot reproduce it.
- *Docker Sandboxes (`sbx`).* Codex support and macOS 14 support are unverified; it is kept as a
  fallback.

### D4. Repository scrub
**Choice.**
- The repository is built on the host from the source repository: `base` is the PR's base tree,
  and `reviewed` is the head tree squashed onto it.
- A fresh `git init` has no remotes, reflog, tags or packed objects.
- A `gh` stub on `PATH` answers `pr view` and `pr diff` from `pr.json` and the repository, and fails
  on anything else.

### D5. CLI shims enforce the hermetic policy
**Choice.** Engines launch CLIs themselves, so `claude` and `codex` shims sit first on `PATH`. Each
shim:
- adds the policy flags: Claude `--strict-mcp-config` and denied `WebSearch`/`WebFetch`; Codex
  `--ignore-user-config --ignore-rules --strict-config -c web_search="disabled"`;
- logs the argv and session id;
- execs the real binary.

The shims never change prompt, model or effort.

The trial has one `CLAUDE_CONFIG_DIR` and one `CODEX_HOME`. All processes share them, so every Codex
process uses the one bind-mounted `auth.json`, and refresh-token rotation stays safe. The homes
hold:
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
- Claude: per-message usage;
- Codex: `turn.completed.usage`.

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
not judge them.

**Alternative considered:** ask each engine for JSON. That changes the engines being measured.

### D10. Staged matrix
**Choice.** Smoke, then screening, then full, as in the `engine-configurations` spec. The full
stage's configurations are picked by the maintainer from the screening Pareto front. A full
cartesian product of models, efforts and roles would exhaust the budget before giving 5 attempts
anywhere.

## Risks / Trade-offs

- **[Claude OAuth token returns 401 in containers (reported upstream).]** → First spike item. If it
  fails, try the `sbx` microVM, then run Claude on a separate host user with the proxy as fallback.
- **[Codex `auth.json` refresh races between processes.]** → One login file per trial, at most one
  Codex trial per login. The spike runs several Codex processes in one trial.
- **[Shim flags conflict with an engine's own flags.]** → The spike runs each engine's default once
  with the shims and checks the argv log.
- **[The judge prefers its own model family.]** → The judge is blind to the engine, and a second
  judge from the other family labels a sample (O1).
- **[Ground truth for our PRs is incomplete or produced by an engine under test.]** → Augmented
  precision, maintainer confirmation and producer marking (specs `review-tasks`, `review-scoring`).
- **[ReviewBench's matcher needs an API key.]** → Port its prompt (MIT) to run through the CLI, and
  record the deviation in the report.
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

- **O1. Judge model.**
  - Proposal: `claude-opus-5-5` at high effort.
  - A second judge, `gpt-6.2-sol`, labels a 20% sample.
  - The report shows the agreement between the two judges.
- **O2. Size of the full stage.** Proposal: up to 8 configurations from the screening Pareto front,
  plus each engine's default.
- **O3. Harbor or our own runner.**
  - D3, D5 and D8 already define the trial, the policy, the raw output and the ledger, so Harbor's
    runner adds little the design relies on.
  - Using Harbor costs a spike item and a second code path.
  - Proposal: our own `docker compose` runner from the start, with ReviewBench kept.
  - This changes the earlier choice of Harbor, so it needs a yes.
- **O4. Calibration of the judge against human labels.** Proposal: the maintainer labels the 5
  calibration PRs once, and the report shows judge-human agreement on them.
