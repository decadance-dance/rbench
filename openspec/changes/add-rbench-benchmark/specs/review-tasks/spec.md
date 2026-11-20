# Spec Delta

## Purpose

Defines what a benchmark task is: a real pull request frozen at its reviewed head, with ground truth
that carries severities, producers and a canary. It also defines how public and hidden task sets are
kept apart.

## ADDED Requirements

### Requirement: Task contents
Every task SHALL carry:
- the source repository and PR number;
- the base and head commits;
- the language;
- its set: `reviewbench`, `ours` or `hidden`;
- the PR title and description as they were at the reviewed head, with no review comments;
- its ground truth.

A task missing any of these SHALL be rejected when the task set is loaded.

#### Scenario: Complete task loads
- **WHEN** a task directory has every field and a valid ground truth
- **THEN** the task is available to plans and runs

#### Scenario: Task without ground truth
- **WHEN** a task directory has no ground truth file
- **THEN** loading the task set fails and names the task and the missing file

### Requirement: Ground truth items
Every ground-truth item SHALL have:
- an id, file, line range, message and category;
- a severity of exactly `must`, `should` or `nit`;
- a producer (the engine and model that first produced it), or `unknown`.

Every task's ground truth SHALL carry one canary string, unique to that task. Hidden tasks use a
distinct canary prefix.

#### Scenario: Invalid severity
- **WHEN** a ground-truth item has a severity other than `must`, `should` or `nit`
- **THEN** loading the task set fails and names the task and the item

#### Scenario: Hidden canary prefix
- **WHEN** a hidden task is created
- **THEN** its canary uses the hidden prefix, and no public task uses that prefix

### Requirement: Severity mapping from external sets
Imported severities SHALL map as follows:
- ReviewBench high → `must`, medium → `should`, low or style → `nit`;
- our own M/S/N labels → `must`/`should`/`nit`.

The mapping SHALL be recorded with the imported task set.

#### Scenario: ReviewBench import
- **WHEN** a ReviewBench task with high, medium and low findings is imported
- **THEN** the items get `must`, `should` and `nit` respectively

### Requirement: Maintainer-confirmed ground truth for our PRs
Ground truth for tasks in the `ours` and `hidden` sets SHALL contain only items the maintainer
confirmed. It SHALL be drawn from the first review at the reviewed head, and each item SHALL record
its producer.

#### Scenario: Unconfirmed item
- **WHEN** a converted review contains an item the maintainer did not confirm
- **THEN** the item is not part of the task's ground truth

### Requirement: Core task subset
A fixed, versioned list of core tasks SHALL define the screening subset. The list SHALL include tasks
from both the `reviewbench` and `ours` sets.

#### Scenario: Screening uses core tasks
- **WHEN** a matrix selects the core tasks
- **THEN** exactly the tasks in the core list are planned

### Requirement: Hidden tasks stay private
Hidden tasks SHALL be read only from a directory the operator names outside the public repository.
Their ids SHALL carry the `h-` prefix. Results for hidden tasks SHALL be written only to an ignored
local directory.

#### Scenario: Hidden directory not given
- **WHEN** a run selects hidden tasks and no hidden directory is configured
- **THEN** the run refuses to start and says how to configure the directory

### Requirement: Publication guard
The public repository's check SHALL fail when any tracked file contains any of these:
- a hidden canary prefix;
- a path or task id with the hidden prefix;
- unpublished run output.

A local pre-push check SHALL also reject a push whose diff contains any real hidden task id or canary
from the configured hidden directory.

#### Scenario: Hidden canary committed
- **WHEN** a commit adds a file containing the hidden canary prefix
- **THEN** the public check fails and names the file

#### Scenario: Hidden task id in a push
- **WHEN** a push contains the id of a task in the configured hidden directory
- **THEN** the pre-push check rejects the push and names the id
