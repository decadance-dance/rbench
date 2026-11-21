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
- Two networks. The agent container is only on an internal bridge (`internal: true`) in isolated
  gateway mode (`com.docker.network.bridge.gateway_mode_ipv4: isolated`, so the bridge has no host
  address). The proxy is on that bridge and on an ordinary one that reaches the internet. The
  agent's only route out is the squid proxy, which allowlists the providers' API and auth hosts and
  logs every CONNECT. The host list comes from a logged smoke run and is fixed in
  `images/proxy/allowlist`.
  - An internal bridge alone is not enough: in Docker's default gateway mode the bridge has a host
    address, and the agent can reach any host service listening on it. Isolated mode removes that
    address. Checked with Docker 29.4.1 (#21): a listener in the Docker host's network namespace
    answered an agent on a default internal bridge at the gateway address, and an agent on an
    isolated one reached no bridge address. On both, `host.docker.internal` and external names did
    not resolve. A deployment without isolated mode needs equivalent host isolation.
- The smoke run (#21, Claude Code 2.1.316, Codex CLI 0.171.0) fixed the allowlist at three hosts:
  - `api.anthropic.com`: the Claude API. `CLAUDE_CODE_OAUTH_TOKEN` needs no refresh host.
  - `chatgpt.com`: Codex's API and account calls under a ChatGPT login.
  - `auth.openai.com`: Codex's token refresh. The run did not reach it, because its token was
    fresh, but a trial does (#20).
- Squid allows `CONNECT` to port 443 of exactly these hosts, with no subdomains, and denies
  everything else, plain HTTP included. The ACL is `acl allowed dstdomain -n
  "/etc/squid/allowlist"`. Without `-n`, squid matches a numeric destination by its reverse DNS
  name, so `CONNECT <ip>:443` passes whenever the IP's PTR record names an allowed host. The
  spike's enforcing configuration lacked `-n`. A docker-only check (#21) showed the difference:
  with a test list naming `dns.google`, `CONNECT 8.8.8.8:443` tunnelled without `-n` and was
  refused with it.
- Both CLIs honour `HTTPS_PROXY`/`HTTP_PROXY`, set with an empty `NO_PROXY`. In the run, neither
  resolved any name but the proxy's.
- In the run, the proxy refused and logged three kinds of traffic, with no effect on either CLI's
  result:
  - Claude's telemetry (`http-intake.logs.us5.datadoghq.com`). With D5's non-essential-traffic
    switches set, one call made no such attempt.
  - Codex's metrics (`ab.chatgpt.com`).
  - Codex's fetches from OpenAI file storage (`*.oaiusercontent.com`).
- The proxy logs one line per request, allowed or refused: time, client, squid result and status,
  method, target, and the address it connected to. Its own health check is left out.
  `images/proxy` holds the squid configuration, the allowlist and the image that carries both.
- The contamination audit (#41) reads this log after the trial. A line for anything but `CONNECT`
  to port 443 of a listed host is a successful connection, which contaminates the trial, when
  squid connected upstream (an address, not `-`) or answered without a 4xx or 5xx status. Any
  other such line is a refused attempt: run health lists it from the trial's `audit.json`. A
  record that is not valid in every field contaminates the trial, as does any file of the trial
  directory the audit cannot read or that is not a regular file, and a proxy log, shim log or
  transcript root that is not inside the trial directory or is reached through a symlink. Run
  health gets only a request's destination (the CONNECT authority, or a URL's scheme, host and
  port), never its path or query, and known hidden values are redacted from what the audit
  records. The canary scan decodes JSON escapes in 256 KiB windows, so its memory per file is
  bounded whatever the length of a string. A contaminated trial's audit is final, and the audit's
  own `status.json` and `audit.json` are not evidence.
- The compose project's live test (#37) passes on Docker 29.4.1 with no credentials and no model
  call. Refused and logged: `CONNECT github.com:443`, a `CONNECT` to the numeric address of
  `api.anthropic.com`, and plain HTTP. `api.anthropic.com` tunnels. External names and
  `host.docker.internal` do not resolve. A direct connection to an address fails. A listener in
  the Docker host's network namespace answers the proxy on the egress bridge's gateway, but not the
  agent there or on the default bridge's gateway. The agent container has no mounts.
- Images are built per engine version and CLI versions, not per task. The scrubbed repository is
  unpacked from the task's archive (D11) and copied in with `docker cp` when the container is
  created, never mounted.
  - The agent image (`images/agent`) installs exactly the CLI versions an engine declares, and
    rbench reads them back from the built image. Its base images are pinned by digest, and its
    build context is `images/agent/` alone: the Dockerfile, the managed configuration, the
    shim scripts (D5) and the `gh` stub (D4).
  - Images are referenced as `<name>@sha256:<hex>`. A local build in Docker's classic image store
    has no registry digest, so the digest is the image ID: the content digest of the image's
    configuration, which names its layers. Trials run an image by that ID, never by a tag or a pull.
  - That ID pins one local build, not its source. Two `--no-cache` builds of the same Dockerfile
    and CLI versions get different IDs, because creation times differ (PR #109 review). A frozen
    plan therefore runs, and resumes, only on the images it recorded. If one is gone from the
    Docker host, the trial fails to start with an infrastructure error. A rebuild gives a new ID,
    so it needs a new run. Reuse (spec run-ledger) matches only trials that ran the same build.
- Each trial is a fresh compose project. Compose identifies a project by name and would adopt
  containers left under it, so starting a trial first removes whatever an earlier attempt left
  under that name. A start that fails removes the containers and networks it created, and the
  error names the trial.
  - The name therefore grants the authority to remove that project, a running one included. The
    executor (#55) gives each trial a name unique to the run and the trial on the Docker daemon,
    stable across its retries, and reuses a name only while it holds the run's lock. A trial id
    alone is unique only within its plan, so two runs could otherwise stop each other's trials.

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
- A trial copies the archive, which must be a regular file, to a private file under a byte
  limit, and reads only those bytes: their SHA-256 must match, and they must start with a git
  bundle header. A path git would also fetch from, such as
  a repository or a `.git` pointer file, could change after the check. The bundle is unpacked on
  the host into a fresh object store (`git init --template=`, every ref of the bundle, no reflog,
  no `FETCH_HEAD`, no work tree) with a fixed `config` and `HEAD`. Each host git step has a time
  limit, and a `reviewed` tree over a size limit is refused. The store is copied with the PR into
  the started agent with `docker compose cp --archive`, which makes the agent's user own it:
  `/rbench/repo`, `/rbench/pr.json` (number, title, description) and an empty `/rbench/output/`.
- The same inspection runs on the host copy and in the agent, before the engine starts. It reads
  the metadata as files before git interprets it: nothing in `.git` but `HEAD`, `config`, `index`,
  `objects` and `refs`, the fixed `config` (no remote, include or other setting) and `HEAD` (on
  `reviewed`), nothing in `objects/info` (no alternates). Then it requires exactly the branches
  `base` and `reviewed`, `reviewed`'s only parent `base`, and every stored object reachable from
  the two commits, counted by object id. Both copies must agree.
- The work tree is checked out in the agent (`git read-tree --reset -u`), not on the host: a
  case-insensitive host file system keeps one of `Foo` and `foo`, Linux keeps both. `git status`
  there must then report nothing. Any failure is an infrastructure error. The object count and
  both commit ids go to the trial's `repository.json`; the audit recounts with `count_objects`
  after the engine, and a changed count, or one it cannot read, contaminates the trial.
- A `gh` stub (`images/agent/gh`, Node, on `PATH`) answers `pr view` and `pr diff` from
  `pr.json` and the repository. `pr diff` is `git diff refs/heads/base refs/heads/reviewed --`,
  passed on as bytes, so a file named `base` or a non-UTF-8 line changes nothing. It reports the
  PR as `OPEN` with no comments or reviews, so engines that skip closed or reviewed PRs still run.
  Its `url` is `https://github.com/rbench-trial/repo/pull/<n>`, of no real repository, and its
  `mergeCommit` is null; rreview 0.1.1 asks for both when it resolves the PR (#52).
  `gh pr comment` writes its body to `/rbench/output/comment-<n>.md`. After the engine, the trial
  copies the regular files named so into `raw/pr-comments/`, which normalization reads like any
  other engine output. The stub opens no connection. Anything else fails with exit 1: another
  command, another PR, `-R`, `--jq`, an invalid `--color`, a repeated option and fields it does not have.

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
  - repeats the policy flags as a second layer: Claude `--strict-mcp-config` and
    `--disallowedTools=WebSearch,WebFetch`; Codex `--strict-config -c web_search="disabled"` before
    the subcommand and `--ignore-rules` right after `exec`;
  - adds the model and effort flags for engines whose adapter passes them by flag, and only when
    the engine's argv has none of its own (Claude `--model`, `--effort`; Codex
    `-c model="..."` and `-c model_reasoning_effort="..."` before the subcommand, which every
    session command takes);
  - logs the argv and what binds the launch to its sessions (below);
  - starts the real binary as its own child, with the engine's stdio unchanged, forwards signals to
    it and exits with its status, or dies by the signal that killed it;
  - reads the engine's options, and rewrites one (below), only before `--`, and for Codex only
    before the first positional argument (a prompt or a session id). Text after them is the
    prompt and is never read as an option.
- Shim settings and log. Before the engine starts, the runner writes one settings file at a fixed
  path in the container, read-only to every trial process. It holds only the trial's public launch
  settings (the policy flags, and the model and effort for roles delivered by flag) and the log
  destination; no credential and no task data. The log destination is a collector the runner
  starts in the container under another user, reached through a Unix socket. It stamps each record
  with the sender's process id from the socket's peer credentials (`SO_PEERCRED`) and appends it to
  a file that trial processes cannot write. An engine can add records but cannot change or remove
  recorded ones. Fixed paths in the image (#38): the settings
  file `/etc/rbench/shim.json`, written by root with mode `0444`; the collector
  `/opt/rbench/lib/collector.py`, started by `docker exec --user rbench-collector`, its socket
  `/run/rbench/collector.sock` and its log `/var/log/rbench/shim.jsonl` (mode `0600`). A shim
  without its settings or its collector refuses to start the CLI. Every exchange with the collector
  has a 10-second limit: a collector that does not accept the launch in time counts as absent.
  Once the CLI runs, a record lost to the limit leaves its sessions unbound, and a watcher still
  running 10 seconds after the CLI exits is killed, which leaves the rollouts it had not reported
  unbound; the trial fails closed and the shim never hangs. The shim still exits with the CLI's
  status. The shims, the watcher, the
  collector and the hook are Python scripts on the image's Debian `python3`, because Node has no
  `SO_PEERCRED`.
- Threat model of the audit (decided). The audit detects a CLI session started without the shims:
  an engine, or a model's shell command, that runs the real binary by path, by accident or on
  purpose. It does not defend against a process inside the container that forges the shims' or
  hooks' records; that is out of scope (Risks). Within this model:
  - The shim is the launcher. It registers with the collector, starts the CLI as its own child, and
    records that child's process id from its own `fork`, not from a value a peer reports. Its
    watcher is a second child of the shim.
  - The collector accepts launch and binding records only from the shim and watcher pair it
    registered for that launch: a connection that opens with a launch record registers it, and a
    watcher is accepted only while its parent process (`/proc/<pid>/stat`) is a shim whose launch
    connection is open. It accepts hook records from any process, because a hook record of
    an unregistered CLI process is the evidence of a bypass.
- Session binding. Each root session in the trial's transcripts must have exactly one origin launch:
  the registered shim launch that started it. It may also have any number of resume launches, each a
  registered shim launch that reopened the same session. rreview's wrap-up, which resumes a pass
  (Claude `--resume`, Codex `exec resume`), is the known user of resumes. A resume never stands in
  for the origin: a session with resumes but no origin marks the trial contaminated.
  - Claude: a SessionStart hook in the managed settings logs each session Claude Code actually
    starts: its `session_id`, its `source` (`startup`, `resume` or `fork`) and the process id of the
    `claude` process it runs under: its nearest ancestor that runs the real binary. The audit joins
    that process id, with the process's start time so that a reused pid cannot match, with the
    child process ids the shims recorded. `startup`, `fork` and `clear` make the launch the
    session's origin, and `resume` and `compact` a resume launch of the same id. The hook runs for sessions started without the shim too. Checked offline
    with Claude Code 2.1.316 (#25): it reported the real id for a fresh session, `--continue`,
    `--resume` and `--resume --fork-session`, and it still ran with `--safe-mode`,
    `--setting-sources user` and `disableAllHooks` set in the user settings, in `--settings` and in
    the project's `.claude/settings.json`. Subagent transcripts lie under their parent session
    (`<session>/subagents/`) and are bound through it.
  - Codex: Codex has no option to set the session id, and the id appears only in the output, in a
    form that depends on the output mode. The shim's watcher polls the open files of the CLI child
    the shim started, and of that child's descendants. It binds a rollout file
    (`sessions/**/rollout-*.jsonl`) only while the CLI holds it open for writing: the flags in
    `/proc/<pid>/fdinfo` must include write or read-write access and append. A file that is only
    read, such as by Codex's metadata reader for existing rollouts, binds nothing. The watcher logs
    the thread id from the rollout's `session_meta`. Whether a launch is an origin or a resume
    comes from its own argv, never from the order of bindings: a launch with `resume` among its
    arguments before `--` (`codex resume`, `codex exec resume`) is a resume launch of every root
    rollout it binds, which it reopens for append without writing a new `session_meta`. Any other
    launch that binds a root rollout is its origin. A rollout bound only by resume launches, such
    as a session the real binary started and a shim launch resumed, has no origin. A `resume`
    that is a prompt or an option's value makes a fresh session look like a resume, which fails
    closed. Checked offline with Codex CLI
    0.171.0 (#25): `codex exec` keeps its rollout open for read-write and append (`O_RDWR |
    O_APPEND`) for the whole session, `codex exec resume <id>` reopens the same file the same way,
    and `codex exec review` holds both the parent rollout and the review thread's rollout in the
    same process. A child thread is bound when the same launch opened it for writing and its
    `parent_thread_id` names a root thread of that launch.
    A rollout whose session_meta names no `parent_thread_id` is a root thread, and binds like one.
    `session_id` and `thread_source` alone are not trusted. If a session ends between two polls,
    its rollout is unbound, and the trial fails closed (see the audit).
  - Rejected: matching rollouts to launches by `CODEX_HOME`, working directory and time. That only
    finds candidates: two root rollouts after one launch in the same home both match. Also
    rejected: a per-launch `CODEX_INTERNAL_ORIGINATOR_OVERRIDE`. It lands in
    `session_meta.originator` (checked offline). But it is an internal variable that names the
    client to the provider, not a logging field, so setting it could change the request. Reading
    the id from `thread.started` or from the `session id:` line on stderr would put the shim into
    the engine's stdio, and it depends on the output mode.
- The audit discovers transcripts on its own: every Claude transcript (`projects/**/*.jsonl`) and
  every Codex rollout anywhere in the trial container's writable filesystem, not only in the homes
  the shim log names. The runner lists the files `docker diff` reports as added or changed in the
  agent container and copies those out with `docker cp`, which reads them whatever their owner. Any of these marks the trial contaminated: a session with no origin launch,
  a session with two origin launches, a hook record whose `claude` process no shim started, a
  child thread whose lineage does not reach a bound root, or a launch with no exit record in the
  log, whose other records may then be incomplete too.
- An engine's own model and effort must equal the planned role's. The adapter builds the engine's
  options from the plan: rreview's `--role-model` and `--role-effort`, and `codex-review`'s `-m`,
  `review_model` and `model_reasoning_effort`. The audit reads the model and effort that applied to
  each session and child thread, as the CLI reports them. That includes Claude subagents, and
  `codex-review`'s review thread, which runs `review_model`. A model mismatch makes the trial
  `misconfigured`, and an effort drift flags it (spec `engine-configurations`, Applied model and
  effort are recorded). In #25, review-council's Codex rollout showed the model and effort of the
  trial's `config.toml`; the other roles' applied values were not compared.
- Shim merge against each engine's own launches, checked in spike #25 (Claude Code 2.1.316, Codex
  CLI 0.171.0, linux/arm64, one run of each engine's default: `plain` on both CLIs, `code-review`,
  `codex-review`, review-council 0.14.0, rreview 0.1.1 with `--sandbox external`, and the plugin
  review engine 5.0.0 on both CLIs). All runs completed, and every session in their transcripts
  matched the prototype's log. Exercised: fresh launches only, one model per CLI
  (`claude-sonnet-5-5`, `gpt-6.2-astra`) at effort `low`, plus one review-council rerun at `medium`,
  no proxy. At `low`, review-council did not dispatch its reviewers. The rreview judge and its
  wrap-up resume never ran. Not exercised: continuations and forks (checked only offline, above),
  the D3 proxy, and other models and efforts. Conflicts found and their fixes:
  - *Variadic `--disallowedTools`.* `--disallowedTools WebSearch WebFetch` also consumes a prompt
    passed as a positional argument after it, and `claude -p` then fails with "Input must be
    provided". Fix: one token, `--disallowedTools=WebSearch,WebFetch`.
  - *`--ignore-rules` is an `exec` option.* Codex rejects it before `exec` ("unexpected argument"),
    and the top-level `codex review` does not accept it. Fix: the shim puts `--strict-config` and
    `-c` before the subcommand and inserts `--ignore-rules` right after `exec` (or `e`), where
    `codex exec review` also takes it. Top-level `codex review`, `resume`, `fork` and the TUI get no
    `--ignore-rules`; no D13 engine launches them.
  - *Non-session commands.* `--strict-config` is refused by subcommands such as `codex features`,
    and engines also call `--version`, `codex plugin` and `codex debug`. The shim adds flags only to
    session launches (Claude: anything but `--version`, `--help` and management subcommands;
    Codex: `exec`, `review`, `resume`, `fork` and the TUI). It logs the other calls unchanged.
    Every `-c` key that the engines and D12 pass loads under `--strict-config`. This was checked
    offline, where an unknown key fails at startup.
  - *Model and effort the engine sets itself.* `codex-review` passes `-m` and
    `model_reasoning_effort`, and rreview passes `--model`/`--effort` and `-m`/`-c` for each role.
    A second value from the shim would duplicate them, so the shim adds model or effort only
    when the argv has none. review-council's Codex side got the model and effort of the trial's
    `config.toml`.
  - *An engine's own `--sandbox read-only` (decided).* review-council 0.14.0 runs its Codex
    reviewer with `--sandbox read-only`. Codex's Linux sandbox needs user namespaces, and in the
    container bubblewrap cannot create them (`codex sandbox linux -- true` fails). The deprecated
    `use_legacy_landlock` feature is no fallback: it aborts with "filesystem-restricted execution
    requires bubblewrap". The Codex shim therefore rewrites `-s`/`--sandbox read-only` to
    `danger-full-access`, so the container is the sandbox, as in D6. It is the only engine flag a
    shim overrides. The shim log keeps the original argv and names the rewrite. The reviewer can
    then write in the container (see Risks).
  - *Shim settings from the environment.* rreview gives its reviewers only an allowlist of
    environment variables, so a log path or model passed to the shim by variable does not reach
    its sessions. The shim reads the fixed settings file above.
  - *`--session-id` with continuations.* Claude Code refuses `--session-id` together with
    `--continue` or `--resume` unless `--fork-session` is also given ("--session-id can only be used
    with --continue or --resume if --fork-session is also specified", checked offline). The shim
    adds `--session-id <uuid>` only to a fresh launch, and to a fork (`--fork-session` with a
    continuation). A launch is a continuation if it has `-c`/`--continue`, `-r`/`--resume` (also as
    `--resume=<value>`) or `--from-pr`. It adds no id when the engine passes its own `--session-id`
    (also as `--session-id=<uuid>`): rreview 0.1.1 passes one on every fresh Claude launch and uses
    `--resume` for its wrap-up. A continuation keeps the resumed session's id. That id comes from
    the SessionStart hook, because the argv may hold a name, a PR or nothing (the latest session).
  - *Other Codex homes.* rreview gives each Codex pass its own `CODEX_HOME` under its run
    directory, with `auth.json` a symlink straight to the login home (rreview resolves the trial's
    symlink first). The audit finds them by its own discovery.
  - *Plugins at the pin.* Claude Code loads the engines' plugins with `--plugin-dir` from the
    pinned sources in the image. The plugin review engine's own Codex marketplace installs from its
    repository's `main`, not from the pin. The image therefore ships a local Codex marketplace whose
    entry is a relative path to the pinned plugin, and the trial installs it into `CODEX_HOME` at
    start without network access.
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
files. It holds nothing unrelated; files left by `codex login` (logs, `tmp/`) are removed. The login
home used in spike #21 still held `auth.lock`, `log/` and `tmp/`. rbench resolves the login home's
symlinks when it creates the trial and mounts that resolved path. Right before each start of the
trial's project, it refuses a login home that is not a directory, lies inside the checkout, has no
regular `auth.json`, holds any other entry (a symlink or a directory under an allowed name
included; every such entry is named), or no longer resolves to the mounted path (#40). It is
mounted as a directory, read-write, at `/rbench/codex-login` in the agent container (its only
mount). The bind never creates a missing source, and a literal `$` in its path is escaped, because
Compose interpolates the file. When the trial is prepared, the trial's user must read `auth.json`,
open `auth.json` for writing (Codex does before it replaces the file), create a file in the login
home and open an existing `auth.json.lock` for writing, or the trial stops with the reason. Only then is the trial's `CODEX_HOME/auth.json` made a symlink to the
`auth.json` in it. Mounting the file alone does not work: Codex 0.171.0
replaces `auth.json` by a temporary file and a rename in the target's directory, and a rename onto
a mount point fails with `EBUSY`. Codex follows the symlink, so the temporary file and its
`auth.json.lock` both land in the login home. Refreshed tokens therefore go back only to that home.
A docker-only check (#40) repeated that save through the symlink with a stand-in login: only the
login home changed, and no copy of `auth.json` appeared in the container. Sessions, logs and state
stay in the trial's `CODEX_HOME` inside the container.

The Claude token reaches the agent container by variable name only. The trial's compose file lists
`CLAUDE_CODE_OAUTH_TOKEN` in the agent's `environment` without a value, so Compose takes it from
its own environment, and rbench sets it only in the environment of `docker compose up`. A value the
operator's shell exports is dropped from the environment of every docker call rbench makes, so a
trial gets only the token rbench read for it. The value is never in an argv, the compose file or
another file in the run directory. Docker's diagnostics and the output of commands rbench runs in
the agent have it redacted. A transcript whose path holds the token stops the collection before
anything is copied. A failed Keychain read reports the item, the exit status and a hint,
never the command's output.

The trial homes hold:
- the policy settings;
- `ENABLE_CLAUDEAI_MCP_SERVERS=false`;
- Codex's ChatGPT apps off for every Codex engine (#104), verified before any Codex trial is scored;
- auto-memory and background tasks off, non-essential traffic off;
- telemetry, error reporting and the autoupdater off;
- `enableWeakerNestedSandbox`;
- the engine's declared plugins.

`CLAUDE_CODE_EFFORT_LEVEL` is unset. The reviewed repository's `CLAUDE.md` and `AGENTS.md` stay.

The Claude switches above are the `env` of the managed settings, with `autoMemoryEnabled: false`,
the `WebSearch` and `WebFetch` deny rules and the SessionStart hook, so they hold below the shims.
The homes are `/home/node/.claude` and `/home/node/.codex`, the CLIs' default paths too, created
empty in the image; the runner writes the engine's `config.toml` and `settings.json` into them. The
managed files' directories must be readable by the trial's user: Claude Code 2.1.316 that cannot
read `/etc/claude-code` runs without its managed settings and only warns on stderr. Checked live
with Docker only (#38): both CLIs, run through the shims and by absolute path, sent their requests
to a local recording endpoint in the container. No request carried `WebSearch`, `WebFetch` or
Codex's `web_search` tool, also with `codex --search -c web_search="live"`; with the managed files
hidden, the same calls carried all three. Codex ran the model set only in the trial's
`config.toml`, and only the sessions started by path failed to bind.

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

As implemented (#58, `rbench.trial.usage`):
- One trial's usage is counted from one collection (shim log and transcripts). The normalization
  step runs in its own project and counts its own collection; `usage.json` keeps both apart, each
  with its list of reasons for being incomplete (empty when complete). A `usage.json` without that
  list, from before #58, is refused on read and on reuse: the trial is re-run.
- The expected sessions are those of the session launches: the launches the shims added flags to.
  Each must have a session in the log (a hook or a binding), and each such session must have usage
  of its own: a Claude session with a counted message, a Codex thread credited with usage.
- A record is counted once across all transcripts of the trial (Claude by `message.id`, Codex by
  thread, turn and response id). A repeat with other counters keeps the larger of each, the final
  figure of a streamed message.
- What the pin persists (Codex CLI 0.171.0 source): a `token_usage_record` per response, with
  `payload.{thread_id, turn_id, response_id}` and that response's `payload.usage`, next to cumulative
  turn and thread fields that are not read. Resume and fork restore the earlier history, so a fork's
  rollout can hold its parent's records and cumulative totals. Each record is credited to the thread
  it names, wherever it is written; a thread with no record anywhere falls back to its own rollout's
  `token_count`, and a fork never does. A fork holding only its parent's copied history therefore
  has no usage of its own and flags the trial.
- The pin's rollout persistence drops `ModelReroute` and `Warning`, so a rollout cannot show the
  effective model. Its `exec --json` stdout shows a reroute as an `item.completed` error item whose
  message starts with `model rerouted: `, but only for the primary thread and turn: `exec` filters
  the others out. A Codex thread's usage therefore has a verified model only when a captured stdout
  that reads whole (present, not empty, every line a JSON object without a repeated key) and has
  no reroute item shows that thread in `thread.started`. Any other thread with usage, a child
  thread included, flags the trial's model attribution as unverified, and so does a launch whose
  stdout does not read whole or shows a reroute.
- Every `turn_context` must name a turn and a model. Two models for one turn, in one rollout or
  across rollouts, leave that turn's records uncounted and flagged; in the cumulative fallback they
  count as two models. A `token_count` whose `info` is null is a rate-limit update and is skipped;
  any other non-object `info` makes the fallback malformed, at its file and line.
- The stdout fallback's verified model is the one model that every launch of the thread names on its
  argv (`-m`, `--model`, `-c model=`); a model set only in `config.toml` flags the trial. An
  all-zero `turn.completed.usage`, the pin's default when no token count arrived, is no usage. The
  runner passes the stdout it captured per launch; the shims never capture it.
- Counters are integers from 0 below 2**53, and so are the totals; the ledger's `store_results`
  and `read_results` hold stored counts to the same bound. An optional counter that is
  absent or null is 0. A line that is not valid JSON, a repeated key included, flags the usage
  incomplete at its file and line. Any other invalid value or a malformed `turn_context`,
  `token_usage_record` or assistant message rejects the usage, naming the file and line; in a
  fallback source it only flags the usage incomplete.

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

- An engine declaration says `output = "markdown"` or `"structured"` (the default). Only a
  Markdown engine's report goes through this step.
- The normalizer runs in the engine's own image, so a Markdown engine must pin `cli.claude`, and
  its declaration is rejected without one.
- A Markdown configuration's trials also spend on Claude, the normalizer's provider. The round's
  frozen budget therefore covers Claude even when every role runs on Codex, and loading the plan
  checks that. Engine estimates still use only the roles' providers. The default normalization
  estimate applies to Markdown configurations only.
- The pin is `claude-sonnet-5-5` at effort `medium`, with prompt version 1
  (`src/rbench/engines/normalization-prompt-v1.md`). Medium, not low: the copy must be complete,
  and the step's tokens are counted apart from the engine's anyway. A Markdown configuration's
  settings hold `normalization: {model, effort, prompt_version, prompt_sha256}`, where the hash
  covers the prompt and its output schema. Changing any of them gives new configuration ids, so
  earlier trials are not reused. A structured engine's settings and ids do not change.
  - A run frozen with another normalizer pin refuses to resume and names the field.
  - A plan written before the output kind existed loads with every engine structured.
- The launch is `claude -p --model <model> --effort <effort> --tools "" --output-format json
  --json-schema <schema>`. It runs through the shim in a fresh compose project of the agent image,
  prepared with D5's homes and shim settings, with no repository copied in. The prompt and the
  report go by stdin, never on the command line. The report sits between two marker lines that
  name its hash, as data that gives no instructions. A blank report has no findings and runs no
  model. The caller owns the project, as for a trial: it starts the project, collects its shim log
  and transcripts, and removes it.
- The answer is `{findings: [{file, start_line, end_line, message, severity}], unlocated:
  [{text}]}`. `severity` is the engine's own label, or null when the engine gives none. One bad
  item rejects the whole result, and the error names the trial and the item. Structured output
  goes through the same validation, with no model step.
- Items the report gives without a file or a line are not findings: spec `engine-configurations`
  (Findings normalization) requires both. Their count and text stay in the normalization record
  under `raw/`, next to the pin, so the loss is visible. The smoke stage (#60) compares the number
  of copied items with the report.
- Tokens come from the `modelUsage` of the result. That differs from D7's transcripts, because
  rbench owns this process's stdout, and its single result covers the whole session. The
  pinned model must appear in `modelUsage`: a session that ran another model is rejected. Side calls
  on other models are counted too. They go to the trial's `usage.json` under `normalization`, apart
  from `engine`. The fields of the result (`structured_output`, `modelUsage` and its
  `inputTokens`, `outputTokens`, `cacheReadInputTokens` and `cacheCreationInputTokens`) are those
  of Claude Code 2.1.316. The smoke stage checks them against a live call.

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
- Claude: `claude -p --tools Read,Grep,Glob,Bash --permission-mode dontAsk --allowedTools Bash
  --output-format json --json-schema <schema>`. `--tools` limits the built-in tools; `-p` denies
  a call that asks for permission, and Bash asks, so `--allowedTools Bash` approves the shell
  and `dontAsk` keeps denying every other call that would ask, instead of approving all of them
  as `bypassPermissions` would. The container is the sandbox (as in D6). `dontAsk` is a mode of
  the pinned Claude Code 2.1.316 (`claude --help`). The findings are the result's
  `structured_output`.
- Codex: `codex exec --output-schema <schema> -c web_search=disabled -c features.goals=false
  -c features.view_image=false -c features.image_generation=false -c features.apps=false
  -c agents.enabled=false -c tools.experimental_request_user_input.enabled=false
  -c model_catalog_json=<catalog>`, with `--sandbox danger-full-access` and
  `--output-last-message <file>`, whose content is the findings; the container is the sandbox
  (as in D6).
  - `agents.enabled=false` turns off the subagent tools. That holds because the trial's config
    is clean: an explicit `features.multi_agent_v2=true` would take precedence over it.
  - `features.apps=false` turns off the apps connectors and the plugin-install suggestion, which
    needs apps.
  - `tools.experimental_request_user_input.enabled=false` drops `request_user_input`, which
    Codex otherwise advertises even when no one can answer it.
  - The adapter (#47) checks these switches in the argv log.
- Some tools come only from the model catalog, and Codex has no flag for them. The benchmark's
  catalog is built from a frozen input: `codex debug models --bundled` of the pinned release,
  the catalog shipped in that binary. Plain `codex debug models` fetches the remote catalog, which
  can change under the same CLI version: in #26 it differed from the bundled one, for example in
  `gpt-6.2-astra`'s default service tier. For each Codex model in the matrix (D13) the benchmark
  overrides exactly these fields and changes nothing else:
  - `apply_patch_tool_type = null`: no `apply_patch` edit tool;
  - `tool_mode = "direct"`: tools are sent as plain functions. The catalog's `code_mode_only`
    puts them behind a JavaScript `exec` runner, and a model's `tool_mode` beats the
    `code_mode` features;
  - `experimental_supported_tools = []`: no clock, sleep or asynchronous messaging tools.

  The catalog is generated per CLI version and committed as
  `engines/plain/codex-catalog-<version>.json`, so planning hashes it without Docker and
  `rbench plan` gives the same ids as the run. `python -m rbench.engines.catalog --image <agent
  image>` writes it from the real CLI in the agent image, with no network and no login (#47 did
  so for 0.171.0). The hash of the transformed file that the trial opens is part of the
  configuration's settings.
- Checked with Codex CLI 0.167.0 and Claude Code against a local endpoint that records requests:
  - Codex by default sends `apply_patch`, `exec_command`, `write_stdin`, `view_image`,
    `web_search`, the goal tools, `tool_search` and `request_user_input`. With the 0.167.0
    options (only `apply_patch_tool_type` in the catalog, no `image_generation`, `apps`,
    `agents` or `request_user_input` switches) it sent only `exec_command`, `write_stdin`,
    `tool_search` and `request_user_input`; the rest of the request was unchanged.
  - Claude with `--tools Read,Grep,Glob,Bash` sends only those four tools.
- Checked in spike #26 with Codex CLI 0.171.0, the benchmark's ChatGPT login, `gpt-6.2-astra`
  at `low` effort and no D3 proxy. Both calls used a catalog built from the remote snapshot, not
  from `--bundled`.
  - The tool list was read from the request body that Codex's rollout trace records
    (`CODEX_ROLLOUT_TRACE_ROOT`). The session rollout has no tool list in this version. Under
    this login the tools travel in an `additional_tools` input item, not in a top-level `tools`
    field.
  - The remote catalog does not override `model_catalog_json`. When it is set, Codex uses that
    catalog as a static one and never refreshes it, and no `apply_patch` was sent.
  - The 0.171.0 catalog, remote and bundled alike, gives `gpt-6.2-sol` and `gpt-6.2-astra`
    `tool_mode = "code_mode_only"`, multi-agent v2 and experimental tools. With only
    `apply_patch_tool_type` nulled, Codex sent:
    - the `exec` runner. Its description advertised nested `exec_command`, `write_stdin`, MCP
      resource tools, `request_plugin_install`, a clock and `image_gen`, and said that more
      deferred nested tools are listed only at run time;
    - the subagent tools (`spawn_agent` and five more);
    - `wait`, `sleep`, `request_user_input` and two asynchronous messaging tools.
  - With the catalog fields above and the switches except the `request_user_input` one, it sent
    only `exec_command`, `write_stdin` and `request_user_input`. `tool_search` is gone: Codex
    offers it only when a deferred searchable tool exists, and with apps off there is none.
  - The intended request, with `request_user_input` also off, is `exec_command` and
    `write_stdin` only. That rests on the 0.171.0 source (the config gate's test in
    `codex-rs/core/src/tools/spec_plan_tests.rs`), not on a live call; the smoke stage verifies
    it.
  - Not verified: `gpt-6.2-sol`'s request and base instructions (only its catalog fields were
    read), other models' base instructions, the `--bundled` catalog and the run through the
    proxy. The smoke stage reads the request-body tool list for both Codex matrix models through
    the proxy (Risks).
- Model and effort come from the matrix, and the hermetic shims (D5) apply as for every engine.
- Settings (#47): the engine's version is the prompt's SHA-256 (`engines/plain.toml` pins it, so
  editing the prompt needs a new pin), and `inputs` holds the findings schema's hash and, for a
  Codex model, the catalog's. The prompt goes on stdin. Before a launch the adapter rehashes the
  three files and refuses any that differ from the frozen plan, so a run never mixes trials of two
  prompts, schemas or catalogs. The answer is validated against the schema by rbench itself; an
  answer off the schema, or one whose objects repeat a key, makes the trial `failed`, and the
  error names the place and the rule, never the answer's text. A reviewer runs one model:
  planning refuses a `models` set for `plain` (`model = [...]` plans one configuration per
  model), and the plan reader accepts `inputs` only on `plain`, with exactly its provider's keys.
  The argv log check requires the schema, the catalog and every switch in the argv the CLI got.

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

- `code-review` runs Claude Code's built-in `/code-review` on the PR from the `reviewed` checkout
  (#48). The command goes by stdin, `/code-review <PR number>\n`, to
  `claude -p --permission-mode bypassPermissions --output-format json`, and the D5 shim adds the
  model, the effort, the policy flags and the session id. The engine is pinned by the Claude Code
  version (pin kind `cli`) and outputs Markdown: the result's `result` text goes through D9.
  Checked offline with Claude Code 2.1.316, against a local recording endpoint with every other
  connection denied and no model call:
  - From stdin in `-p`, the command expands inline in the root session (no fork) to a prompt with
    "Review target: `<n>`". That fixes the target only: no model ran, so which tools the review
    then calls was not observed. If it reads the PR with `gh`, it gets the D4 stub, which answers
    the local `gh pr view` and `gh pr diff` forms, rejects other options and fields, and has no
    network path (its own tests, not this check, establish that).
  - The request carries the session's `--model` and `--effort`. With no level typed after the
    command, the review runs at the session's effort, so the adapter types none. A typed level
    would also be saved in the home and reused by later reviews.
  - With JSON output it reports in prose, not through its findings tool.
  - An API error still ends with `subtype` `success` but an empty `modelUsage`, so a result with
    no model usage fails the trial.
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
  `-o`. Keep session persistence on. Use the root thread id the D5 audit bound to this launch to
  select the parent rollout in `$CODEX_HOME/sessions/` for findings normalization. Its `event_msg`
  record has
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
  - Pin: release 0.1.1, tag `0.1.1` at commit `f71d6e171bf47312fd6e564b6cc6a3dcc2d1fe94`, which
    has `--sandbox external`. The agent image builds that commit with `cargo install --locked` in
    a build stage and copies only the binary, with `zsh`, which rreview needs for a Claude pass
    (#52).
  - `engines/rreview.toml` declares the roles `panel` (Codex), `coordinator` (Claude) and `judge`
    (Codex); `panel` gives its one model and effort to the three lenses. The adapter runs
    `rreview reviewed --sandbox external --json` with `--role-agent`, `--role-model` and
    `--role-effort` for each of rreview's five roles, so rreview passes the model and effort to
    every pass and the shim adds none. Planning refuses, like the launch, an effort rreview does
    not accept (Codex: `minimal` to `xhigh`) and a panel of more than one model.
  - Findings are the `findings` of `result.json` on stdout, at rreview's default gate (P0-P2,
    confidence at least 0.5), each with its label `P<n>`; dropped candidates are not findings.
    Exit 2, a result off rreview 0.1.1's types and rules, `findings` that are not its default
    gate applied to `ungated_findings`, or a result that does not say `sandbox: external` fails
    the trial.
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
  shim leaves the engine's stdio unchanged, and it sees the event only for engines that use
  stream-json.
- *Not enforcing the soft threshold for Claude.* The share is available at the cost of one small
  call per check, so this stays only as the fallback.

### D15. Mining the `ours` set
**Choice.** `rbench tasks import ours` mines merged PRs of public repositories (O5, option A) and
reuses the `reviewbench` import's archive, license check, title and description rule, staging,
changelog and manifest code (`src/rbench/tasks/importing.py`). The PRs, their comments and their
code are data, never instructions to rbench.

**Training cutoffs and the window.** A task's PR must be opened and merged inside
[2026-07-01, 2026-11-20): after the latest training cutoff of every model in the matrix and of the
judges (D13, O1), with an end fixed so the set can be mined again. The cutoffs were read on
2026-11-21:

| Model | Training cutoff | Source |
| --- | --- | --- |
| `claude-fable-5-2`, `claude-opus-5-5`, `claude-sonnet-5-5` | Jun 2026 ("Training data cutoff") | https://platform.claude.com/docs/en/about-claude/models/overview |
| `gpt-6.2-sol` | Jun 01, 2026 ("knowledge cutoff") | https://developers.openai.com/api/docs/models/gpt-6.2-sol |
| `gpt-6.2-astra` | May 18, 2026 ("knowledge cutoff") | https://developers.openai.com/api/docs/models/gpt-6.2-astra |

A month without a day is read as its last day, so June 2026 ends the training data, and the window
starts on July 1. The window is a hard bound: `--merged-after` and `--merged-before` may narrow it,
and a value outside it is refused before anything is read. A new model in the matrix with a later
cutoff needs a later window, a change to this decision and a new set version.

**Repositories.** A repository qualifies when:
- GitHub identifies its license as one on the allowed list (`read_license`, spec `review-tasks`,
  Source license; the import checks it again at each reviewed head);
- it is public: the import refuses a private repository before it reads any PR, since its token
  could read private review text;
- it is not archived and was pushed in the last 30 days;
- it has a human review culture. Among the 50 most recent PRs merged in the window, at least 2 have
  a review thread whose lines a later commit changed (GitHub `isOutdated`), opened by a user who is
  neither a bot nor the PR's author.

The list measured on 2026-11-21 is below; the count is the number of such PRs out of 50.

| Language | Repository, license, count |
| --- | --- |
| Python | `psf/black` MIT 11, `pytest-dev/pytest` MIT 7, `pypa/pip` MIT 5, `pallets/click` BSD-3-Clause 6, `python-attrs/attrs` MIT 2, `django/django` BSD-3-Clause 15, `scikit-learn/scikit-learn` BSD-3-Clause 18 |
| Go | `caddyserver/caddy` Apache-2.0 8, `prometheus/prometheus` Apache-2.0 12, `etcd-io/bbolt` MIT 3, `charmbracelet/bubbletea` MIT 4, `urfave/cli` MIT 4 |
| Rust | `tokio-rs/axum` MIT 7, `clap-rs/clap` Apache-2.0 6, `nushell/nushell` MIT 13 |
| JavaScript | `expressjs/express` MIT 4, `fastify/fastify` MIT 4 |

Not taken:
- `jqlang/jq` and `python/mypy`: GitHub reports `NOASSERTION`.
- `fastapi/fastapi`, `aio-libs/aiohttp` and `tox-dev/tox`: no such PR.
- `cli/cli` and `junegunn/fzf`: their review threads came mostly from bots.

Version 1 of the set (mined on 2026-11-21) has 32 tasks and 49 items from 13 of the 17
repositories. `pallets/click`, `django/django`, `urfave/cli` and `clap-rs/clap` gave none: their
candidate PRs were rewritten after the review (the reviewed head is not among the PR's commits),
changed their title or description inside the review window, or, for one `django/django` PR,
GitHub did not identify the license at the reviewed head. The changelog names every skipped PR
and its reason.

**PRs.** In each repository the candidates are the PRs opened and merged inside the window by a
human author. They are taken in merge order until 3 of them make tasks; `--max-per-repo` may lower
that cap, and a value above 3 is refused before anything is read. A PR makes a task when all of
these hold:
- it changes at most 1500 lines in at most 30 files;
- it has at least one item (below);
- its title and description pass D1's rule;
- its license at the reviewed head passes the check.

These checks run before the PR takes one of its repository's 3 places or a row of the label sheet,
so a refused PR never crowds out an eligible one; the changelog names every refused PR that had
items.

The *reviewed head* is the commit of the PR's first review that has inline comments and comes from
a human other than the author. That is the state before the review's follow-ups, as ReviewBench's
head is. The *base* is the merge base of the PR's base commit and the reviewed head. When the
reviewed head is no longer among the PR's commits (the branch was rewritten after the review), the
follow-ups cannot be ordered: the PR is skipped and the changelog names it. The same holds when the
PR's head does not lead back to the reviewed head by first parents within the listed commits.

**Reading of O5.** O5 takes ground truth from "review comments, plus follow-up commits that change
the commented lines". This design makes that wording precise without changing it:
- An item is one review comment.
- A follow-up commit is the evidence that the author acted on the comment. A commit without a
  comment makes no item.
- A comment the author did not act on is not ground truth. A finding that matches it is judged as
  an unmatched finding (spec `review-scoring`, Labels for unmatched findings), so the judge, not
  the ground truth, decides whether it counts toward precision.

A comment becomes an item when all of these hold:
- it opens its thread (it is not a reply);
- it sits on the new side of the diff at the reviewed head, with a line range;
- its author is a GitHub `User`, not a bot and not the PR's author;
- a later non-merge commit on the PR's first-parent path (below), committed after the comment was
  posted, changes those lines. Changing means it deletes or replaces one of them, or inserts lines
  between them or right after the last one; a deleted file counts as a change.

The lines are followed from the reviewed head along the PR's *first-parent path*: the commits
reached from the PR's head by first parents, back to the reviewed head. GitHub gives every
commit's diff, a merge's too, against its first parent, so each diff on this path applies to the
tree the previous one left. A commit off the path (one brought in by a merge) is never read and is
never evidence: its diff describes another tree. Each diff on the path is read on all its pages:
- An edit above the lines moves them, and a pure rename (no content change) carries them to the new
  path. Inserting lines between them grows the range; inserting right after the last one keeps it.
- Following the lines is separate from the evidence rule. A merge is never evidence, since it
  brings in the base branch, and neither is a commit whose committer date is not after the comment,
  since nobody could have acted on the comment yet; the committer date is no later than the push,
  so this rule only errs toward dropping an item. When such a commit only moves the lines or
  inserts next to them, they stay followed and a later commit can still be the evidence; when it
  deletes or replaces one of them, they are gone and the comment is not an item.
- A commit whose diff GitHub does not show (binary or too large) gives no evidence, and the lines
  are no longer followed.

Comments on removed lines and comments on a whole file are not items, because the item format needs
a line range in the reviewed tree. The GitHub API lists at most 250 commits of a PR; when the PR's
head is not among them, its first-parent path cannot be traced, so the PR is skipped and named.

**Field mapping to spec `review-tasks`.**

| Field | Source |
| --- | --- |
| task id | `<owner>_<repo>_<number>-<first 8 hex of the reviewed head>`, lowercase |
| source repository, PR number | the mined repository and PR |
| base and head commits | the merge base and the reviewed head (above) |
| SHA-256 of the source archive | computed by the import from the bundle (D11), fetched from the repository itself |
| language | the repository's primary language on GitHub |
| set | `ours` |
| PR title and description | D1's rule as it stands: the values in force from the reviewed head's committer date to the merge, or the task is skipped and the changelog names it |
| item id | `<task id>#<comment id>` |
| file, line range | the comment's path and `original_start_line`–`original_line` |
| message | the comment's body, verbatim |
| category | from the path alone: test files → `testing`, docs and `.md`, `.rst`, `.txt` → `documentation`, else `other`; no metric uses it |
| severity | the rule below, or the maintainer's label |
| producer | `human` |
| source | `<comment URL> fixed-by <commit sha>`: the comment and the commit that acted on it |
| canary | `rbench-canary-` and the first 32 hex digits of the SHA-256 of `rbench-ours-canary:<task id>`, so a fresh import is byte-reproducible; a task that already has a canary keeps it |

**Severity rule.** The import gives every item a pre-label with no model call. The rules are
applied in this order:
- `nit`: the comment opens with nit, nitpick, minor, optional, style or typo, as a word or as a
  Conventional Comments label;
- `must`: the comment's first line has `blocking` as one token of a decoration, as in `(blocking)`
  or `(blocking, security)` but not `(non-blocking)`, or the comment belongs to a review that
  requested changes;
- `should`: every other item.

`--label-sheet <file>` only mines, and writes one CSV row per item: task id, item id, file and
lines, link, pre-label and an empty label. The maintainer labels each row `M`, `S` or `N` (spec
`review-tasks`, Severity mapping from external sets). `--labels <file>` applies the filled sheet.
With `--require-labels`, an item without a label refuses the import and is named. The sheet is
working data for the maintainer and is never committed: the import refuses a path that git tracks,
and it quotes every cell built from GitHub text that a spreadsheet would read as a formula (one
starting with `=`, `+`, `-`, `@`, a tab or a carriage return). The manifest's `source` records the
repositories, the window, the filters, the M/S/N map, this rule, and where the imported items'
severities came from: all from the `rule`, all from the `maintainer`, or both (`rule+maintainer`).

A run that finds a skip the changelog does not name yet makes a new version that names it, even
when no task changed.

**Decided (A):** the pre-label is not final. The maintainer confirms every item's severity through
the label sheet, and the import runs with `--require-labels`, so every imported item's severity
comes from the `maintainer`.

**Alternatives considered:**
- *Severity from a model.* It costs model calls and adds another judge whose errors would reach the
  ground truth.
- *Every comment as ground truth.* It would count questions, praise and rejected suggestions.

## Risks / Trade-offs

- **[Claude OAuth token returns 401 in containers (reported upstream).]** → Spiked (#19): not
  reproduced. A `claude setup-token` token passed by `--env-file` as `CLAUDE_CODE_OAUTH_TOKEN`
  authenticates `claude -p` in a Linux container on Docker Desktop (Claude Code 2.1.316,
  linux/arm64) with an empty `CLAUDE_CONFIG_DIR`, also with D5's managed settings and shim flags.
  D3 stands; the `sbx` and separate-user fallbacks are not needed. #19 ran with the managed settings
  and shim flags but no proxy. #21 ran `claude -p` with the same token through D3's enforcing proxy,
  with and without D5's traffic switches but without the managed settings or shim flags. Credential
  source (decided): on
  macOS the operator keeps the token in the Keychain (generic password, service
  `rbench-claude-oauth`). rbench reads it at trial start and passes it to `docker` by variable name
  only (D5: named without a value in the compose file, the value in the environment of
  `docker compose up`), so the value never appears
  on an argv, in a file in the run dir or in an image (configuration or layer). The env file the
  spike used (`<env-file>`, outside the repository, mode 600) is a fallback for the spike only. Not
  yet checked: whether the managed settings are enforced (the spike checked authentication only)
  and token expiry over a long run.
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
  A minimal `codex exec` call works through D3's enforcing proxy (#21); `codex exec review`
  through it remains a smoke check.
- **[The inner Linux sandbox fails inside Docker.]** → Confirmed in #22 and #25: bubblewrap cannot
  create a user namespace in the container, and Codex 0.171.0 has no sandbox without it. D6 uses
  the container as the sandbox for rreview. The Codex shim rewrites review-council's
  `--sandbox read-only` to `danger-full-access` (D5). review-council's Codex reviewer can then also
  write inside the container. Edits are not scored as output. But a write to the checkout can change
  what a later reviewer, verifier or judge of the same trial reads, and so change the scored
  findings. The smoke stage records whether the checkout changed during review-council trials.
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
- **[Shim flags conflict with an engine's own flags.]** → Spiked (#25): each engine's default ran
  once with prototype shims. The conflicts found (a variadic flag, an `exec`-only flag, flags on
  non-session commands, duplicate model and effort, an engine's own read-only sandbox, settings
  passed by environment, `--session-id` with continuations) and their fixes are in D5. #25
  exercised fresh launches only, one model per CLI at `low` (review-council also at `medium`), and
  no proxy. The rreview judge and its wrap-up resume never ran. The D3 proxy, other models and
  efforts, and continuations and forks are left to the smoke stage, and so is a new engine or CLI
  version.
- **[A process inside the container forges the audit's records.]** → Out of scope (decided). The
  audit detects sessions started without the shims, not forged shim, watcher or hook records: a
  process running as the trial's user could send records to the collector or fake a hook's parent.
  Tolerated because what such a process could gain is small. Engines' writes are not scored, the
  container is torn down after the trial, and the network policy (D3 proxy, managed configuration)
  is enforced below the shims, so a forged record cannot reach the network or the ground truth.
- **[A session can escape the binding.]** → The Codex watcher polls, so a session that opens and
  closes its rollout between two polls stays unbound and fails its trial closed (D5). The smoke
  stage counts such false alarms. The Claude binding depends on the managed SessionStart hook. Only
  user, project and `--settings` values of `disableAllHooks` were tried against it (#25), and a
  Claude Code upgrade reruns that check.
- **[An engine skips its own fan-out on a small change.]** → At effort `low` on the spike's one-line
  diff, review-council 0.14.0 did not dispatch its reviewers: no subagent and no Codex reviewer
  ran, and it reported one unverified finding. At `medium`, the lowest matrix effort, it dispatched
  all three, Codex included. The smoke stage checks the roster review-council reports on a core
  task.
- **[In #25 the plugin review engine's lifecycle hooks did not run under `codex exec`.]** → The
  plugin's install notes have the user trust its hooks in the Codex TUI (`/hooks`). The fresh
  trial's transcript showed no hook output, so the engine ran its skill without them. The hooks did
  run on Claude Code. Headless `exec` has hook-trust handling of its own, so this run leaves open
  what happens once the pinned plugin's hooks are trusted. Whether the trial home pre-trusts them is
  open for the engine's adapter.
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
  findings. The smoke stage counts such calls in `plain` transcripts. In spike #26 (0.171.0,
  `gpt-6.2-astra`) the request never mentioned `apply_patch`; other models may differ.
- **[A remote model catalog may override `model_catalog_json` under a ChatGPT login.]** → Checked
  in spike #26 with Codex CLI 0.171.0 and the real login: it does not. Codex fetched the remote
  catalog for `codex debug models`, but a session with `model_catalog_json` used the local
  catalog only (D12).
- **[`plain`'s Codex tool list was observed for one model, without the proxy.]** → Spike #26 read
  the request of `gpt-6.2-astra` at `low` effort only, outside the D3 proxy and with a catalog
  built from the remote snapshot. The intended two-tool request is backed by the source only
  (D12). The smoke stage (#60) runs `plain` for both Codex matrix models through the proxy with
  the `--bundled` catalog. It reads the tool list from the request body in the rollout trace,
  since neither the session rollout nor a transcript count of `apply_patch` attempts shows what
  was offered. A CLI upgrade repeats this check.
- **[GPT models run outside their native code mode in `plain`.]** → The pinned catalog makes
  `gpt-6.2-*` code-mode-only, with subagent and image tools. `plain` overrides that (D12),
  because spec `engine-configurations` gives `plain` the same file-reading and shell tools on
  both CLIs. `plain` therefore measures each model with that minimal toolset. Each other engine
  runs in its own harness, with its own prompts and settings, so a `plain` score compares with an
  engine's score as minimal tools against that engine's harness. The report states this next to
  the `plain` results on the leaderboard and the quality-against-cost plot (#66).
- **[Codex engines other than `plain` keep the apps connectors under a ChatGPT login.]** →
  `features.apps` is on by default and is turned off only for `plain` (D12). Turning it off for
  every Codex engine (D5) is #104, a prerequisite for scoring Codex trials (see "Codex loads the
  account's ChatGPT apps" below).
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
- **[The allowlist and refused traffic (#21).]** → The run fixed the list from one minimal call per
  CLI. A path it did not exercise (a refresh, a tool call, review mode, another model) may need a
  host it never saw:
  - Such a path shows up as a refused attempt in run health; the call fails rather than leaks.
  - A host is added only with a logged reason.
  - In the one enforced Codex run, under the default settings, the proxy refused seven attempts:
    one metrics call to `ab.chatgpt.com` and six fetches from `*.oaiusercontent.com`, whose purpose
    was not identified. Other runs may differ. Refused attempts do not contaminate a trial (spec
    `trial-isolation`), but they are noise in run health.
  - Whether Codex's switches (OTel metrics, apps) stop this traffic, the storage fetches included,
    is untested and open.
  - Codex's token refresh through the proxy is unverified: the run's token was fresh, so
    `auth.openai.com` is listed from #20 and the 0.171.0 source, not from a logged connection.
  - No CLI ignored the proxy env. If a future version does, it gets no route at all, because the
    agent's bridge is internal and isolated (D3), so it fails visibly instead of leaking.
- **[Codex loads the account's ChatGPT apps.]** → Under a ChatGPT login, Codex 0.171.0 fetched the
  account's apps (connectors) and cached their tools in the trial's `CODEX_HOME` (#21). They are
  served through `chatgpt.com`, which the model call needs too. The proxy log shows the same
  allowed host for both, so neither the allowlist nor the audit can tell them apart. #26 turned the
  apps off for `plain` with `-c features.apps=false`; #104 does it for every Codex engine in D5's
  managed policy. Verified apps disablement is a prerequisite: no Codex trial is scored before #104
  shows it.
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
  - D15 states how the `ours` set is mined: the window after the training cutoffs, the
    repositories, the precise reading of this decision (a follow-up commit is the evidence that a
    comment was acted on), the field mapping and the severity rule.
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
