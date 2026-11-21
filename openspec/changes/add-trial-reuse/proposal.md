## Why

Issue #71 lets an operator carry completed trials into a new run when the relevant frozen inputs
still match. The existing reuse requirement states which inputs must match. This change specifies
how the operator requests reuse and how the ledger records the source run.

## What Changes

- `rbench run --reuse <run-directory>` opts into reuse when creating a smoke or screening run.
- A reused trial carries a private copy of the validated findings and usage and records the source
  run id in the optional `reused_from` field of its done status. Raw files stay in the source run,
  which can be read while it is locked by a runner.
- The full stage refuses `--reuse` because it requires fresh attempts.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `run-ledger`: Reuse of earlier trials.

## Impact

- Affects run creation and trial checkpoints. Existing done statuses remain readable.
- Archive after `add-rbench-benchmark`, which introduces the requirement this change modifies.
