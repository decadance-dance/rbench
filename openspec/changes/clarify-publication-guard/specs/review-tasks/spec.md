## MODIFIED Requirements

### Requirement: Publication guard
The public repository's check SHALL fail when any tracked file has any of these:
- a path or task id with the hidden prefix;
- unpublished run output.

It SHALL name the file without printing the matched value.

The hidden canary prefix, the real hidden task ids and their canaries are operator configuration
kept in the hidden directory, so they SHALL be checked where the hidden directory is configured:
- a local pre-push check SHALL reject a push whose new commits, ref names or tags contain any of
  them;
- the public check, when run locally with the hidden directory configured, SHALL also fail when
  any tracked file contains any of them.

A configured hidden directory that cannot be read SHALL fail the check that reads it.

#### Scenario: Hidden task id committed
- **WHEN** a commit adds a file whose path or content holds a task id with the hidden prefix
- **THEN** the public check fails and names the file without printing the id

#### Scenario: Hidden canary committed
- **WHEN** a commit adds a file containing the hidden canary prefix and the hidden directory is
  configured
- **THEN** the local check fails and names the file without printing the value

#### Scenario: Hidden task id in a push
- **WHEN** a push contains the id of a task in the configured hidden directory
- **THEN** the pre-push check rejects the push and names the id
