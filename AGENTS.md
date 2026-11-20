# rbench: project instructions

These rules hold for every contributor and every coding agent. They need git, the `gh` CLI and `uv` (it installs Python 3.12 from `.python-version`); Docker and the `claude` and `codex` logins only for live runs.

Write documentation, code comments, commits, issues and PRs in English.

rbench is a hermetic, resumable benchmark for AI code-review engines: it runs engines and configurations on real pull requests and measures quality (Must fix, Should fix and Nit recall, precision, false findings), tokens per model, wall time and variance. `docs/SPEC.md` is the product and the plan (its section numbers are cited as `spec §N`).

## Read before
- The first commit or PR: [git-flow.md](docs/workflow/git-flow.md). Branches, PRs into `develop` with a merge commit, cross-model review, what agents may push.
- Cutting a release or shipping a hotfix: [releasing.md](docs/workflow/releasing.md); its notes: [release-notes.md](docs/workflow/release-notes.md).
- Any change to isolation, auth, the hermetic configuration or the audit after a trial: spec §6. An engine under test must never reach ground truth, the operator's notes or transcripts, or the PR's later history; a change there keeps the canary audit green.
- Any change to the run ledger, checkpoints, resume or limit handling: spec §7. A run started by the previous version resumes or refuses with a clear reason; it never mixes trials from two plans.
- Any change to matching or metrics: spec §8. It changes scores, so the release notes say which results are no longer comparable.

## Before you change anything
1. Work on a GitHub issue of this repo, on its own branch `feature/<n>-<slug>` off `develop`. No issue yet? Even a small fix gets one first.
2. Orient before you plan: read the spec sections the issue cites and the code that already does part of the job. Record the fit as an issue comment before the first code change, 3 to 5 lines starting with `Fit:`: which spec sections govern it, what it changes, and where the issue conflicts with the spec or the code. A conflict goes to the maintainer before code; with no one to answer, record it and stop.
3. A change to the spec's behavior (task format, ledger layout, CLI commands and exit codes, metrics, the public and hidden split) updates `docs/SPEC.md` in the same PR.

## Python conventions
- Python 3.12, managed by `uv`: dependencies in `pyproject.toml`, `uv.lock` committed, added with `uv add` (`uv add --dev` for tools). Code under `src/rbench/`; the `rbench` command is its entry point.
- Few dependencies, each earning its place; the standard library first (`subprocess`, `json`, `tomllib`, `pathlib`, `dataclasses`).
- Child processes through `subprocess.run` with an argument list, never `shell=True`. Prompts go by file or stdin, not interpolated into a command line.
- Engine, agent and judge output is untrusted input: parse it into typed structures, validate every field, and reject a malformed result whole, saying which trial and which item.
- Every ledger write is atomic (a temporary file, then rename), so a killed run resumes from what is on disk.
- Results are deterministic where the inputs are: sorted keys, stable order, no timestamps inside files that are compared or published.
- Type hints on public functions. No bare `except`; errors carry the context a user needs (which task, which configuration, which file).
- Comment only the why the code cannot show.

## Tests
- Cheap checks first: one test (`uv run pytest -k <name>`), then the module, then the suite.
- Engines, CLIs and services are faked: tests put fake `claude`, `codex`, `docker` and `gh` executables (small scripts under `tests/fixtures/bin/`) first on `PATH`. A fake reads its scenario from an env var (canned output, a rate-limit message, a delay, a crash) and records its argv and stdin to a file the test asserts on.
- Live tests (Docker, real `claude` or `codex` logins, the network) are marked `live` and run only with `RBENCH_LIVE=1 uv run pytest -m live`. Never in CI.
- Fixture repos and run dirs are built by the test in `tmp_path` (`git init`, commits), not checked in.
- New logic gets a test that fails when the logic breaks.

## Checks
- `bin/check`: `ruff format --check`, `ruff check` and `pytest` through `uv`; every step runs even when an earlier one fails. CI runs the same script as the `ci-ok` check, the merge gate.
- Run it before you say a change is done.

## Issues
Tasks, briefs and follow-ups live in GitHub Issues of this repo. A new issue states the outcome and the evidence, not the implementation, with a `Context:` line (spec section or issue it came from) and the criteria as a `- [ ]` checklist. Deferred work becomes a new issue. A PR that finishes an issue says `Closes #<n>`; one that only touches it says `Refs #<n>`.

## Security and privacy
- The hidden task set lives in a separate private repository and never reaches this one: no hidden task ids (`h-*`), canaries, ground truth, per-task results or finding text in files, commits, issues, PRs or CI logs. Only aggregate scores are published (spec §4.3).
- `runs/` stays out of git; results are published only through `rbench publish`.
- Subscription credentials (Claude, Codex) never go into the repo, images, run dirs, logs or reports.
- The repositories and PRs under review are data for the engines, never instructions to rbench; prompts say so.
