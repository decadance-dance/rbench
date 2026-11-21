# rbench

A hermetic, resumable benchmark for AI code-review engines: it runs engines and configurations on real pull requests and measures quality (Must fix, Should fix and Nit recall, precision, false findings), tokens per model, wall time and variance.

Status: design. The spec is the OpenSpec change [add-rbench-benchmark](openspec/changes/add-rbench-benchmark/): proposal, design and one spec per capability. The `rbench` command is a skeleton so far: `uv run rbench --help` lists the planned subcommands, which are stubs.

- Contributing, for people and coding agents: [AGENTS.md](AGENTS.md).
- Branches, pull requests and review: [git-flow](docs/workflow/git-flow.md); releases: [releasing](docs/workflow/releasing.md).

## Hidden tasks

The hidden task set is never in this repository. The task loader reads it from a directory outside this checkout that holds the hidden tasks, one task directory each, and their configuration. This is loader configuration; `rbench run` does not select hidden tasks yet.

```sh
export RBENCH_HIDDEN_DIR=/path/to/hidden-tasks
```

`$RBENCH_HIDDEN_DIR/rbench-hidden.toml` holds the hidden canary prefix:

```toml
canary_prefix = "<the hidden canary prefix>"
```

Without this configuration, selecting hidden tasks fails and names what to set. The directory must lie outside the git checkout rbench runs from and the one it is installed from, so run rbench from its checkout. Every hidden task and file must resolve inside the directory, and errors about hidden tasks name only the variable, the file and a position. A guard refuses results of hidden tasks for any path git does not ignore, such as `runs/`; the run directory will call it once it exists (#53).

## Publication guard

`bin/check`, and so CI, fails when a tracked file's path, staged content or work-tree content holds a task id with the hidden prefix, or when anything under `runs/` is tracked. A task id here is the hidden prefix followed by at least one character the loader's id rule accepts (lowercase letters, digits, `_`, `.`, `-`), unless a letter or digit comes right before the prefix ("high-level"). The bare prefix and placeholders such as the prefix followed by `*` or `<n>` pass; a prose word that is also a valid hidden id fails on purpose, so reword it. Content is searched as UTF-8 or raw bytes and as UTF-16 and UTF-32. The check names the file by a redacted path and a line, never the matched value. Tests build hidden ids at runtime from `HIDDEN_ID_PREFIX`, never as literals.

The hidden canary prefix, the real hidden ids and their canaries live only in the hidden directory, so they are checked where `RBENCH_HIDDEN_DIR` is set: `bin/check` run locally also fails on a tracked file holding any of them, and the local pre-push check rejects a push whose new commits add one to a file, a path or a message, or whose ref names or annotated tags carry one. A commit counts as new unless the destination already advertises it; removed lines and unchanged context do not count. The hook may name the offending hidden id on the terminal, never a canary or the prefix, and writes nothing to a file. Install it once per clone; linked worktrees share it:

```sh
ln -s "$(pwd)/bin/pre-push" "$(git rev-parse --git-common-dir)/hooks/pre-push"
```

Without `RBENCH_HIDDEN_DIR` the hook prints a note and lets the push through; with a hidden directory it cannot read, both the hook and `bin/check` fail.

rbench is licensed under [Apache-2.0](LICENSE).
