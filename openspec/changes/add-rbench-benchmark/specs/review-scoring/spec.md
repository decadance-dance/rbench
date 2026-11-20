# Spec Delta

## Purpose

Turns each trial's findings into comparable scores. Findings are matched to ground truth with a
pinned judge, then metrics per trial and per configuration are computed, with their uncertainty and
paired comparisons for regression checks.

## ADDED Requirements

### Requirement: Matching findings to ground truth
A pinned judge SHALL match each trial's findings to ground-truth items:
- a match means the same underlying issue;
- location is context only;
- one finding can match several items, and the reverse.

Every score SHALL record the judge model, its effort, the matcher version and the judge prompt hash.

#### Scenario: Same issue, different line
- **WHEN** a finding describes a ground-truth item's issue but points a few lines away
- **THEN** the judge matches them

#### Scenario: Same area, different failure
- **WHEN** a finding points at the same code as a ground-truth item but describes a different
  failure
- **THEN** the judge does not match them

### Requirement: Labels for unmatched findings
The judge SHALL label every finding that matches no ground-truth item as one of:
- `valid` (a real issue missing from ground truth);
- `false`;
- `nit`.

#### Scenario: Real issue outside ground truth
- **WHEN** an unmatched finding describes a real defect
- **THEN** it is labelled `valid` and counts toward augmented precision

### Requirement: Judge isolation and caching
- The judge SHALL be blind to the engine and configuration that produced the findings.
- Judge results SHALL be cached by the hash of the findings, the ground truth, the judge model and
  the prompt.
- Judging SHALL be resumable under the same limit handling as trials.

#### Scenario: Re-judging unchanged findings
- **WHEN** judging runs again with the same findings, ground truth, model and prompt
- **THEN** the cached result is used and no judge tokens are spent

### Requirement: Per-trial metrics
Every judged trial SHALL report:
- grounded recall, separately for `must`, `should` and `nit` items;
- grounded and augmented precision;
- the number of false findings;
- the number of nits;
- the engine's wall time;
- token usage per model: input, output, cache read and cache creation, summed over all its CLI
  sessions, including side calls to small models.

Normalization and judge tokens SHALL be reported apart from the engine's tokens.

#### Scenario: Multi-process engine
- **WHEN** an engine runs three Claude sessions and two Codex sessions
- **THEN** the trial's token usage is the sum of all five, split by model

#### Scenario: Missing session usage
- **WHEN** fewer sessions have usage than the CLI launches recorded
- **THEN** the trial's token figure is flagged as incomplete

### Requirement: Per-configuration aggregates
For each configuration and task set, the benchmark SHALL report:
- each metric's mean, with a 95% interval from a bootstrap that resamples tasks;
- tokens and wall time as median and interquartile range;
- per ground-truth item, the share of attempts that found it.

#### Scenario: Stability of one item
- **WHEN** a configuration found a `must` item in 4 of 5 attempts
- **THEN** that item's hit rate is reported as 4 of 5

### Requirement: Paired comparison
Comparing two configurations SHALL use only the tasks and attempts both completed. It SHALL report
each metric's delta with a 95% interval, and SHALL flag a change only when the interval excludes 0.

#### Scenario: Regression check
- **WHEN** rreview at two refs is compared and its `must` recall delta interval spans 0
- **THEN** no regression is flagged for `must` recall

### Requirement: Self-produced ground truth is marked
When an engine is scored on ground-truth items that it, or one of its models, produced, the
benchmark SHALL mark the affected scores.

#### Scenario: rreview scored on its own items
- **WHEN** ground-truth items in a task were produced by rreview
- **THEN** rreview's scores on that task are marked as including self-produced ground truth
