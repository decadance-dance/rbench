## Why

The publication guard requirement of `review-tasks` asks the public check to fail on a hidden canary
prefix. The hidden canary prefix is operator configuration that lives only in the hidden directory,
never in this repository, so the public check in CI, which has no hidden directory, cannot know it.
The maintainer decided (2026-11-21) that the prefix stays operator configuration and is checked where
the hidden directory is configured (#36).

## What Changes

- The public check, in CI too, fails on a tracked file with a path or task id with the hidden
  prefix, or on unpublished run output. It names the file without printing the matched value.
- The hidden canary prefix, the real hidden task ids and their canaries are checked where the hidden
  directory is configured: by the local pre-push check, and by the public check when it runs
  locally with the hidden directory configured. A configured hidden directory that cannot be read
  fails that check.
- The scenario "Hidden canary committed" now describes the local check.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `review-tasks`: the Publication guard requirement says which check looks for which value.

## Impact

- `src/rbench/tasks/guard.py`, `bin/check` and `bin/pre-push` (#36).
- No task, ledger or metric format changes; no result becomes incomparable.
