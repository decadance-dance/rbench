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

The runner SHALL skip a source trial whose status is missing, unreadable or anything other than
`done`. If a matching source trial has a valid `done` status but its findings or usage fail
validation, the runner SHALL refuse the new run and identify the source trial. For runs with hidden
tasks, the diagnostic SHALL use the trial's position in the source plan.

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

#### Scenario: Source trial status unavailable or unfinished
- **WHEN** a matching source trial has a missing or unreadable status, or its status is not `done`
- **THEN** the new trial starts pending and other eligible source trials are still reused

#### Scenario: Invalid results for a done source trial
- **WHEN** a matching source trial has a valid `done` status but its findings or usage fail validation
- **THEN** the new run is refused and the error identifies the source trial by its position in the
  source plan if the run includes hidden tasks
