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

rbench is licensed under [Apache-2.0](LICENSE).
