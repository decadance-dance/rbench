# Spec Delta

## ADDED Requirements

### Requirement: Task archive release
`rbench tasks release <set>` SHALL publish a version of a public set (`reviewbench` or `ours`) as one
GitHub release tagged `tasks-<set>-v<version>`, with one asset per distinct archive, named by its
SHA-256. Every archive SHALL match the SHA-256 its task records before anything is uploaded. The
release SHALL target the commit that last changed the set's manifest, which SHALL already be on
GitHub and SHALL hold the set exactly as released; a set with uncommitted changes SHALL be refused.

#### Scenario: Older version published later
- **WHEN** version 1 of a set is released from a checkout of the commit that recorded it, after
  version 2 was committed
- **THEN** the release `tasks-<set>-v1` targets the commit holding the version 1 manifest

#### Scenario: Altered local archive
- **WHEN** an archive to upload does not match the SHA-256 its task records
- **THEN** the command fails naming the task, and nothing is uploaded

### Requirement: Task archive release refusals
`rbench tasks release` SHALL refuse the hidden set and a hidden task in a public set directory
before contacting GitHub. It SHALL refuse a version whose tag already has a release, a draft or a
git tag, so a published version never changes; the error SHALL say that a draft left by an
interrupted upload is deleted by hand.

#### Scenario: Hidden set refused
- **WHEN** `rbench tasks release hidden` is run, or a public set directory holds a hidden task
- **THEN** the command fails before contacting GitHub, and no release is created

#### Scenario: Version already released
- **WHEN** a release or a draft already has the tag of the set's current version
- **THEN** the command fails naming the tag, and the existing release is not changed

### Requirement: Task archive fetch
`rbench tasks fetch` SHALL download the archives of the public sets from their version's release
and SHALL keep a downloaded archive only when its SHA-256 matches the one its task records.

#### Scenario: Altered download
- **WHEN** a downloaded archive does not match the SHA-256 its task records
- **THEN** `rbench tasks fetch` fails naming the task, and the archive directory holds no file from
  that download
