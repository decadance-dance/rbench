# Spec Delta

## Purpose

Presents a run's results as one readable, self-contained report that answers two questions: which
configuration is best for its cost, and did a change regress. It also controls what a published
report may reveal about hidden tasks.

## ADDED Requirements

### Requirement: Self-contained report
The benchmark SHALL write one HTML report and one machine-readable results file for any run,
including a partial one. The report SHALL open from disk without a server or network access.

#### Scenario: Partial run
- **WHEN** a report is generated for a run with 60% of trials done
- **THEN** the report opens offline and shows 60% completion

### Requirement: Overview
The report SHALL open with:
- the stages and task sets that were run;
- trial counts and completion;
- the judge model and prompt hash;
- what could not be pinned;
- a short note on how to read the report.

#### Scenario: Judge identity shown
- **WHEN** a report is opened
- **THEN** its overview names the judge model and prompt hash used for every score in it

### Requirement: Leaderboard
The report SHALL show one row per configuration. Each row SHALL give:
- the engine, and the model and effort per role;
- recall by severity and augmented precision, each with its interval;
- false findings per PR;
- median tokens split by model;
- median wall time;
- the number of trials.

Rows SHALL be sortable, and filterable by engine, model, effort and task set.

#### Scenario: Filter by task set
- **WHEN** the reader selects the `ours` task set
- **THEN** every number in the leaderboard is recomputed for that set only

### Requirement: Quality against cost
The report SHALL plot `must` recall against median total tokens per configuration, colored by
engine, and SHALL mark the Pareto front.

#### Scenario: Dominated configuration
- **WHEN** a configuration has lower `must` recall and more tokens than another
- **THEN** it is not on the marked front

### Requirement: Compare view
The reader SHALL be able to pick two configurations and see:
- the paired delta of each metric, with its interval;
- the tasks where the two differ most.

#### Scenario: Two rreview refs
- **WHEN** the reader compares rreview at two refs
- **THEN** each metric shows its delta and interval, and the tasks with the largest differences are
  listed

### Requirement: Task drill-down
For each task, the report SHALL show:
- the PR summary;
- a grid of ground-truth items against trials, marking hit, partial and miss;
- every finding next to the item it matched, with the judge's reasoning;
- every unmatched finding with its label.

#### Scenario: Reading a miss
- **WHEN** the reader opens a task where a configuration missed a `must` item
- **THEN** the grid shows the miss for each attempt, and the item's text is visible

### Requirement: Run health
The report SHALL list:
- failed, interrupted and contaminated trials, with reasons;
- wasted tokens;
- flags for incomplete token figures and for effort fallbacks.

#### Scenario: Contaminated trial listed
- **WHEN** a trial was excluded as contaminated
- **THEN** the run health section lists it with its reason, and no score includes it

### Requirement: Hidden tasks in published reports
A published report SHALL show hidden tasks only in aggregate numbers. It SHALL NOT show any of these
for a hidden task:
- its id;
- its PR summary;
- its findings or ground truth;
- its per-task rows in the drill-down or the compare view.

#### Scenario: Publishing a run with hidden tasks
- **WHEN** a run that includes hidden tasks is published
- **THEN** the published report and results contain hidden-set aggregates per configuration and
  nothing per hidden task
