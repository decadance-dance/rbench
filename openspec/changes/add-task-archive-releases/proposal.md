# Proposal

## Why

Spec `review-tasks` (Source archive) says the archives of public tasks are published with each
task-set version, and design D11 makes them GitHub release assets fetched by `rbench tasks fetch`.
Neither says how a release is created, what it refuses, or how a download is checked. Issue #33
adds a working `rbench tasks release` command that writes to GitHub, so its behavior is specified
here.

## What Changes

- `rbench tasks release <set>` publishes one GitHub release per version of a public set (`reviewbench`
  or `ours`). Its tag is `tasks-<set>-v<version>`, and its assets are the set's source archives,
  each named by its SHA-256. Every archive is verified against its task before anything is uploaded.
- The release targets the commit that last changed the set's manifest. That commit must already be
  on GitHub, and the set must have no uncommitted changes.
- The hidden set, and any hidden task, is refused. So is a version whose tag already has a release,
  a draft or a git tag; a published version never changes.
- `rbench tasks fetch` downloads the archives of the public sets and verifies each one against the
  SHA-256 its task records before it is kept; a mismatch names the task and leaves nothing behind.

## Impact

- Affected spec: `review-tasks` (adds Requirement: Published task archives).
- Code: `src/rbench/tasks/release.py`, `src/rbench/cli.py`.
- No change to task contents, manifests, hashes or scores.
