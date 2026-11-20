# Spec Delta

## Purpose

Turns each trial's findings into comparable scores. Findings are matched to ground truth with a
pinned judge, then metrics per trial and per configuration are computed, with their uncertainty and
paired comparisons for regression checks.

## ADDED Requirements

### Requirement: Matching findings to ground truth
A pinned judge SHALL match each trial's findings to ground-truth items:
- a match means the same underlying issue, and counts the same whether the finding names its root
  cause or a symptom;
- only items in the finding's file are compared; within it, location is context only;
- one finding can match several items, and the reverse.

Every score SHALL record the judge model, its effort, the matcher version (pinned to a
ReviewBench commit) and the judge prompt hash.

#### Scenario: Same issue, different line
- **WHEN** a finding describes a ground-truth item's issue but points a few lines away in the same
  file
- **THEN** the judge matches them

#### Scenario: Same area, different failure
- **WHEN** a finding points at the same code as a ground-truth item but describes a different
  failure
- **THEN** the judge does not match them

#### Scenario: Same issue, different file
- **WHEN** a finding describes a ground-truth item's issue at a caller in another file, and the item
  is in the callee's file
- **THEN** they are not matched, and the item is a recall miss unless another finding matches it

### Requirement: Labels for unmatched findings
The judge SHALL label every finding that matches no ground-truth item as one of:
- `valid` (a real issue that matches no ground-truth item);
- `false`;
- `nit`.

#### Scenario: Real issue outside ground truth
- **WHEN** an unmatched finding describes a real defect
- **THEN** it is labelled `valid` and counts toward augmented precision

#### Scenario: Real issue reported in a different file
- **WHEN** a finding in a caller's file correctly describes a defect, and the only ground-truth item
  for that issue is in the callee's file, another file
- **THEN** the finding is labelled `valid` and counts toward augmented precision, and the item is a
  recall miss unless another finding of the trial matches it, such as one in the callee's file

### Requirement: Judge isolation and caching
- The judge SHALL be blind to the engine and configuration that produced the findings.
- The judge and the normalization step SHALL run hermetic, in the same container setup and shims as
  trials, with no tools.
- Judge results SHALL be cached by the hash of the findings, the ground truth, the judge model and
  the prompt.
- Judging SHALL be resumable under the same limit handling as trials.

#### Scenario: Re-judging unchanged findings
- **WHEN** judging runs again with the same findings, ground truth, model and prompt
- **THEN** the cached result is used and no judge tokens are spent

#### Scenario: Judge tries a tool
- **WHEN** the judge model asks to read a file or run a command
- **THEN** no tool is available to it

### Requirement: Agreement between judges
When a second judge labels a sample of trials, the benchmark SHALL report the agreement between the
two judges, split by the model family that produced the findings.

#### Scenario: Agreement by family
- **WHEN** a second judge labels a sample with findings from Claude and from GPT models
- **THEN** the report gives one agreement figure for Claude-produced findings and one for
  GPT-produced findings

### Requirement: Per-trial metrics
Every judged trial SHALL report:
- grounded recall, separately for `must`, `should` and `nit` items;
- grounded and augmented precision;
- the number of false findings;
- the number of nits;
- the engine's wall time;
- token usage per model: uncached input, output, cache read and cache creation, summed over all its
  CLI sessions, including side calls to small models.

Normalization and judge tokens SHALL be reported apart from the engine's tokens.

#### Scenario: Multi-process engine
- **WHEN** an engine runs three Claude sessions and two Codex sessions
- **THEN** the trial's token usage is the sum of all five, split by model

#### Scenario: Missing session usage
- **WHEN** fewer sessions have usage than the CLI launches recorded
- **THEN** the trial's token figure is flagged as incomplete

### Requirement: Token usage counted once
Each model message's usage SHALL be counted once, even when a transcript repeats it on several
lines.

#### Scenario: Usage repeated across transcript lines
- **WHEN** a Claude transcript repeats one message's usage on several lines with the same message id
- **THEN** that usage is counted once

### Requirement: Failed trials score zero
A `failed` trial SHALL score as a trial with zero findings.

#### Scenario: Failed trial
- **WHEN** a trial ends `failed` with a timeout
- **THEN** it scores zero recall and counts toward its configuration's failure rate

### Requirement: Per-configuration aggregates
For each configuration and task set, the benchmark SHALL report:
- each metric's mean, with a 95% interval from a bootstrap that resamples tasks;
- the standard deviation of recall across attempts;
- the failure rate: the share of trials that ended `failed`;
- tokens and wall time as median and interquartile range;
- per ground-truth item, the share of attempts that found it.

#### Scenario: Stability of one item
- **WHEN** a configuration found a `must` item in 2 of 3 attempts
- **THEN** that item's hit rate is reported as 2 of 3

### Requirement: Paired comparison
Comparing two configurations SHALL use only the tasks both completed, and only tasks with the same
content hash in both. It SHALL pair by task, using each configuration's mean over its attempts on
that task, not by attempt number. It SHALL report each metric's delta with a 95% interval, and SHALL
flag a change only when the interval excludes 0.

#### Scenario: Regression check
- **WHEN** rreview at two refs is compared and its `must` recall delta interval spans 0
- **THEN** no regression is flagged for `must` recall

#### Scenario: Different attempt counts
- **WHEN** one configuration has 3 attempts on a task and the other has 2
- **THEN** the task is paired using the mean of 3 and the mean of 2

### Requirement: Self-produced ground truth is marked
When an engine is scored on ground-truth items that it, or one of its models, produced, the
benchmark SHALL mark the affected scores.

#### Scenario: rreview scored on its own items
- **WHEN** ground-truth items in a task were produced by rreview
- **THEN** rreview's scores on that task are marked as including self-produced ground truth
