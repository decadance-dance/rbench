## MODIFIED Requirements

### Requirement: Reuse of earlier trials
When the operator names an earlier run with `rbench run --reuse <run-directory>`, the new run
SHALL take its done trials, but only those where all of these match:
- the task's content hash;
- the configuration hash;
- the engine version;
- the CLI versions;
- the image digest.

The runner SHALL copy the validated findings and usage into the new run before marking the trial
`done`. Its done status SHALL record the source run id in `reused_from`, an optional field.
Raw files SHALL stay in the source run. Reuse SHALL leave the source run unchanged and SHALL NOT
require its lock. Trials that are not reused SHALL start pending. The run creation summary SHALL
report the number of pending trials.

The runner SHALL refuse `--reuse` for the full stage and name the stage in the error.

#### Scenario: Engine version differs
- **WHEN** an earlier trial ran a different rreview ref
- **THEN** it is not reused

#### Scenario: Task changed between set versions
- **WHEN** the new run uses a set version that changed one task's ground truth
- **THEN** trials of that task are not reused, and trials of unchanged tasks are

#### Scenario: Earlier trial copied
- **WHEN** an eligible done trial is reused from the named earlier run
- **THEN** the new trial has its own findings and usage, and its done status records the source
  run id in `reused_from`
