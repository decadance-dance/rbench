# rbench

A hermetic, resumable benchmark for AI code-review engines: it runs engines and configurations on real pull requests and measures quality (Must fix, Should fix and Nit recall, precision, false findings), tokens per model, wall time and variance.

Status: design. The spec is the OpenSpec change [add-rbench-benchmark](openspec/changes/add-rbench-benchmark/): proposal, design and one spec per capability. The `rbench` command is a skeleton so far: `uv run rbench --help` lists the planned subcommands, which are stubs.

- Contributing, for people and coding agents: [AGENTS.md](AGENTS.md).
- Branches, pull requests and review: [git-flow](docs/workflow/git-flow.md); releases: [releasing](docs/workflow/releasing.md).

## Hidden tasks

The hidden task set is never in this repository. To run it, point rbench at a directory outside this checkout that holds the hidden tasks, one task directory each, and their configuration:

```sh
export RBENCH_HIDDEN_DIR=/path/to/hidden-tasks
```

`$RBENCH_HIDDEN_DIR/rbench-hidden.toml` holds the hidden canary prefix:

```toml
canary_prefix = "<the hidden canary prefix>"
```

A run that selects hidden tasks without this configuration refuses to start. Hidden tasks load only from that directory, and their results go only to a path git ignores, such as `runs/`.

rbench is licensed under [Apache-2.0](LICENSE).
