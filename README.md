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

`bin/check`, and so CI, fails when a tracked file's path or content holds a task id with the hidden prefix, or when anything under `runs/` is tracked; it names the file and line, never the id. Tests build hidden ids at runtime from `HIDDEN_ID_PREFIX`, never as literals. The hidden canary prefix is known only in the hidden directory, so the local pre-push check covers it: with `RBENCH_HIDDEN_DIR` set, it rejects a push whose commits, messages or ref names add a hidden task id, a canary or the canary prefix, and names the id on the terminal only. Install it once per clone; linked worktrees share it:

```sh
ln -s "$(pwd)/bin/pre-push" "$(git rev-parse --git-common-dir)/hooks/pre-push"
```

Without `RBENCH_HIDDEN_DIR` the hook prints a note and lets the push through; with a hidden directory it cannot read, it rejects the push.

rbench is licensed under [Apache-2.0](LICENSE).
