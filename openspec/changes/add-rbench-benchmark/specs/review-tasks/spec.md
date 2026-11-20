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
- the SHA-256 of its source archive;
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
- a producer: the engine and model that first produced it, `human` for a human reviewer, or
  `unknown`.

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

### Requirement: Recorded origin of ground truth for our PRs
Every ground-truth item in the `ours` and `hidden` sets SHALL record its producer and its source:
the review, comment or commit it was taken from. How these sets obtain their ground truth is an open
decision (design.md, O5).

#### Scenario: Item without a source
- **WHEN** a ground-truth item in the `ours` set has no recorded source
- **THEN** loading the task set fails and names the task and the item

### Requirement: Source archive
When a task is imported, the benchmark SHALL fetch its source once and store it as an archive: a git
bundle of the scrubbed two-commit repository (see trial-isolation, Scrubbed repository). After
import:
- trials SHALL build their repository only from the archive, and SHALL verify its SHA-256 first;
- nothing SHALL contact the source repository again;
- archives of public tasks SHALL be published with each task-set version, and archives of hidden
  tasks SHALL stay in the hidden directory.

#### Scenario: Source repository gone
- **WHEN** the source repository of a task is deleted or force-pushed after import
- **THEN** the task still runs from its archive with the same result hash

#### Scenario: Archive altered
- **WHEN** a task's archive does not match its recorded SHA-256
- **THEN** the trial does not start, and the error names the task

### Requirement: Source license
Before a task of the `reviewbench` or `ours` set is imported, the benchmark SHALL read the license of
its source repository at the reviewed head and SHALL import the task only when that license is on
the benchmark's list of licenses that allow redistribution. A missing or unrecognized license SHALL
refuse the import. The license id SHALL be recorded in the set's manifest, and the license file
SHALL be kept in the archive.

#### Scenario: Repository without a license
- **WHEN** a PR from a repository with no license file is imported into the `ours` set
- **THEN** the import fails and names the repository

#### Scenario: Permissive license
- **WHEN** a PR from an MIT-licensed repository is imported
- **THEN** the task is imported, and the manifest records `MIT` for it

### Requirement: Task set versions
Every task set SHALL have a manifest with:
- a version;
- a content hash per task, covering its metadata, PR description, ground truth and archive hash;
- a changelog that names the tasks each version changed.

Any change to a task's content SHALL change its hash and SHALL require a new set version with a
changelog entry. The repository check SHALL fail when a task's content does not match its manifest
hash. Runs SHALL record the set version and the per-task hashes.

#### Scenario: Ground truth fixed without a new version
- **WHEN** a ground-truth item is edited and the manifest is not updated
- **THEN** the check fails and names the task

#### Scenario: New version with one changed task
- **WHEN** version 2 of a set changes the ground truth of one task
- **THEN** the changelog names that task, and every other task keeps its hash

### Requirement: Pinned ReviewBench source
Tasks of the `reviewbench` set SHALL be imported from one ReviewBench commit, recorded in the set's
manifest. Importing from another commit SHALL produce a new set version.

#### Scenario: ReviewBench updated upstream
- **WHEN** ReviewBench publishes new commits after the set was imported
- **THEN** the set is unchanged until it is imported again, as a new version

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
